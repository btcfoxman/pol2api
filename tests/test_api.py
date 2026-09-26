from fastapi.testclient import TestClient
import pytest
from app import main
from app.config import Settings
from app.db import Database
from app.service import PolService


@pytest.fixture
def api(tmp_path, monkeypatch):
    settings = Settings(
        api_key="api-key",
        admin_token="admin-key",
        sync_token="sync-key",
        database_path=str(tmp_path / "db.sqlite"),
    )
    db = Database(settings.database_path, 1)
    service = PolService(db, settings)
    monkeypatch.setattr(main, "database", db)
    monkeypatch.setattr(main, "service", service)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(service, "_schedule", lambda *a: None)
    with TestClient(main.app) as client:
        yield client, db


def test_api_auth_and_model_list(api):
    c, db = api
    assert c.get("/v1/models").status_code == 401
    response = c.get("/v1/models", headers={"Authorization": "Bearer api-key"})
    assert response.status_code == 200
    assert "sd-2-0-4k" in [m["id"] for m in response.json()["data"]]
    assert {"wan-3.0", "wan-3.0-480p", "wan-3.0-1080p"} <= {
        m["id"] for m in response.json()["data"]
    }
    assert c.get("/api/accounts").status_code == 401


def test_external_routes_create_query_results(api):
    c, db = api
    headers = {"X-API-Key": "api-key"}
    r = c.post(
        "/v1/videos", headers=headers, json={"model": "sd-2-5", "prompt": "a video"}
    )
    assert r.status_code == 200
    task = r.json()["id"]
    assert c.get(f"/v1/videos/{task}", headers=headers).json()["status"] == "queued"
    assert c.get(f"/v1/videos/{task}/content", headers=headers).status_code == 409
    db.update_task(
        task, status="succeeded", result_urls=["https://example.com/output.mp4"]
    )
    r = c.get(f"/v1/videos/{task}/content", headers=headers, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "https://example.com/output.mp4"
    assert (
        c.get(f"/v1/responses/{task}", headers=headers).json()["status"] == "completed"
    )
    assert c.get("/v1/videos/missing", headers=headers).status_code == 404
    invalid = c.post(
        "/v1/videos",
        headers=headers,
        json={"model": "sd-2-5", "prompt": "x", "resolution": "4k"},
    )
    assert invalid.status_code == 422


@pytest.mark.parametrize(
    "route,model",
    [
        ("/v1/videos", "wan-3.0-480p"),
        ("/v1/responses", "wan-3.0"),
        ("/api/v3/contents/generations/tasks", "wan-3.0-1080p"),
    ],
)
def test_wan_alias_submission_and_query_adapters(api, route, model):
    client, db = api
    headers = {"X-API-Key": "api-key"}
    payload = {"model": model, "duration": 4}
    payload["input" if route == "/v1/responses" else "prompt"] = "scene"
    result = client.post(route, headers=headers, json=payload)
    assert result.status_code == 200
    body = result.json()
    task_id = body["data"]["id"] if route.startswith("/api/v3") else body["id"]
    task = db.get_task(task_id)
    assert task["request"]["upstream_model"] == "wan-v3-0"
    assert task["request"]["model"] == model
    db.update_task(
        task_id,
        generation_id="123",
        status="succeeded",
        result_urls=["https://example.com/wan-original.mp4"],
    )
    result = client.get("/v1/videos/" + task_id, headers=headers).json()
    assert result["status"] == "succeeded"


def test_admin_settings_sync_and_task_defaults(api):
    c, db = api
    assert c.post("/login", data={"token": "admin-key"}).status_code == 200
    assert c.get("/api/settings").json()["agent_mode_enabled"] is False
    assert c.patch("/api/settings", json={"agent_mode_enabled": True}).json()[
        "agent_mode_enabled"
    ] is True
    result = c.post(
        "/v1/videos",
        headers={"X-API-Key": "api-key"},
        json={
            "model": "sd-2-0-mini",
            "prompt": "scene",
            "duration": 4,
            "image_urls": ["https://example.com/a.png"],
        },
    )
    assert result.status_code == 200
    assert db.get_task(result.json()["id"])["request"]["_submission_mode"] == "agent"
    assert c.patch("/api/settings", json={"agent_mode_enabled": False}).json()[
        "agent_mode_enabled"
    ] is False
    assert (
        c.post(
            "/api/accounts/sync", json={"name": "a"}, headers={"X-API-Key": "bad"}
        ).status_code
        == 401
    )
    synced = c.post(
        "/api/accounts/sync",
        json={"name": "a", "cookie_header": "__Secure-next-auth.session-token=private"},
        headers={"X-API-Key": "sync-key"},
    )
    assert synced.status_code == 200 and "private" not in synced.text
    c.post("/login", data={"token": "admin-key"})
    assert c.get("/").status_code == 200
    assert (
        c.patch("/api/settings", json={"task_workers": 3}).json()["task_workers"] == 3
    )
    r = c.post("/api/tasks", json={"model": "sd-2-0-1080p", "prompt": "x"})
    assert r.status_code == 200 and r.json()["request"]["resolution"] == "1080p"
    assert c.delete("/api/tasks").json()["deleted"] == 0


def test_batch_import_email_password_proxy_and_row_errors(api):
    client, db = api
    client.post("/login", data={"token": "admin-key"})
    response = client.post(
        "/api/accounts/batch-import",
        json={
            "text": "first@example.com|fake-secret|127.0.0.1:20001\n"
            "bad-row\n"
            "second@example.com|other-secret|socks5://127.0.0.1:20002",
            "start_login": False,
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["imported"] == 2
    assert result["needs_session"] == 2
    assert result["login_queued"] == 0
    assert result["errors"] == [
        {"index": 2, "error": "格式应为 邮箱|密码|代理，邮箱和密码不能为空"}
    ]
    assert "fake-secret" not in response.text
    assert "other-secret" not in response.text
    saved = db.get_account(result["accounts"][0]["id"], include_secrets=True)
    assert saved["password"] == "fake-secret"
    assert saved["proxy_url"] == "socks5://127.0.0.1:20001"
    assert saved["enabled"] is False
    assert saved["status"] == "login_required"


def test_batch_import_queues_password_login_by_default(api, monkeypatch):
    client, db = api
    client.post("/login", data={"token": "admin-key"})
    jobs = []
    monkeypatch.setattr(
        main.service._maintenance,
        "submit",
        lambda function, *args: jobs.append((function, args)),
    )
    response = client.post(
        "/api/accounts/batch-import",
        json={"text": "queued@example.com|fake-secret|127.0.0.1:20001"},
    )
    assert response.status_code == 200
    assert response.json()["login_queued"] == 1
    assert len(jobs) == 1
    account = db.get_account(response.json()["accounts"][0]["id"])
    assert account["status"] == "login_pending"
    assert account["enabled"] is False


def test_batch_import_keeps_valid_json_rows_and_existing_session(api):
    client, db = api
    client.post("/login", data={"token": "admin-key"})
    existing = db.upsert_account(
        {
            "name": "saved@example.com",
            "email": "saved@example.com",
            "cookie_header": "session=existing",
            "max_concurrency": 4,
            "enabled": True,
        }
    )
    response = client.post(
        "/api/accounts/batch-import",
        json={
            "accounts": [
                {"email": "fresh@example.com", "cookie_header": "session=new"},
                {"email": "invalid@example.com", "max_concurrency": 0},
            ],
            "start_login": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["imported"] == 1
    assert response.json()["errors"][0]["index"] == 2
    assert "session=new" not in response.text
    result = client.post(
        "/api/accounts/batch-import",
        json={"text": "saved@example.com|new-secret|127.0.0.1:20003"},
    )
    assert result.json()["imported"] == 1
    updated = db.get_account(existing["id"], include_secrets=True)
    assert updated["cookie_header"] == "session=existing"
    assert updated["password"] == "new-secret"
    assert updated["enabled"] is True
    assert updated["auto_login"] is True
    assert updated["max_concurrency"] == 4


def test_responses_and_generic_content_forms(api):
    c, db = api
    headers = {"Authorization": "Bearer api-key"}
    r = c.post(
        "/v1/responses",
        headers=headers,
        json={
            "model": "sd-2-5",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "图1"},
                        {
                            "type": "input_image",
                            "image_url": "https://example.com/image.png",
                        },
                    ],
                }
            ],
        },
    )
    assert r.status_code == 200
    assert db.get_task(r.json()["id"])["prompt"] == "[@image_1]"
    r = c.post(
        "/api/v3/contents/generations/tasks",
        headers=headers,
        json={"model": "sd-2-5", "content": [{"type": "text", "text": "a video"}]},
    )
    assert r.status_code == 200 and r.json()["code"] == 100


@pytest.mark.parametrize(
    "invalid",
    [{"duration": True}, {"n": 1.5}, {"image_urls": "bad"}, {"content": [None]}],
)
def test_invalid_external_payload_returns_422(api, invalid):
    c, db = api
    response = c.post(
        "/v1/videos",
        headers={"X-API-Key": "api-key"},
        json={"model": "sd-2-5", "prompt": "a video", **invalid},
    )
    assert response.status_code == 422


def test_download_variants_and_unknown_outcome(api):
    c, db = api
    headers = {"X-API-Key": "api-key"}
    task = c.post(
        "/v1/videos", headers=headers, json={"model": "sd-2-5", "prompt": "scene"}
    ).json()["id"]
    db.update_task(
        task,
        status="succeeded",
        result_urls=["https://example.com/original.mp4"],
        upstream_response={
            "downloads": [
                {
                    "url": "https://example.com/original.mp4",
                    "original_url": "https://example.com/original.mp4",
                    "preview_url": "https://example.com/preview.mp4",
                    "no_watermark_url": "",
                    "watermark_verified": False,
                }
            ]
        },
    )
    assert (
        c.get(
            f"/v1/videos/{task}/content?variant=no_watermark", headers=headers
        ).status_code
        == 403
    )
    r = c.get(
        f"/v1/videos/{task}/content?variant=original",
        headers=headers,
        follow_redirects=False,
    )
    assert r.headers["location"].endswith("original.mp4")
    assert c.get(f"/api/tasks/{task}/download").status_code == 401
    c.post("/login", data={"token": "admin-key"})
    assert (
        c.get(f"/api/tasks/{task}/download", follow_redirects=False).status_code == 307
    )
    db.update_task(task, status="expired", generation_id="123", error_code="TIMEOUT")
    assert (
        c.get(f"/v1/videos/{task}", headers=headers).json()["error"]["outcome"]
        == "unknown"
    )


def test_failure_contract_across_polling_adapters(api):
    c, db = api
    headers = {"X-API-Key": "api-key"}
    task = c.post(
        "/v1/videos", headers=headers, json={"prompt": "scene", "model": "sd-2-5"}
    ).json()["id"]
    db.update_task(
        task,
        status="failed",
        error_code="UPLOAD_FAILED",
        error_message="private diagnostic",
    )
    expected = {
        "code": "UPLOAD_FAILED",
        "category": "UPSTREAM_MAINTENANCE",
        "message": "上游维护中，请稍后再试~",
        "outcome": "rejected",
        "refunded": False,
    }
    for path in (
        f"/v1/videos/{task}",
        f"/v1/responses/{task}",
        f"/api/v3/contents/generations/tasks/{task}",
    ):
        response = c.get(path, headers=headers)
        body = response.json()
        if "data" in body and isinstance(body["data"], dict):
            body = body["data"]
        assert body["error"] == expected
        assert "private diagnostic" not in response.text


def test_media_validation_has_same_classification_before_task_creation(api):
    c, db = api
    response = c.post(
        "/v1/videos",
        headers={"X-API-Key": "api-key"},
        json={
            "model": "sd-2-0",
            "prompt": "scene",
            "image_urls": [f"https://example.org/{i}.png" for i in range(10)],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["category"] == "MEDIA_LIMIT_EXCEEDED"
    assert response.json()["detail"] == "素材超限，请修改后再试~"
