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


def test_admin_settings_sync_and_task_defaults(api):
    c, db = api
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
