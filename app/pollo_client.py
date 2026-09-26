from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import math
import mimetypes
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, unquote_to_bytes

from curl_cffi import requests as curl_requests
from PIL import Image
import requests

from app.config import rewrite_loopback_proxy
from app.agent import agent_files, agent_prompt, agent_video_detail
from app.recipes import input_values, media_rules
from app.task_errors import classify_failure


class UpstreamError(RuntimeError):
    def __init__(self, message, code="UPSTREAM_ERROR", status_code=502, *, stage=""):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.stage = stage


class SubmissionUnknown(UpstreamError):
    def __init__(self):
        super().__init__(
            "提交结果未知；请先核对上游记录，系统不会自动重复提交", "SUBMISSION_UNKNOWN"
        )


def public_media_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("素材 URL 无效")
    addresses = socket.getaddrinfo(
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    if any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("素材 URL 必须指向公共网络地址")


class PolloClient:
    def __init__(self, account, settings):
        self.account = dict(account)
        self.settings = settings
        self.base = settings.upstream_base_url.rstrip("/")
        self.proxy = rewrite_loopback_proxy(
            account.get("proxy_url", ""), settings.proxy_host_override
        )
        self.session = curl_requests.Session(impersonate="chrome")
        domain = urlsplit(self.base).hostname
        records = account.get("cookie_records") or []
        if isinstance(records, str):
            records = json.loads(records)
        cookies = {
            x["name"]: x["value"]
            for x in records
            if isinstance(x, dict) and x.get("name")
        }
        for part in (
            account.get("cookie_header") or account.get("cookies") or ""
        ).split(";"):
            key, sep, value = part.strip().partition("=")
            if sep:
                cookies[key] = value
        for name, value in cookies.items():
            self.session.cookies.set(name, value, domain=domain, path="/")

    def close(self):
        self.session.close()

    def session_context(self):
        records = [
            dict(name=c.name, value=c.value, domain=c.domain, path=c.path)
            for c in self.session.cookies.jar
        ]
        return dict(
            cookie_records=records,
            cookie_header="; ".join(f"{x['name']}={x['value']}" for x in records),
        )

    def login_password(self, email: str, password: str):
        """Exchange Pollo email credentials for a NextAuth session."""
        # Observed in Pollo's browser sigV1 implementation (2026-09-18).
        signature_seed = hashlib.md5(
            b"nvKTEonFAll-in-One AI Writing CopilotwNLf2plwvtlcCxam"
        ).hexdigest()
        device_number = hashlib.sha256(
            (signature_seed + password + "xJ7fTJBgQ55/9r|").encode()
        ).hexdigest()
        # Keep browser clearance and device cookies. Clearing the whole jar can
        # make an otherwise valid browser account fail the credentials callback.
        for cookie in list(self.session.cookies.jar):
            if cookie.name.startswith("__Secure-next-auth.session-token"):
                self.session.cookies.delete(
                    cookie.name, domain=cookie.domain, path=cookie.path
                )
        request_args = dict(
            proxy=self.proxy or None,
            timeout=self.settings.request_timeout_seconds,
            allow_redirects=False,
        )
        headers = {
            "Accept": "application/json",
            "Origin": self.base,
            "Referer": self.base + "/login",
        }
        if self.account.get("user_agent"):
            headers["User-Agent"] = self.account["user_agent"]
        try:
            csrf = self.session.get(
                self.base + "/api/auth/csrf", headers=headers, **request_args
            )
            if csrf.status_code != 200:
                raise UpstreamError("登录需要浏览器验证", "CHALLENGE_REQUIRED")
            try:
                csrf_data = csrf.json()
            except ValueError as exc:
                raise UpstreamError("登录需要浏览器验证", "CHALLENGE_REQUIRED") from exc
            csrf_token = csrf_data.get("csrfToken") if isinstance(csrf_data, dict) else None
            if not isinstance(csrf_token, str) or not csrf_token:
                raise UpstreamError("登录需要浏览器验证", "CHALLENGE_REQUIRED")
            response = self.session.post(
                self.base + "/api/auth/callback/system-user",
                data={
                    "email": email,
                    "password": password,
                    "deviceNumber": device_number,
                    "version": "v1",
                    "redirect": "false",
                    "csrfToken": csrf_token,
                    "callbackUrl": self.base + "/",
                    "json": "true",
                },
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                **request_args,
            )
            if response.status_code == 429:
                raise UpstreamError("密码登录请求过于频繁（回调 HTTP 429）", "RATE_LIMITED")
            if response.status_code != 200:
                status = int(response.status_code)
                challenge = response.headers.get("cf-mitigated") == "challenge"
                if challenge or status == 403:
                    raise UpstreamError(
                        f"密码登录需要浏览器验证或检查代理（回调 HTTP {status}）",
                        "CHALLENGE_REQUIRED",
                    )
                if status == 401:
                    raise UpstreamError(
                        "Pollo 拒绝邮箱或密码（回调 HTTP 401）", "LOGIN_FAILED"
                    )
                if status >= 500:
                    raise UpstreamError(
                        f"Pollo 登录服务暂不可用（回调 HTTP {status}）",
                        "UPSTREAM_HTTP_ERROR",
                    )
                raise UpstreamError(
                    f"Pollo 登录回调被拒绝（HTTP {status}）", "LOGIN_FAILED"
                )
            try:
                callback = response.json()
            except ValueError as exc:
                raise UpstreamError("密码登录需要浏览器验证", "CHALLENGE_REQUIRED") from exc
            if not isinstance(callback, dict):
                raise UpstreamError("密码登录响应无效", "LOGIN_FAILED")
            callback_url = str(callback.get("url") or "")
            if "error=" in callback_url:
                raise UpstreamError("邮箱或密码错误，或账号需要验证", "LOGIN_FAILED")
            session = self.session.get(
                self.base + "/api/auth/session", headers=headers, **request_args
            )
            session_data = session.json() if session.status_code == 200 else {}
            if (
                not isinstance(session_data, dict)
                or not (session_data.get("user") or {}).get("id")
            ):
                raise UpstreamError("密码登录未建立会话", "LOGIN_FAILED")
        except UpstreamError:
            raise
        except Exception as exc:
            raise UpstreamError("密码登录网络请求失败", "NETWORK_ERROR") from exc
        return self.session_context()

    def _request(self, method, path, *, payload=None, params=None):
        headers = {
            "Accept": "application/json",
            "Origin": self.base,
            "Referer": self.base + "/create",
            "x-recipe-protocol": "1",
            "user-language": "en",
        }
        if self.account.get("user_agent"):
            headers["User-Agent"] = self.account["user_agent"]
        attempts = self.settings.request_retries + 1 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    self.base + path,
                    json=payload,
                    params=params,
                    headers=headers,
                    proxy=self.proxy or None,
                    timeout=self.settings.request_timeout_seconds,
                    allow_redirects=False,
                )
            except Exception as exc:
                if attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 5))
                    continue
                raise UpstreamError("Pollo 网络请求失败", "NETWORK_ERROR") from exc
            try:
                response_body = response.json()
            except Exception:
                response_body = None
            stage = "upload_sign" if path == "/api/upload/sign" else ""
            item = (
                response_body[0]
                if isinstance(response_body, list) and response_body
                else response_body
            )
            # tRPC business errors can arrive with HTTP 400/403/422. Decode them
            # before deciding that a valid account needs to log in again.
            if (
                path.startswith("/api/trpc/")
                and response.status_code < 500
                and isinstance(item, dict)
                and item.get("error")
            ):
                return response_body
            if stage and isinstance(item, dict) and not item.get("sign"):
                error = item.get("error") or item
                message = error.get("message") if isinstance(error, dict) else error
                code = (
                    str(error.get("code") or "UPLOAD_REJECTED")
                    if isinstance(error, dict)
                    else "UPLOAD_REJECTED"
                )
                raise self._business_error(
                    str(message or "上传签名请求被拒绝"),
                    code,
                    response.status_code if response.status_code >= 400 else 422,
                    stage,
                )
            if response.status_code == 401 or (
                response.status_code == 403 and not stage
            ):
                raise UpstreamError(
                    "Pollo 会话失效或需要浏览器验证", "AUTH_REQUIRED", 401
                )
            if stage and response.status_code >= 300:
                raise UpstreamError(
                    "上传签名请求被拒绝",
                    "UPLOAD_REJECTED",
                    response.status_code,
                    stage=stage,
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 5))
                    continue
                raise UpstreamError(
                    f"Pollo HTTP {response.status_code}",
                    "RATE_LIMITED"
                    if response.status_code == 429
                    else "UPSTREAM_HTTP_ERROR",
                    response.status_code,
                )
            if response.status_code >= 300:
                raise UpstreamError(
                    f"Pollo HTTP {response.status_code}",
                    "UPSTREAM_HTTP_ERROR",
                    response.status_code,
                )
            if response_body is None:
                raise UpstreamError(
                    "Pollo 返回非 JSON 响应", "INVALID_RESPONSE", stage=stage
                )
            return response_body
        raise UpstreamError("Pollo 请求失败")

    @staticmethod
    def _business_error(message, code, status, stage=""):
        category = classify_failure(code, message, stage=stage)
        if (
            status == 401
            or code == "UNAUTHORIZED"
            or (code == "FORBIDDEN" and not stage and category == "GENERATION_FAILED")
        ):
            return UpstreamError("Pollo 会话失效或需要验证", "AUTH_REQUIRED", 401)
        return UpstreamError(message, code, status, stage=stage)

    def rpc(self, name, value=None, *, mutation=False):
        data = {"0": {"json": value}}
        if mutation:
            body = self._request(
                "POST", "/api/trpc/" + name, payload=data, params={"batch": "1"}
            )
        else:
            body = self._request(
                "GET",
                "/api/trpc/" + name,
                params={
                    "batch": "1",
                    "input": json.dumps(
                        data, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            )
        item = body[0] if isinstance(body, list) and body else body
        if not isinstance(item, dict):
            raise UpstreamError("无效 tRPC 响应", "INVALID_RESPONSE")
        if "error" in item:
            error = item["error"]
            if not isinstance(error, dict):
                raise UpstreamError("无效 tRPC 错误响应", "INVALID_RESPONSE")
            err = error.get("json", error)
            if not isinstance(err, dict):
                raise UpstreamError("无效 tRPC 错误响应", "INVALID_RESPONSE")
            details = err.get("data") or {}
            code = str(details.get("code") or err.get("code") or "TRPC_ERROR")
            message = str(err.get("message") or "Pollo 请求被拒绝")[:500]
            raise self._business_error(
                message,
                code,
                int(details.get("httpStatus") or 422),
                "upload_complete"
                if name == "uploadAsset.complete"
                else "submit"
                if name == "recipe.submit"
                else "",
            )
        if "result" not in item or "data" not in item["result"]:
            raise UpstreamError("无效 tRPC 数据", "INVALID_RESPONSE")
        result = item["result"]["data"]
        return (
            result.get("json")
            if isinstance(result, dict) and "json" in result
            else result
        )

    def account_state(self):
        session = self._request("GET", "/api/auth/session")
        user = session.get("user") or {}
        if not user.get("id"):
            raise UpstreamError("请导入已登录的 Pollo 会话", "AUTH_REQUIRED", 401)
        usage = self.rpc("subUsage.getSubUsage", {"appName": "Pollo"})
        rows = [
            r for r in usage.get("usageList", []) if r.get("usageType") == "credits"
        ]
        balance = sum(
            max(float(r.get("totalCount") or 0) - float(r.get("useCount") or 0), 0)
            for r in rows
        )
        project = self.account.get("team_id") or self.account.get("cognito_sub")
        if not project:
            projects = self.rpc("userProject.getListV2", {"pageSize": 20})
            items = projects.get("items") or []
            if items:
                project = items[0].get("id") or items[0].get("projectId")
        return dict(
            available_balance=balance,
            buckets=usage,
            plan="paid" if usage.get("isActiveSub") else "free",
            user_id=str(user["id"]),
            email=user.get("email") or "",
            team_id=project or "",
            **self.session_context(),
        )

    def manifest(self, payload):
        return self.rpc(
            "recipe.manifest",
            {"recipeCode": payload["_recipe"], "model": payload["upstream_model"]},
        )

    def download(self, value, limit):
        if value.startswith("data:"):
            head, _, raw = value.partition(",")
            if len(raw) > limit * 4 // 3 + 1024:
                raise ValueError("素材过大")
            data = (
                base64.b64decode(raw, validate=True)
                if ";base64" in head
                else unquote_to_bytes(raw)
            )
            mime = head[5:].split(";")[0]
        else:
            attempts = min(max(self.settings.request_retries, 0), 2) + 1
            for attempt in range(attempts):
                try:
                    data, mime = self._download_http(value, limit)
                    break
                except (requests.RequestException, OSError) as exc:
                    status = getattr(
                        getattr(exc, "response", None), "status_code", None
                    )
                    transient = status is None or status in (
                        408,
                        429,
                        500,
                        502,
                        503,
                        504,
                    )
                    if not transient or attempt + 1 == attempts:
                        raise UpstreamError(
                            "素材下载失败", "MEDIA_DOWNLOAD_FAILED", 422
                        ) from exc
                    time.sleep(min(2**attempt, 2))
        if not data or len(data) > limit:
            raise ValueError("素材为空或过大")
        return data, mime

    def _download_http(self, url, limit):
        # Retry only this read-only step, starting with an empty buffer each time.
        # Never pass account cookies to caller-provided media URLs.
        with requests.Session() as session:
            session.trust_env = False
            try:
                for _ in range(6):
                    public_media_url(url)
                    with session.get(
                        url,
                        stream=True,
                        allow_redirects=False,
                        timeout=self.settings.media_timeout_seconds,
                        proxies={"http": self.proxy, "https": self.proxy}
                        if self.proxy
                        else {},
                    ) as response:
                        if response.is_redirect:
                            from urllib.parse import urljoin

                            url = urljoin(url, response.headers["Location"])
                            continue
                        response.raise_for_status()
                        chunks = []
                        size = 0
                        for chunk in response.iter_content(65536):
                            size += len(chunk)
                            if size > limit:
                                raise ValueError("素材超过模型允许大小")
                            chunks.append(chunk)
                        data = b"".join(chunks)
                        mime = response.headers.get("Content-Type", "").split(";")[0]
                        if not data:
                            raise UpstreamError(
                                "素材下载内容为空", "MEDIA_DOWNLOAD_FAILED", 422
                            )
                        return data, mime
            except ValueError as exc:
                if str(exc).startswith("素材 URL"):
                    raise UpstreamError(
                        "素材链接无法下载", "MEDIA_DOWNLOAD_FAILED", 422
                    ) from exc
                raise
        raise UpstreamError("素材重定向次数过多", "MEDIA_DOWNLOAD_FAILED", 422)

    def upload_media(self, source, kind, rule):
        data, mime = self.download(
            source["value"],
            min(
                self.settings.media_max_bytes,
                rule.get("maxFilesize", self.settings.media_max_bytes),
            ),
        )
        metadata = {"size": len(data)}
        if kind == "image":
            try:
                with Image.open(io.BytesIO(data)) as im:
                    im.verify()
                with Image.open(io.BytesIO(data)) as im:
                    metadata.update(width=im.width, height=im.height)
                    mime = Image.MIME.get(im.format, mime)
            except (OSError, SyntaxError, ValueError) as exc:
                raise UpstreamError(
                    "图片格式无法解析", "MEDIA_FORMAT_UNSUPPORTED", 422
                ) from exc
        else:
            with tempfile.TemporaryDirectory(prefix="pol-media-") as folder:
                path = Path(folder) / "media"
                path.write_bytes(data)
                try:
                    probe = subprocess.run(
                        [
                            self.settings.ffprobe_executable,
                            "-v",
                            "error",
                            "-show_streams",
                            "-show_format",
                            "-of",
                            "json",
                            str(path),
                        ],
                        capture_output=True,
                        timeout=30,
                        check=True,
                    )
                    info = json.loads(probe.stdout)
                except Exception as exc:
                    raise ValueError(
                        "无法解析音视频；请检查媒体格式及 ffprobe 安装"
                    ) from exc
            stream = next(
                (s for s in info.get("streams", []) if s.get("codec_type") == kind),
                None,
            )
            if not stream:
                raise ValueError(f"文件中没有 {kind} 流")
            duration = float(
                info.get("format", {}).get("duration") or stream.get("duration") or 0
            )
            if not math.isfinite(duration) or not rule.get(
                "minDuration", 0
            ) <= duration <= rule.get("maxDuration", float("inf")):
                raise ValueError("参考素材时长超出模型范围")
            metadata["duration"] = duration
            if kind == "video":
                metadata.update(
                    width=int(stream["width"]), height=int(stream["height"])
                )
                num, _, den = str(stream.get("avg_frame_rate", "0/1")).partition("/")
                fps = float(num) / float(den or 1) if float(den or 1) else 0
                if fps and not rule.get("minFps", 0) <= fps <= rule.get(
                    "maxFps", float("inf")
                ):
                    raise ValueError("参考视频帧率超出模型范围")
        if "width" in metadata:
            width, height = metadata["width"], metadata["height"]
            for name, sign in (("minSize", -1), ("maxSize", 1)):
                if name in rule:
                    w, h = map(int, rule[name].split("*"))
                    if (
                        (width < w or height < h)
                        if sign < 0
                        else (width > w or height > h)
                    ):
                        raise ValueError("参考素材尺寸超出模型范围")
            ratio = width / height
            if (
                not rule.get("minRatio", 0)
                <= ratio
                <= rule.get("maxRatio", float("inf"))
            ):
                raise ValueError("参考素材画幅超出模型范围")
            if (
                not rule.get("minTotalPixels", 0)
                <= width * height
                <= rule.get("maxTotalPixels", float("inf"))
            ):
                raise ValueError("参考视频像素总数超出模型范围")
        ext = mimetypes.guess_extension(mime) or (
            ".mp4" if kind == "video" else ".mp3" if kind == "audio" else ".png"
        )
        if ext == ".jpe":
            ext = ".jpg"
        filename = source["label"] + ext
        signed = self._request(
            "POST", "/api/upload/sign", payload={"filename": filename, "type": kind}
        )
        if (
            not isinstance(signed, dict)
            or not isinstance(signed.get("sign"), str)
            or not signed.get("accessURL")
        ):
            raise UpstreamError(
                "上传签名响应无效", "UPLOAD_FAILED", stage="upload_sign"
            )
        destination = urlsplit(signed.get("sign", ""))
        if destination.scheme != "https" or not (destination.hostname or "").endswith(
            ".r2.cloudflarestorage.com"
        ):
            raise UpstreamError(
                "上传签名目标无效", "UPLOAD_FAILED", stage="upload_sign"
            )
        # Separate session: never forward account cookies to storage or caller media hosts.
        with requests.Session() as upload_session:
            upload_session.trust_env = False
            try:
                response = upload_session.put(
                    signed["sign"],
                    data=data,
                    headers={"Content-Type": mime or "application/octet-stream"},
                    timeout=self.settings.media_timeout_seconds,
                    allow_redirects=False,
                    proxies={"http": self.proxy, "https": self.proxy}
                    if self.proxy
                    else {},
                )
            except requests.RequestException as exc:
                raise UpstreamError(
                    "素材上传请求失败", "UPLOAD_FAILED", stage="upload_storage"
                ) from exc
            if not 200 <= response.status_code < 300:
                raise UpstreamError(
                    "素材上传被拒绝",
                    "UPLOAD_REJECTED",
                    response.status_code,
                    stage="upload_storage",
                )
        completed = self.rpc(
            "uploadAsset.complete",
            {"accessURL": signed["accessURL"], "fileName": filename},
            mutation=True,
        )
        return {
            "type": kind,
            "name": source["label"],
            kind: completed.get("url") or signed["accessURL"],
            "metadata": metadata,
        }

    def prepare(self, payload):
        manifest = self.manifest(payload)
        props = manifest["schema"]["properties"]
        user_input = input_values(payload, manifest)
        rules = media_rules(manifest, payload["_recipe"])
        total_refs = sum(
            len(payload.get(field, [])) for field in ("_images", "_videos", "_audio")
        )
        max_refs = props.get("refs", {}).get(
            "maxItems", rules.get("maxItems", float("inf"))
        )
        if total_refs > max_refs:
            raise ValueError("实时素材总数量限制已变化")
        refs = []
        for kind, field in (
            ("image", "_images"),
            ("video", "_videos"),
            ("audio", "_audio"),
        ):
            limits = rules["upload"].get(kind, {})
            items = payload.get(field, [])
            if len(items) > limits.get("maxNum", 0):
                raise ValueError("实时素材数量限制已变化")
            for source in items:
                upload_rule = limits
                if payload["_recipe"] == "multi2video":
                    frame = "image" if not refs else "imageTail"
                    upload_rule = props[frame]["x-ui-config"]["upload"]["image"]
                ref = self.upload_media(source, kind, upload_rule)
                ref["order"] = len(refs) + 1
                refs.append(ref)
            subset = [r for r in refs if r["type"] == kind]
            if sum(r["metadata"].get("duration", 0) for r in subset) > limits.get(
                "maxTotalDuration", float("inf")
            ):
                raise ValueError("参考素材总时长超出限制")
            if sum(r["metadata"].get("size", 0) for r in subset) > limits.get(
                "maxTotalFilesize", float("inf")
            ):
                raise ValueError("参考素材总大小超出限制")
        if payload["_recipe"] == "ref2video":
            user_input["refs"] = refs
        else:
            for frame, ref in zip(("image", "imageTail"), refs):
                user_input[frame] = ref["image"]
        body = {
            "recipeCode": payload["_recipe"],
            "modelKey": payload["upstream_model"],
            "userInput": user_input,
            "numOutputs": payload["n"],
            "entitlement": {"unlimited": False},
        }
        quote = self.rpc("recipe.estimateTask", body, mutation=True)
        cost = quote.get("discountCost", quote.get("cost"))
        if cost is None or not math.isfinite(float(cost)) or float(cost) < 0:
            raise UpstreamError("上游未返回有效报价")
        project = self.account.get("team_id") or self.account.get("cognito_sub")
        if not project:
            raise UpstreamError(
                "账户没有可用项目，请在 Pollo 创建项目后刷新账户",
                "PROJECT_REQUIRED",
                422,
            )
        body["projectId"] = project
        return body, quote, float(cost)

    def generate(self, body):
        try:
            result = self.rpc("recipe.submit", body, mutation=True)
        except UpstreamError as exc:
            if (
                exc.status_code >= 500
                or exc.status_code in (408, 425)
                or exc.code
                in (
                    "NETWORK_ERROR",
                    "INVALID_RESPONSE",
                )
            ):
                raise SubmissionUnknown() from exc
            raise
        except Exception as exc:
            # A malformed response can follow an accepted POST. Never replay it.
            raise SubmissionUnknown() from exc
        if not isinstance(result, dict) or not result.get("id"):
            raise SubmissionUnknown()
        return result

    def _agent_data(self, method, path, *, payload=None):
        response = self._request(method, path, payload=payload)
        if not isinstance(response, dict) or response.get("code") != 200:
            message = response.get("msg") if isinstance(response, dict) else None
            raise UpstreamError(str(message or "Agent 响应无效"), "AGENT_ERROR")
        data = response.get("data")
        if not isinstance(data, dict):
            raise UpstreamError("Agent 响应缺少数据", "INVALID_RESPONSE")
        return data

    def generate_agent(self, body, payload):
        project = body.get("projectId")
        if not project:
            raise UpstreamError("账号没有可用项目", "PROJECT_REQUIRED", 422)
        created = self._agent_data(
            "POST",
            "/api/agent-gateway/agent/v1/threads",
            payload={
                "metadata": {
                    "idempotency_key": str(uuid.uuid4()),
                    "project_id": project,
                }
            },
        )
        thread_id = created.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise UpstreamError("Agent 未返回会话 ID", "INVALID_RESPONSE")
        message_id = str(uuid.uuid4())
        request = {
            "input": {
                "messages": [
                    {
                        "id": message_id,
                        "type": "human",
                        "content": agent_prompt(payload),
                        "additional_kwargs": {
                            "startTime": int(time.time() * 1000),
                            "files": agent_files(body),
                        },
                    }
                ]
            },
            "config": {"configurable": {"mode": "fast", "plan_mode": "autopilot"}},
            "metadata": {"idempotency_key": message_id, "project_id": project},
            "stream_mode": ["messages-tuple", "values", "custom"],
            "stream_subgraphs": True,
            "stream_resumable": True,
            "assistant_id": "lead_agent",
            "on_disconnect": "continue",
        }
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Origin": self.base,
            "Referer": self.base + "/agent",
        }
        if self.account.get("user_agent"):
            headers["User-Agent"] = self.account["user_agent"]
        try:
            response = self.session.post(
                self.base
                + "/api/agent-gateway/agent/v1/threads/"
                + thread_id
                + "/runs/stream",
                json=request,
                headers=headers,
                proxy=self.proxy or None,
                timeout=self.settings.request_timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
        except Exception as exc:
            # A timed out POST may already be running. Never replay it.
            raise SubmissionUnknown() from exc
        try:
            if response.status_code in (401, 403):
                raise UpstreamError("Agent 会话失效或需要浏览器验证", "AUTH_REQUIRED", 401)
            if response.status_code == 429:
                raise UpstreamError("Agent 请求过于频繁", "RATE_LIMITED", 429)
            if response.status_code != 200:
                raise UpstreamError(
                    f"Agent 提交被拒绝（HTTP {response.status_code}）",
                    "UPSTREAM_HTTP_ERROR",
                    response.status_code,
                    stage="submit",
                )
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raise SubmissionUnknown()
        finally:
            response.close()
        return {"id": thread_id}

    def agent_status(self, thread_id, payload):
        data = self._agent_data(
            "POST",
            "/api/agent-gateway/agent/v1/threads/status",
            payload={"thread_ids": [thread_id]},
        )
        thread = next(
            (item for item in data.get("threads") or [] if item.get("thread_id") == thread_id),
            None,
        )
        if not thread:
            raise UpstreamError("Agent 任务未找到", "TASK_NOT_FOUND")
        state = thread.get("status")
        if state == "idle":
            status = self.agent_detail(thread_id, payload)["status"]
        elif state == "interrupted":
            interrupt = (thread.get("metadata") or {}).get("interrupt") or {}
            status = "processing" if interrupt.get("class") == "background" else "failed"
        elif state in {"busy", "pending", "running"}:
            status = "processing"
        else:
            status = "failed"
        return {"id": thread_id, "status": status, "threadStatus": state}

    def agent_detail(self, thread_id, payload):
        base = "/api/agent-gateway/agent/v1/threads/" + thread_id
        artifacts = self._agent_data("POST", base + "/artifacts", payload={})
        conversation = self._agent_data(
            "POST", base + "/conversation", payload={"limit": 100}
        )
        return agent_video_detail(
            artifacts, conversation.get("messages") or [], payload
        )

    def status(self, record_id):
        records = self.rpc(
            "generationPolling.fetchRecordsStatus", {"recordIds": [int(record_id)]}
        )
        if not isinstance(records, list) or not records:
            raise UpstreamError("上游任务暂未找到", "TASK_NOT_FOUND")
        state = next((r for r in records if str(r.get("id")) == str(record_id)), None)
        if state is None:
            raise UpstreamError("上游任务未匹配", "TASK_NOT_FOUND")
        return state

    def detail(self, record_id):
        return self.rpc("generation.queryRecordDetail", {"id": int(record_id)})

    def downloads(self, detail):
        results = []
        for item in video_outputs(detail):
            value = dict(item)
            warning = ""
            if value.get("videoId") and not value.get("videoUrlNoWatermark"):
                try:
                    data = self.rpc(
                        "video.getVideoNoWatermarkUrl",
                        {"videoId": str(value["videoId"])},
                        mutation=True,
                    )
                    value["videoUrlNoWatermark"] = data.get("videoUrlNoWatermark")
                except Exception as exc:
                    # Download entitlement/transport failures never change generation outcome.
                    warning = getattr(exc, "code", "DOWNLOAD_LOOKUP_FAILED")
            urls = result_urls(value)
            if not urls:
                continue
            clean = https_url(value.get("videoUrlNoWatermark"))
            original = https_url(value.get("mediaUrl"))
            row = dict(
                url=urls[0],
                preview_url=https_url(value.get("videoUrl") or value.get("previewUrl")),
                original_url=original,
                no_watermark_url=clean,
                watermark_verified=bool(clean),
                source="official_download"
                if clean
                else "original"
                if original
                else "preview",
                metadata=value.get("videoMeta") or {},
            )
            if warning:
                row["download_warning"] = warning
            results.append(row)
        return results


def https_url(value):
    return value if isinstance(value, str) and value.startswith("https://") else ""


def video_outputs(detail):
    items = detail.get("generations") or [detail]
    return [
        {**detail, **item, "generations": []}
        if item.get("videoId") and item.get("videoId") == detail.get("videoId")
        else {**item, "generations": []}
        for item in items
    ]


def result_urls(detail):
    outputs = video_outputs(detail)
    return list(
        dict.fromkeys(
            url
            for item in outputs
            if (
                url := next(
                    (
                        https_url(item.get(field))
                        for field in (
                            "videoUrlNoWatermark",
                            "mediaUrl",
                            "videoUrl",
                            "previewUrl",
                        )
                        if https_url(item.get(field))
                    ),
                    "",
                )
            )
        )
    )
