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


@pytest.mark.parametrize("status", [408, 425, 500, 503])
def test_http_submit_uncertainty_never_becomes_safe_rejection(monkeypatch, status):
    from unittest.mock import Mock

    c = client()
    response = [
        {
            "error": {
                "json": {
                    "message": "Unavailable",
                    "data": {"code": "BAD_REQUEST", "httpStatus": status},
                }
            }
        }
    ]
    send = Mock(return_value=Mock(status_code=status, json=lambda: response))
    monkeypatch.setattr(c.session, "request", send)
    with pytest.raises(SubmissionUnknown):
        c.generate({})
    assert send.call_count == 1
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


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (403, {"error": "Forbidden"}, "UPLOAD_REJECTED"),
        (
            403,
            {"message": "Image content violates safety policy", "code": "FORBIDDEN"},
            "FORBIDDEN",
        ),
        (401, {"message": "Unauthorized"}, "AUTH_REQUIRED"),
        (200, {"success": False, "message": "Upload rejected"}, "UPLOAD_REJECTED"),
    ],
)
def test_upload_sign_rejections_do_not_invalidate_session(
    monkeypatch, status, body, expected
):
    from unittest.mock import Mock
    from app.task_errors import classify_failure

    c = client()
    monkeypatch.setattr(
        c.session,
        "request",
        Mock(return_value=Mock(status_code=status, json=lambda: body)),
    )
    with pytest.raises(UpstreamError) as caught:
        c._request(
            "POST",
            "/api/upload/sign",
            payload={"filename": "test.png", "type": "image"},
        )
    assert caught.value.code == expected
    if body.get("code") == "FORBIDDEN":
        assert (
            classify_failure(expected, str(caught.value)) == "IMAGE_MODERATION_FAILED"
        )
    c.close()


def test_trpc_http_403_preserves_business_reason(monkeypatch):
    from unittest.mock import Mock
    from app.task_errors import classify_failure

    c = client()
    response = [
        {
            "error": {
                "json": {
                    "message": "InputImageSensitiveContentDetected",
                    "data": {"code": "FORBIDDEN", "httpStatus": 403},
                }
            }
        }
    ]
    monkeypatch.setattr(
        c.session,
        "request",
        Mock(return_value=Mock(status_code=403, json=lambda: response)),
    )
    with pytest.raises(UpstreamError) as caught:
        c.rpc("uploadAsset.complete", {}, mutation=True)
    assert caught.value.code == "FORBIDDEN"
    assert (
        classify_failure(caught.value.code, str(caught.value))
        == "IMAGE_MODERATION_FAILED"
    )
    c.close()


@pytest.mark.parametrize("storage_failure", [False, True])
def test_storage_rejection_is_not_a_source_download_or_login_error(
    monkeypatch, storage_failure
):
    import io
    from PIL import Image
    from unittest.mock import Mock
    import requests

    c = client()
    content = io.BytesIO()
    Image.new("RGB", (64, 64)).save(content, format="PNG")
    monkeypatch.setattr(c, "download", lambda *a: (content.getvalue(), "image/png"))
    monkeypatch.setattr(
        c,
        "_request",
        lambda *a, **k: {
            "sign": "https://test.r2.cloudflarestorage.com/file",
            "accessURL": "https://example.org/file",
        },
    )
    put = (
        Mock(side_effect=requests.Timeout())
        if storage_failure
        else Mock(return_value=Mock(status_code=403))
    )
    monkeypatch.setattr(requests.Session, "put", put)
    with pytest.raises(UpstreamError) as caught:
        c.upload_media(
            {"value": "https://example.org/input.png", "label": "image_1"}, "image", {}
        )
    assert caught.value.code in {"UPLOAD_REJECTED", "UPLOAD_FAILED"}
    assert caught.value.stage == "upload_storage"
    assert put.call_count == 1
    c.close()


@pytest.mark.parametrize("succeed", [False, True])
def test_source_tls_disconnect_retries_get_only_and_reports_download_failure(
    monkeypatch, succeed
):
    from unittest.mock import MagicMock, Mock
    import requests

    c = client()
    monkeypatch.setattr("app.pollo_client.public_media_url", lambda url: None)
    monkeypatch.setattr("app.pollo_client.time.sleep", lambda seconds: None)
    response = MagicMock()
    response.__enter__.return_value = response
    response.is_redirect = False
    response.headers = {"Content-Type": "image/png"}
    response.iter_content.return_value = [b"complete image bytes"]
    effects = [requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")]
    effects += [response] if succeed else [requests.exceptions.SSLError("EOF")] * 2
    get = Mock(side_effect=effects)
    monkeypatch.setattr(requests.Session, "get", get)
    if succeed:
        assert (
            c.download("https://example.org/image.png", 100)[0]
            == b"complete image bytes"
        )
        assert get.call_count == 2
    else:
        with pytest.raises(UpstreamError) as caught:
            c.download("https://example.org/image.png", 100)
        assert caught.value.code == "MEDIA_DOWNLOAD_FAILED"
        assert get.call_count == 3
    c.close()


def test_source_http_403_is_not_retried_or_treated_as_upstream_auth(monkeypatch):
    import requests
    from unittest.mock import Mock

    c = client()
    error = requests.HTTPError(response=Mock(status_code=403))
    download = Mock(side_effect=error)
    monkeypatch.setattr(c, "_download_http", download)
    with pytest.raises(UpstreamError) as caught:
        c.download("https://example.org/expired.png", 100)
    assert caught.value.code == "MEDIA_DOWNLOAD_FAILED" and download.call_count == 1
    c.close()


def test_reference_video_pixel_limit_is_checked_before_upload(monkeypatch):
    from unittest.mock import Mock
    from app.task_errors import classify_failure

    c = client()
    monkeypatch.setattr(c, "download", lambda *a: (b"video bytes", "video/mp4"))
    probe = {
        "streams": [
            {
                "codec_type": "video",
                "width": 1440,
                "height": 2560,
                "avg_frame_rate": "24/1",
            }
        ],
        "format": {"duration": "15.084"},
    }
    monkeypatch.setattr(
        "app.pollo_client.subprocess.run",
        Mock(return_value=Mock(stdout=json.dumps(probe))),
    )
    upload = Mock(side_effect=AssertionError("Must reject before upload or generation"))
    monkeypatch.setattr(c, "_request", upload)
    rule = MODELS["seedance-2-0-mini"]["reference_config"]["upload"]["video"]
    with pytest.raises(ValueError) as caught:
        c.upload_media(
            {"value": "https://example.org/video.mp4", "label": "video_1"},
            "video",
            rule,
        )
    assert (
        classify_failure("INVALID_REQUEST", str(caught.value)) == "MEDIA_LIMIT_EXCEEDED"
    )
    upload.assert_not_called()
    c.close()
