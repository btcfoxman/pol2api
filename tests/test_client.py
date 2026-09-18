import json
import pytest
from app.config import Settings
from app.pollo_client import PolloClient, SubmissionUnknown, UpstreamError, result_urls
from app.model_catalog import MODELS, normalize_generation_request


def client():
    return PolloClient(
        {
            "cookie_header": "__Secure-next-auth.session-token=fake",
            "team_id": "project-test",
        },
        Settings(request_retries=3),
    )


def test_trpc_batch_and_errors(monkeypatch):
    c = client()
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return [{"result": {"data": {"json": {"id": 123, "status": "waiting"}}}}]

    monkeypatch.setattr(c, "_request", request)
    assert c.generate({"modelKey": "seedance-2-0-mini"})["id"] == 123
    assert (
        calls[0][0] == "POST"
        and calls[0][2]["payload"]["0"]["json"]["modelKey"] == "seedance-2-0-mini"
    )
    c.rpc("subUsage.getSubUsage", {"appName": "Pollo"})
    assert json.loads(calls[1][2]["params"]["input"]) == {
        "0": {"json": {"appName": "Pollo"}}
    }
    monkeypatch.setattr(
        c,
        "_request",
        lambda *a, **k: [
            {
                "error": {
                    "json": {
                        "message": "Denied",
                        "data": {"code": "UNAUTHORIZED", "httpStatus": 401},
                    }
                }
            }
        ],
    )
    with pytest.raises(UpstreamError, match="失效"):
        c.rpc("user.find")
    c.close()


def test_submit_network_failure_is_never_retried(monkeypatch):
    c = client()
    calls = []

    def broken(*args, **kwargs):
        calls.append(1)
        raise TimeoutError()

    monkeypatch.setattr(c.session, "request", broken)
    with pytest.raises(SubmissionUnknown):
        c.generate({})
    assert len(calls) == 1
    c.close()


def test_missing_submit_id_is_ambiguous(monkeypatch):
    c = client()
    monkeypatch.setattr(c, "rpc", lambda *a, **k: {"status": "waiting"})
    with pytest.raises(SubmissionUnknown):
        c.generate({})
    c.close()


def test_malformed_submit_error_response_is_ambiguous(monkeypatch):
    c = client()
    monkeypatch.setattr(c, "_request", lambda *a, **k: [{"error": None}])
    with pytest.raises(SubmissionUnknown):
        c.generate({})
    c.close()


def test_cookie_auth_and_balance_no_double_count(monkeypatch):
    c = client()
    monkeypatch.setattr(
        c,
        "_request",
        lambda *a, **k: {"user": {"id": "user-test", "email": "test@example.invalid"}},
    )
    monkeypatch.setattr(
        c,
        "rpc",
        lambda *a, **k: {
            "usageList": [{"usageType": "credits", "totalCount": 22, "useCount": 8}],
            "rewardUsage": {"totalCount": 22, "useCount": 8},
        },
    )
    state = c.account_state()
    assert state["available_balance"] == 14
    assert state["team_id"] == "project-test"
    assert c.session.cookies.get("__Secure-next-auth.session-token") == "fake"
    c.close()


def test_prepare_uses_full_references_and_discount_quote(monkeypatch):
    c = client()
    calls = []
    spec = MODELS["seedance-2-0-mini"]
    manifest = {
        "schema": {
            "properties": {
                **spec["properties"],
                "refs": {"x-ui-config": spec["reference_config"]},
            }
        },
        "initialValues": spec["initial_values"],
    }
    monkeypatch.setattr(c, "manifest", lambda p: manifest)
    monkeypatch.setattr(
        c,
        "upload_media",
        lambda source, kind, rule: {
            "type": kind,
            "name": source["label"],
            kind: "https://videocdn.pollo.ai/test",
            "metadata": {"duration": 2, "size": 123},
        },
    )

    def rpc(name, value, **kwargs):
        calls.append((name, value.copy()))
        return {
            "cost": 31,
            "discountCost": 12,
            "entitlementSource": "promotion_discount",
        }

    monkeypatch.setattr(c, "rpc", rpc)
    p = normalize_generation_request(
        {
            "prompt": "图1 视频1",
            "image_urls": ["https://example.com/a.png"],
            "video_urls": ["https://example.com/b.mp4"],
            "resolution": "480p",
        },
        Settings(),
    )
    body, quote, cost = c.prepare(p)
    assert cost == 12
    assert "projectId" not in calls[0][1] and body["projectId"] == "project-test"
    assert body["userInput"]["prompt"] == "[@image_1] [@video_1]"
    assert len(body["userInput"]["refs"]) == 2
    assert body["modelKey"] == body["userInput"]["model"] == "seedance-2-0-mini"
    c.close()


def test_query_id_types_and_result_urls(monkeypatch):
    c = client()
    seen = []

    def rpc(name, value):
        seen.append((name, value))
        return [
            {
                "id": 123,
                "status": "succeed",
                "generations": [{"videoId": "video-a", "status": "succeed"}],
            }
        ]

    monkeypatch.setattr(c, "rpc", rpc)
    assert c.status("123")["status"] == "succeed"
    assert seen[0][1] == {"recordIds": [123]}
    assert result_urls(
        {
            "generations": [
                {"videoUrl": "https://example.com/a.mp4"},
                {"mediaUrl": "https://example.com/b.mp4"},
            ]
        }
    ) == ["https://example.com/a.mp4", "https://example.com/b.mp4"]
    c.close()
