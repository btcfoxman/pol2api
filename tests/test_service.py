import pytest
from app.config import Settings
from app.db import Database
from app.service import PolService
from app.pollo_client import SubmissionUnknown


class FakeClient:
    submits = 0
    balance = 14
    ambiguous = False

    def downloads(self, detail):
        return [{"url": "https://example.com/result.mp4"}]

    def __init__(self, account, settings):
        self.account = account

    def close(self):
        pass

    def account_state(self):
        return {
            "available_balance": self.balance,
            "buckets": {},
            "team_id": "project",
            "plan": "free",
        }

    def prepare(self, payload):
        return (
            {"modelKey": payload["upstream_model"]},
            {"cost": 31, "discountCost": 12},
            12,
        )

    def generate(self, body):
        type(self).submits += 1
        if self.ambiguous:
            raise SubmissionUnknown()
        type(self).balance -= 12
        return {"id": 123, "status": "waiting"}

    def status(self, rid):
        return {"id": int(rid), "status": "succeed"}

    def detail(self, rid):
        return {
            "status": "succeed",
            "generateRecord": {"creditDecimal": "12"},
            "generations": [{"videoUrl": "https://example.com/result.mp4"}],
            "videoMeta": {"width": 864, "height": 496, "duration": 4.096},
        }


@pytest.fixture
def setup(tmp_path):
    FakeClient.submits = 0
    FakeClient.balance = 14
    FakeClient.ambiguous = False
    settings = Settings(
        database_path=str(tmp_path / "db.sqlite"),
        poll_interval_seconds=2,
        account_maintenance_interval_seconds=86400,
    )
    db = Database(settings.database_path, 1)
    account = db.upsert_account(
        {
            "name": "fixture",
            "cookie_header": "test=fake",
            "last_balance": 14,
            "team_id": "project",
            "status": "active",
        }
    )
    service = PolService(db, settings, FakeClient)
    yield db, service, account
    service.stop()


def request():
    return {
        "model": "sd-2-0-mini",
        "prompt": "图1",
        "image_urls": ["https://example.com/a.png"],
        "resolution": "480p",
    }


def test_job_lifecycle_balance_and_public_view(setup):
    db, s, a = setup
    task = s.create_task(request())
    done = s.wait_task(task["id"], 5)
    assert done["status"] == "succeeded"
    assert done["actual_cost"] == 12 and done["generation_id"] == "123"
    assert db.get_account(a["id"])["last_balance"] == 2
    assert db.get_account(a["id"])["reserved_balance"] == 0
    assert FakeClient.submits == 1
    public = s.public_task(done)
    assert public["data"][0]["url"].endswith("result.mp4")
    assert "upstream_request" not in public and "account_id" not in public


def test_no_submission_when_quote_exceeds_balance(setup):
    db, s, a = setup
    FakeClient.balance = 2
    task = s.create_task(request())
    done = s.wait_task(task["id"], 5)
    assert done["status"] == "failed" and done["error_code"] == "NO_ACCOUNT"
    assert FakeClient.submits == 0
    assert db.get_account(a["id"])["active_tasks"] == 0


def test_account_generation_restriction_pauses_new_work_and_keeps_balance_evidence(
    setup, monkeypatch
):
    from app.pollo_client import UpstreamError

    db, service, account = setup

    def rejected(*args):
        FakeClient.submits += 1
        raise UpstreamError(
            "Generation is currently restricted for your account. Please contact support if you believe this is a mistake.",
            "TOO_MANY_REQUESTS",
            429,
            stage="submit",
        )

    monkeypatch.setattr(FakeClient, "generate", rejected)
    first = service.create_task(request())
    done = service.wait_task(first["id"], 5)
    future = service._futures.get(first["id"])
    if future is not None:
        future.result(timeout=5)
    saved = db.get_account(account["id"])
    assert saved["enabled"] is False and saved["status"] == "generation_restricted"
    assert saved["reserved_balance"] == 0 and saved["last_balance"] == 14
    assert (
        done["generation_id"] == ""
        and done["error_message"] == "上游维护中，请稍后再试~"
    )
    service.check_account(account["id"])
    assert db.get_account(account["id"])["status"] == "generation_restricted"
    second = service.create_task(request())
    assert (
        service.wait_task(second["id"], 5)["error_message"] == "上游维护中，请稍后再试~"
    )
    assert FakeClient.submits == 1


def test_already_preparing_task_does_not_submit_after_account_restricted(
    setup, monkeypatch
):
    db, service, account = setup
    original_prepare = FakeClient.prepare

    def prepare(client, payload):
        db.update_account(
            account["id"], {"enabled": False, "status": "generation_restricted"}
        )
        return original_prepare(client, payload)

    monkeypatch.setattr(FakeClient, "prepare", prepare)
    job = service.create_task(request())
    done = service.wait_task(job["id"], 5)
    assert done["error_code"] == "ACCOUNT_RESTRICTED" and FakeClient.submits == 0


def test_ambiguous_submit_blocks_retry(setup):
    db, s, a = setup
    FakeClient.ambiguous = True
    task = s.create_task(request())
    done = s.wait_task(task["id"], 5)
    assert done["error_code"] == "SUBMISSION_UNKNOWN" and FakeClient.submits == 1
    with pytest.raises(ValueError):
        s.retry_task(task["id"])
    assert db.get_account(a["id"])["status"] == "login_required"


def test_upload_rejection_returns_typed_error_and_leaves_account_usable(
    setup, monkeypatch
):
    from app.pollo_client import UpstreamError

    db, service, account = setup

    def rejected(*args):
        raise UpstreamError("Forbidden", "UPLOAD_REJECTED", 403, stage="upload_sign")

    monkeypatch.setattr(FakeClient, "prepare", rejected)
    job = service.create_task(request())
    done = service.wait_task(job["id"], 5)
    assert done["error_message"] == "上游维护中，请稍后再试~"
    assert service.public_task(done)["error"]["category"] == "UPSTREAM_MAINTENANCE"
    assert service.public_task(done)["error"]["outcome"] == "rejected"
    assert service.public_task(done)["error"]["refunded"] is False
    assert FakeClient.submits == 0
    saved = db.get_account(account["id"])
    assert saved["status"] == "active" and saved["reserved_balance"] == 0
    assert saved["active_tasks"] == 0 and saved["last_balance"] == 14


def test_restart_resumes_record_and_does_not_debit_twice(setup):
    db, s, a = setup
    from app.model_catalog import normalize_generation_request

    task = db.create_task(
        "existing", normalize_generation_request(request(), s.settings)
    )
    db.acquire_account(task_id=task["id"])
    db.reserve_task_balance(task["id"], a["id"], 12)
    db.record_submission(task["id"], a["id"], "123", {"submit": {"id": 123}}, 12)
    db.record_submission(task["id"], a["id"], "123", {}, 12)
    assert db.get_account(a["id"])["last_balance"] == 2
    FakeClient.balance = 2
    s.start()
    done = s.wait_task("existing", 5)
    assert done["status"] == "succeeded" and FakeClient.submits == 0
    assert db.get_account(a["id"])["last_balance"] == 2


def test_runtime_settings_persist_and_validate(setup):
    db, s, a = setup
    s.update_runtime_settings(
        {"task_workers": 2, "model_map": '{"custom":"seedance-2-5"}'}
    )
    assert db.get_setting("task_workers") == "2"
    with pytest.raises(ValueError):
        s.update_runtime_settings({"task_workers": 0})
    with pytest.raises(ValueError):
        s.update_runtime_settings({"model_map": '{"bad":"unknown"}'})
    assert s.settings.task_workers == 2


def test_restart_restores_decimal_boolean_and_empty_settings(setup):
    db, s, a = setup
    s.update_runtime_settings(
        {
            "low_balance_disable_threshold": 1.5,
            "proxy_pool_enabled": False,
            "proxy_pool": "",
        }
    )
    restarted = PolService(
        db,
        Settings(proxy_pool="socks5://example.invalid:1080", proxy_pool_enabled=True),
        FakeClient,
    )
    try:
        assert restarted.settings.low_balance_disable_threshold == 1.5
        assert restarted.settings.proxy_pool_enabled is False
        assert restarted.settings.proxy_pool == ""
    finally:
        restarted.stop()


def test_accepted_submission_storage_failure_blocks_replay(setup, monkeypatch):
    db, s, a = setup

    def broken(*args):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(db, "record_submission", broken)
    task = s.create_task(request())
    done = s.wait_task(task["id"], 5)
    assert done["error_code"] == "SUBMISSION_UNKNOWN"
    assert done["upstream_response"]["submit"]["id"] == 123
    assert FakeClient.submits == 1
    with pytest.raises(ValueError):
        s.retry_task(task["id"])


def test_restart_never_replays_interrupted_submission(setup):
    db, s, a = setup
    from app.model_catalog import normalize_generation_request

    task = db.create_task(
        "interrupted", normalize_generation_request(request(), s.settings)
    )
    db.acquire_account(task_id=task["id"])
    db.reserve_task_balance(task["id"], a["id"], 12)
    db.update_task(task["id"], status="submitting")
    s.start()
    done = db.get_task(task["id"])
    assert done["error_code"] == "SUBMISSION_UNKNOWN"
    assert FakeClient.submits == 0
    assert db.get_account(a["id"])["reserved_balance"] == 0
    with pytest.raises(ValueError):
        s.retry_task(task["id"])


def test_retry_query_keeps_record_and_does_not_submit_or_charge_again(setup):
    db, s, a = setup
    from app.model_catalog import normalize_generation_request

    task = db.create_task(
        "timed-out", normalize_generation_request(request(), s.settings)
    )
    db.acquire_account(task_id=task["id"])
    db.reserve_task_balance(task["id"], a["id"], 12)
    db.record_submission(task["id"], a["id"], "123", {}, 12)
    db.release_account(a["id"], task_id=task["id"])
    db.update_task(task["id"], status="expired", error_code="TIMEOUT")
    FakeClient.balance = 2
    resumed = s.retry_task(task["id"])
    assert resumed["id"] == task["id"]
    done = s.wait_task(task["id"], 5)
    assert done["status"] == "succeeded" and FakeClient.submits == 0
    assert db.get_account(a["id"])["last_balance"] == 2


@pytest.mark.parametrize(
    "failure_code,category,message",
    [
        (
            3008,
            "OUTPUT_MODERATION_FAILED",
            "生成的视频内容违规，请修改描述后重试，积分已返还~",
        ),
        (
            3000,
            "CONTENT_MODERATION_FAILED",
            "检测到内容有敏感或违规情况，请修改后重试，积分已返还～",
        ),
    ],
)
def test_terminal_moderation_refund_keeps_message_and_net_cost_consistent(
    setup, monkeypatch, failure_code, category, message
):
    from app.task_errors import COPYRIGHT_REFUND_MESSAGE, SENSITIVE_INPUT_REFUND_MESSAGE

    db, service, account = setup
    monkeypatch.setattr(
        FakeClient, "status", lambda *a: {"id": 123, "status": "failed"}
    )
    failed = {
        "status": "failed",
        "failCode": failure_code,
        "failMsg": COPYRIGHT_REFUND_MESSAGE
        if failure_code == 3008
        else SENSITIVE_INPUT_REFUND_MESSAGE,
        "refundCreditDecimal": None,
    }

    def detail(*args):
        FakeClient.balance = 14  # Upstream balance endpoint reflects the refund.
        return {
            **failed,
            "generateRecord": {**failed, "id": 123, "creditDecimal": "12"},
            "generations": [dict(failed)],
        }

    monkeypatch.setattr(FakeClient, "detail", detail)
    job = service.create_task(request())
    done = service.wait_task(job["id"], 5)
    assert done["error_code"] == "GENERATION_FAILED" and done["actual_cost"] == 0
    assert done["upstream_response"]["refund"] == {
        "credits": 12,
        "charged": 12,
        "source": "upstream_failure_message",
    }
    assert service.public_task(done)["error"]["category"] == category
    assert done["error_message"] == message
    assert FakeClient.submits == 1
    future = service._futures.get(job["id"])
    if future is not None:
        future.result(timeout=5)
    saved = db.get_account(account["id"])
    assert saved["last_balance"] == 14 and saved["reserved_balance"] == 0
