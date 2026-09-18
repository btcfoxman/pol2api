from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
import threading
import time
import uuid

from app.cdp import read_session
from app.config import normalize_proxy_url
from app.db import now_ts
from app.model_catalog import (
    MEDIA_LIMITS,
    model_map_json,
    normalize_generation_request,
    public_models,
)
from app.pollo_client import PolloClient, SubmissionUnknown, UpstreamError, result_urls
from app.schemas import SettingsPatch
from app.task_errors import failure_diagnostic, public_failure

LOGGER = logging.getLogger("pol2api")
TERMINAL = {"succeeded", "failed", "expired"}


class PolService:
    runtime_fields = tuple(SettingsPatch.model_fields)

    def __init__(self, db, settings, client_factory=PolloClient):
        self.db = db
        self.settings = settings
        SettingsPatch(**{k: getattr(settings, k) for k in self.runtime_fields})
        self.client_factory = client_factory
        stored = {}
        for field in self.runtime_fields:
            raw = db.get_setting(field, None)
            if raw is not None:
                # Let the schema parse persisted strings, including decimal
                # thresholds and intentionally cleared text fields.
                stored[field] = raw
        if stored:
            for field, value in (
                SettingsPatch(**stored).model_dump(exclude_none=True).items()
            ):
                setattr(settings, field, value)
        self.settings.model_map = model_map_json(self.settings.model_map)
        self._executor = ThreadPoolExecutor(
            max_workers=50, thread_name_prefix="pol-task"
        )
        self._maintenance = ThreadPoolExecutor(
            max_workers=20, thread_name_prefix="pol-account"
        )
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._running = set()
        self._futures = {}
        self._checking = set()
        self._account_locks = {}
        self._thread = None

    def start(self):
        self._stop.clear()
        # A crash between POST and saving its ID must never replay the POST.
        with self.db.connect() as conn:
            uncertain = [
                dict(row)
                for row in conn.execute(
                    "SELECT id,account_id FROM tasks WHERE status='submitting'"
                )
            ]
        for task in uncertain:
            self.db.update_task(
                task["id"],
                status="failed",
                error_code="SUBMISSION_UNKNOWN",
                error_message="服务在提交期间重启，请先核对上游任务",
                completed_at=now_ts(),
            )
            if task["account_id"]:
                self.db.release_account(task["account_id"], task_id=task["id"])
                self.db.update_account(
                    task["account_id"],
                    {
                        "status": "login_required",
                        "last_error": "有提交结果未知的任务，请核对后刷新余额",
                    },
                )
        for task in self.db.recoverable_tasks():
            self._schedule(task["id"])
        self._thread = threading.Thread(
            target=self._maintain, daemon=True, name="pol-maintenance"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._executor.shutdown(wait=False, cancel_futures=False)
        self._maintenance.shutdown(wait=False, cancel_futures=True)

    def runtime_settings(self):
        return {
            **{k: getattr(self.settings, k) for k in self.runtime_fields},
            "media_limits": MEDIA_LIMITS,
        }

    def update_runtime_settings(self, changes):
        values = SettingsPatch(**changes).model_dump(exclude_none=True)
        if "model_map" in values:
            values["model_map"] = model_map_json(values["model_map"])
        with self._lock:
            self.db.set_settings(values)
            for k, v in values.items():
                setattr(self.settings, k, v)
        return self.runtime_settings()

    def models(self):
        result = public_models(self.settings.model_map)
        if not self.settings.allow_video_reference_inputs:
            for model in result:
                model["capabilities"]["media_limits"]["videos"] = 0
        return result

    def _account_lock(self, account_id):
        with self._lock:
            return self._account_locks.setdefault(account_id, threading.RLock())

    def _proxy(self):
        pool = [
            normalize_proxy_url(v)
            for v in self.settings.proxy_pool.replace(",", "\n").splitlines()
            if v.strip()
        ]
        counts = self.db.proxy_assignment_counts()
        return (
            min(pool, key=lambda p: counts.get(p, 0))
            if self.settings.proxy_pool_enabled and pool
            else ""
        )

    def upsert_account(self, payload, *, start_login=False):
        value = dict(payload)
        if value.get("project_id"):
            value["team_id"] = value.pop("project_id")
        value["proxy_url"] = normalize_proxy_url(value.get("proxy_url", ""))
        if not value["proxy_url"] and value.get("use_proxy_pool", True):
            value["proxy_url"] = self._proxy()
        record = self.db.upsert_account(value)
        if start_login:
            self.schedule_login(record["id"])
        return self.db.get_account(record["id"])

    def sync_account(self, payload):
        return self.upsert_account(payload, start_login=False)

    def batch_import(self, source, *, start_login=False, use_proxy_pool=True):
        if isinstance(source, str):
            source = json.loads(source)
        if not isinstance(source, list):
            raise ValueError("批量导入需要账号 JSON 数组")
        imported = []
        errors = []
        from app.schemas import AccountSyncRequest

        for index, raw in enumerate(source):
            try:
                value = AccountSyncRequest.model_validate(raw).model_dump()
                value["use_proxy_pool"] = use_proxy_pool
                record = self.upsert_account(value)
                imported.append(record)
                if start_login:
                    self._maintenance.submit(self._safe_check, record["id"])
            except Exception as exc:
                errors.append({"index": index + 1, "error": str(exc)})
        return {"imported": len(imported), "accounts": imported, "errors": errors}

    def update_account(self, account_id, changes):
        if not self.db.get_account(account_id):
            raise KeyError("account not found")
        if "project_id" in changes:
            changes["team_id"] = changes.pop("project_id")
        if "proxy_url" in changes:
            changes["proxy_url"] = normalize_proxy_url(changes["proxy_url"])
        return self.db.update_account(account_id, changes)

    def delete_account(self, account_id):
        account = self.db.get_account(account_id)
        if account and account["active_tasks"]:
            raise ValueError("运行中的账号不能删除")
        return self.db.delete_account(account_id)

    def schedule_login(self, account_id):
        account = self.db.get_account(account_id)
        if not account or not account.get("cdp_port"):
            raise ValueError("请填写现有 Chrome 的 CDP 端口")
        with self._lock:
            if account_id in self._checking:
                return False
            self._checking.add(account_id)

        def run():
            try:
                with self._account_lock(account_id):
                    self.db.update_account(
                        account_id, read_session(account["cdp_port"])
                    )
                self._safe_check(account_id)
            except Exception:
                self.db.update_account(
                    account_id,
                    {
                        "status": "login_required",
                        "last_error": "读取 CDP 会话失败，请确认浏览器已登录并开启调试端口",
                    },
                )
            finally:
                with self._lock:
                    self._checking.discard(account_id)

        self._maintenance.submit(run)
        return True

    def _state(self, account, client):
        try:
            return client.account_state(), client
        except UpstreamError as exc:
            if (
                exc.code != "AUTH_REQUIRED"
                or not self.settings.browser_recovery_enabled
                or not account.get("cdp_port")
            ):
                raise
            self.db.update_account(account["id"], read_session(account["cdp_port"]))
            client.close()
            account = self.db.get_account(account["id"], include_secrets=True)
            client = self.client_factory(account, self.settings)
            return client.account_state(), client

    def _store_state(self, account_id, state, *, balance=True):
        changes = {
            k: state[k]
            for k in ("email", "user_id", "team_id", "cookie_header", "cookie_records")
            if k in state
        }
        changes.update(
            status="active",
            plan=state.get("plan", ""),
            last_checked_at=now_ts(),
            last_error="",
        )
        if balance:
            changes.update(
                last_balance=state["available_balance"],
                balance_details=state.get("buckets", {}),
            )
        return self.db.update_account(account_id, changes)

    def check_account(self, account_id):
        with self._account_lock(account_id):
            account = self.db.get_account(account_id, include_secrets=True)
            if not account:
                raise KeyError("account not found")
            if account["active_tasks"]:
                raise ValueError("账号有运行任务，完成后刷新余额以避免重复计算预留额度")
            client = self.client_factory(account, self.settings)
            try:
                state, client = self._state(account, client)
                self._store_state(account_id, state)
                self.db.disable_account_for_low_balance_if_idle(
                    account_id, self.settings.low_balance_disable_threshold
                )
                return self.db.get_account(account_id)
            except UpstreamError as exc:
                self.db.update_account(
                    account_id,
                    {
                        "status": "login_required"
                        if exc.code == "AUTH_REQUIRED"
                        else "network_error",
                        "last_error": str(exc),
                    },
                )
                raise
            finally:
                client.close()

    def _safe_check(self, account_id):
        try:
            self.check_account(account_id)
        except Exception:
            LOGGER.info("Account %s check did not complete", account_id)

    def _maintain(self):
        while not self._stop.wait(self.settings.account_maintenance_interval_seconds):
            accounts = [
                a
                for a in self.db.list_accounts()
                if a["enabled"] and not a["active_tasks"]
            ]
            # Limit each maintenance batch independently from the task queue.
            for start in range(
                0, len(accounts), self.settings.account_maintenance_workers
            ):
                futures = [
                    self._maintenance.submit(self._safe_check, a["id"])
                    for a in accounts[
                        start : start + self.settings.account_maintenance_workers
                    ]
                ]
                for f in futures:
                    if self._stop.is_set():
                        return
                    f.result()

    def create_task(self, payload, *, caller_request=None):
        normal = normalize_generation_request(payload, self.settings)
        with self._lock:
            if (
                self.db.active_task_count()
                >= self.settings.task_workers + self.settings.task_queue_capacity
            ):
                raise UpstreamError("任务队列已满", "QUEUE_FULL", 429)
            task = self.db.create_task(
                "pol_" + uuid.uuid4().hex,
                normal,
                caller_request=caller_request or payload,
            )
            self._schedule(task["id"])
        return task

    def _schedule(self, task_id):
        with self._lock:
            if task_id in self._futures and not self._futures[task_id].done():
                return
            self._futures[task_id] = self._executor.submit(self._run_guarded, task_id)
            self._futures[task_id].add_done_callback(
                lambda future: self._forget_future(task_id, future)
            )

    def _forget_future(self, task_id, future):
        with self._lock:
            if self._futures.get(task_id) is future:
                self._futures.pop(task_id, None)

    def _run_guarded(self, task_id):
        while not self._stop.is_set():
            with self._lock:
                if len(self._running) < self.settings.task_workers:
                    self._running.add(task_id)
                    break
            self._stop.wait(0.2)
        else:
            return
        try:
            self._run_task(task_id)
        finally:
            with self._lock:
                self._running.discard(task_id)

    def _run_task(self, task_id):
        task = self.db.get_task(task_id)
        if not task or task["status"] in TERMINAL:
            return
        account = None
        client = None
        cost = None
        uncertain = False
        released = False
        deadline = time.monotonic() + self.settings.task_timeout_seconds
        record_id = task.get("generation_id")
        payload = task["request"]
        excluded = set()
        audit = task.get("upstream_response") or {}
        try:
            while (
                not account and time.monotonic() < deadline and not self._stop.is_set()
            ):
                account = self.db.acquire_account(
                    preferred_id=task.get("account_id")
                    if record_id
                    else payload.get("account_id"),
                    task_id=task_id,
                    recovering=bool(record_id),
                    exclude_ids=excluded,
                    kind="video",
                )
                if not account:
                    candidates = [
                        a
                        for a in self.db.list_accounts()
                        if a["enabled"]
                        and a["status"] in ("active", "pending")
                        and a["id"] not in excluded
                        and (
                            not payload.get("account_id")
                            or a["id"] == payload["account_id"]
                        )
                    ]
                    if not candidates and not record_id:
                        raise UpstreamError("没有可用账号或余额不足", "NO_ACCOUNT", 503)
                    self._stop.wait(0.5)
                    continue
                client = self.client_factory(account, self.settings)
                if record_id:
                    break
                with self._account_lock(account["id"]):
                    fresh = self.db.get_account(account["id"], include_secrets=True)
                    # Don't refresh balances while other tasks hold reservations.
                    if fresh["active_tasks"] == 1 or fresh["last_balance"] is None:
                        state, client = self._state(fresh, client)
                        self._store_state(account["id"], state)
                    account = self.db.get_account(account["id"], include_secrets=True)
                    client.account = dict(account)
                self.db.update_task(
                    task_id, status="preparing", progress=10, channel="pollo"
                )
                body, quote, cost = client.prepare(payload)
                self.db.update_task(
                    task_id,
                    upstream_request={
                        "submit": {
                            "method": "POST",
                            "path": "/api/trpc/recipe.submit?batch=1",
                            "body": body,
                        }
                    },
                    upstream_response={"quote": quote},
                    estimated_cost=cost,
                    progress=30,
                )
                audit = {"quote": quote}
                if not self.db.reserve_task_balance(task_id, account["id"], cost):
                    excluded.add(account["id"])
                    self.db.release_account(account["id"], task_id=task_id)
                    client.close()
                    client = None
                    account = None
                    continue
                if self._stop.is_set():
                    return
                self.db.update_task(task_id, status="submitting", progress=35)
                generated = client.generate(body)
                record_id = str(generated["id"])
                audit["submit"] = generated
                try:
                    self.db.record_submission(
                        task_id, account["id"], record_id, audit, cost
                    )
                except Exception as exc:
                    LOGGER.exception("Could not persist accepted task %s", task_id)
                    raise SubmissionUnknown() from exc
                audit["budget_debited"] = cost
            if self._stop.is_set():
                return
            if not account or not record_id:
                raise TimeoutError("等待可用账号超时")
            poll_errors = 0
            while time.monotonic() < deadline and not self._stop.is_set():
                try:
                    status = client.status(record_id)
                    poll_errors = 0
                    raw = str(status.get("status") or "")
                    audit["poll"] = status
                    self.db.update_task(
                        task_id,
                        status="running",
                        progress=65 if raw == "processing" else 45,
                        raw_status=status,
                        upstream_response=audit,
                    )
                    if raw in (
                        "succeed",
                        "failed",
                        "fail",
                        "error",
                        "cancelled",
                        "canceled",
                    ):
                        detail = client.detail(record_id)
                        audit["detail"] = detail
                        urls = result_urls(detail)
                        if raw == "succeed" and not urls:
                            self._stop.wait(self.settings.poll_interval_seconds)
                            continue
                        if raw == "succeed":
                            try:
                                audit["downloads"] = client.downloads(detail)
                                urls = [
                                    row["url"] for row in audit["downloads"]
                                ] or urls
                            except Exception:
                                audit["download_warning"] = "DOWNLOAD_LOOKUP_FAILED"
                        charged = (detail.get("generateRecord") or {}).get(
                            "creditDecimal"
                        )
                        cost = (
                            float(charged)
                            if charged is not None
                            else float(self.db.get_task(task_id)["estimated_cost"])
                        )
                        if raw != "succeed":
                            refund = detail.get("refundCreditDecimal")
                            cost = max(cost - float(refund or 0), 0)
                            audit["failure"] = {
                                "stage": "generation",
                                "message": failure_diagnostic(detail)
                                or failure_diagnostic(status),
                            }
                        failure = (
                            public_failure(
                                {
                                    **self.db.get_task(task_id),
                                    "error_code": "GENERATION_FAILED",
                                    "upstream_response": audit,
                                }
                            )
                            if raw != "succeed"
                            else {}
                        )
                        self.db.update_task(
                            task_id,
                            status="succeeded" if raw == "succeed" else "failed",
                            progress=100,
                            result_urls=urls if raw == "succeed" else [],
                            thumbnail_url=detail.get("thumbnail")
                            or detail.get("cover")
                            or "",
                            raw_status=detail,
                            upstream_response=audit,
                            actual_cost=cost,
                            completed_at=now_ts(),
                            error_code="" if raw == "succeed" else "GENERATION_FAILED",
                            error_message=""
                            if raw == "succeed"
                            else failure["message"],
                        )
                        if raw == "succeed":
                            self.db.record_model_cost(task_id, payload, cost)
                        self.db.release_account(account["id"], task_id=task_id)
                        released = True
                        if not self.db.get_account(account["id"])["active_tasks"]:
                            self._safe_check(account["id"])
                        return
                except UpstreamError as exc:
                    if exc.code == "AUTH_REQUIRED":
                        self.db.update_account(
                            account["id"],
                            {"status": "login_required", "last_error": str(exc)},
                        )
                        raise
                    poll_errors += 1
                    audit["poll_error"] = {"code": exc.code, "message": str(exc)}
                    self.db.update_task(task_id, upstream_response=audit)
                self._stop.wait(
                    min(
                        self.settings.poll_interval_seconds * 2 ** min(poll_errors, 4),
                        60,
                        max(deadline - time.monotonic(), 0),
                    )
                )
            if not self._stop.is_set():
                raise TimeoutError("查询超时；上游任务可能仍在运行，可重试查询原任务")
        except Exception as exc:
            uncertain = getattr(exc, "code", "") == "SUBMISSION_UNKNOWN"
            error_code = getattr(
                exc,
                "code",
                "TIMEOUT"
                if isinstance(exc, TimeoutError)
                else "INVALID_REQUEST"
                if isinstance(exc, ValueError)
                else "INTERNAL_ERROR",
            )
            audit["failure"] = {
                "message": str(exc)[:800],
                "stage": getattr(exc, "stage", ""),
            }
            failure = public_failure(
                {
                    **self.db.get_task(task_id),
                    "error_code": error_code,
                    "upstream_response": audit,
                }
            )
            self.db.update_task(
                task_id,
                status="expired" if isinstance(exc, TimeoutError) else "failed",
                error_code=error_code,
                error_message=failure["message"],
                completed_at=now_ts(),
                upstream_response=audit,
            )
            if uncertain and account:
                self.db.update_account(
                    account["id"],
                    {
                        "status": "login_required",
                        "last_error": "提交结果未知，请核对任务与余额",
                    },
                )
            elif (
                isinstance(exc, UpstreamError)
                and exc.code == "AUTH_REQUIRED"
                and account
            ):
                self.db.update_account(
                    account["id"], {"status": "login_required", "last_error": str(exc)}
                )
            if not isinstance(exc, (ValueError, UpstreamError, TimeoutError)):
                LOGGER.exception("Task %s failed", task_id)
        finally:
            if client:
                client.close()
            if account and not released:
                if not self._stop.is_set():
                    self.db.release_account(account["id"], task_id=task_id)
                # On shutdown keep leases/reservations for restart recovery.

    def retry_task(self, task_id):
        task = self.db.get_task(task_id)
        if not task:
            raise KeyError("task not found")
        if task["status"] not in ("failed", "expired"):
            raise ValueError("只能重试失败或超时任务")
        if task.get("error_code") == "SUBMISSION_UNKNOWN":
            raise ValueError("提交结果未知，请先核对上游记录，不能直接重新提交")
        if task.get("generation_id") and task.get("error_code") != "GENERATION_FAILED":
            self.db.update_task(
                task_id,
                status="submitted",
                completed_at=None,
                error_code="",
                error_message="",
            )
            self._schedule(task_id)
            return self.db.get_task(task_id)
        return self.create_task(task["caller_request"])

    def wait_task(self, task_id, timeout=None):
        end = time.monotonic() + (timeout or self.settings.synchronous_timeout_seconds)
        while time.monotonic() < end and not self._stop.is_set():
            task = self.db.get_task(task_id)
            if task["status"] in TERMINAL:
                return task
            self._stop.wait(0.2)
        return self.db.get_task(task_id)

    def public_task(self, task):
        result = {
            "id": task["id"],
            "object": "video.generation",
            "created_at": task["created_at"],
            "model": task["model"],
            "status": task["status"],
            "progress": task["progress"],
            "data": (task.get("upstream_response") or {}).get("downloads")
            or [{"url": url} for url in task["result_urls"]],
            "usage": {
                "estimated_credits": task["estimated_cost"],
                "actual_credits": task["actual_cost"],
            },
        }
        if task["status"] in ("failed", "expired"):
            result["error"] = public_failure(task)
        metadata = (task.get("raw_status") or {}).get("videoMeta")
        if metadata:
            result["metadata"] = metadata
        return result

    def refresh_downloads(self, task_id):
        task = self.db.get_task(task_id)
        if not task:
            raise KeyError("task not found")
        if task["status"] != "succeeded":
            raise UpstreamError("video is not ready", "NOT_READY", 409)
        account = self.db.get_account(task["account_id"], include_secrets=True)
        if not account or not task.get("generation_id"):
            raise ValueError("任务缺少上游账户或记录 ID")
        client = self.client_factory(account, self.settings)
        try:
            detail = client.detail(task["generation_id"])
            downloads = client.downloads(detail)
            if not downloads:
                raise UpstreamError("上游未返回下载地址", "DOWNLOAD_UNAVAILABLE")
            audit = {
                **task.get("upstream_response", {}),
                "detail": detail,
                "downloads": downloads,
            }
            self.db.update_task(
                task_id,
                upstream_response=audit,
                raw_status=detail,
                result_urls=[row["url"] for row in downloads],
            )
            return self.db.get_task(task_id)
        finally:
            client.close()

    def download_url(self, task_id, variant="best", index=0, refresh=False):
        task = self.db.get_task(task_id)
        if not task:
            raise KeyError("task not found")
        if variant not in ("best", "original", "no_watermark", "preview") or index < 0:
            raise ValueError("无效下载选项")
        if task["status"] != "succeeded":
            raise UpstreamError("video is not ready", "NOT_READY", 409)
        rows = (task.get("upstream_response") or {}).get("downloads") or []
        if refresh or (not rows and variant != "best"):
            task = self.refresh_downloads(task_id)
            rows = task["upstream_response"]["downloads"]
        rows = rows or [{"url": url} for url in task.get("result_urls", [])]
        if index >= len(rows):
            raise KeyError("video not found")
        field = {
            "best": "url",
            "original": "original_url",
            "no_watermark": "no_watermark_url",
            "preview": "preview_url",
        }[variant]
        url = rows[index].get(field)
        if not url:
            raise UpstreamError(
                "此下载版本不可用",
                "DOWNLOAD_UNAVAILABLE",
                403 if variant == "no_watermark" else 404,
            )
        return url

    def task_media_source(self, task_id, index):
        task = self.db.get_task(task_id)
        if not task:
            raise KeyError("task not found")
        media = sum(
            [task["request"].get(k, []) for k in ("_images", "_videos", "_audio")], []
        )
        if index < 0 or index >= len(media):
            raise IndexError("media not found")
        return media[index]["value"]

    def status(self):
        return {
            "service": "pol2api",
            "running_tasks": len(self._running),
            "active_tasks": self.db.active_task_count(),
        }
