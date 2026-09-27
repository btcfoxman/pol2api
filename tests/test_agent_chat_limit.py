from app.config import Settings
from app.db import Database
from app.pollo_client import UpstreamError
from app.service import PolService
from app.db import now_ts


class LimitedClient:
    def __init__(self, account, settings):
        self.account = account

    def close(self):
        pass

    def account_state(self):
        return {"available_balance": 100, "buckets": {}, "team_id": "project", "plan": "free"}

    def prepare(self, payload):
        return {"projectId": "project", "userInput": {}}, {"discountCost": 10}, 10

    def generate_agent(self, body, payload):
        raise UpstreamError("Agent 今日对话额度已用尽，明日重置", "AGENT_CHAT_LIMIT", 429,
                            stage="submit")


def test_chat_limit_disables_account_and_returns_rejected_outcome(tmp_path):
    settings = Settings(database_path=str(tmp_path / "pol.sqlite"), agent_mode_enabled=True,
                        account_maintenance_interval_seconds=86400)
    db = Database(settings.database_path, 1)
    account = db.upsert_account({"name": "limited", "cookie_header": "session=fake",
                                 "last_balance": 100, "team_id": "project", "status": "active"})
    service = PolService(db, settings, LimitedClient)
    try:
        task = service.create_task({"model": "sd-2-0-mini", "prompt": "A short scene.",
                                    "duration": 5, "resolution": "480p", "account_id": account["id"],
                                    "image_urls": ["https://example.com/a.png"]})
        done = service.wait_task(task["id"], 5)
        assert done["status"] == "failed"
        assert done["error_code"] == "AGENT_CHAT_LIMIT"
        assert service.public_task(done)["error"]["outcome"] == "rejected"
        saved = db.get_account(account["id"])
        assert saved["status"] == "agent_chat_limited"
        assert saved["enabled"] is False
        service._store_state(account["id"], LimitedClient({}, settings).account_state())
        assert db.get_account(account["id"])["status"] == "agent_chat_limited"
        service._release_agent_chat_limit_accounts()
        assert db.get_account(account["id"])["enabled"] is False
        db.update_account(account["id"], {"last_checked_at": now_ts() - 86401})
        service._release_agent_chat_limit_accounts()
        assert db.get_account(account["id"])["status"] == "pending"
        assert db.get_account(account["id"])["enabled"] is True
        db.update_account(account["id"], {"enabled": False, "status": "agent_chat_limited"})
        service.update_account(account["id"], {"enabled": True})
        assert db.get_account(account["id"])["status"] == "active"
    finally:
        service.stop()
