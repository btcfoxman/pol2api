from copy import deepcopy
import pytest

from app.config import Settings
from app.model_catalog import normalize_generation_request
from app.pollo_client import PolloClient, UpstreamError, result_urls
from app.recipes import RECIPES

IMAGE = "https://example.com/frame.png"


def normalized(**kwargs):
    return normalize_generation_request(
        dict(model="sd-2-0", prompt="scene", **kwargs), Settings()
    )


def test_first_tail_frame_payload_and_live_quote(monkeypatch):
    payload = normalized(
        image_url=IMAGE, image_tail_url=IMAGE + "?tail", web_search=False, seed=1
    )
    client = PolloClient({"team_id": "project"}, Settings())
    seen = []
    monkeypatch.setattr(
        client, "manifest", lambda p: RECIPES["seedance-2-0"]["multi2video"]
    )
    monkeypatch.setattr(
        client,
        "upload_media",
        lambda src, kind, rule: {"image": src["value"], "type": kind, "metadata": {}},
    )
    monkeypatch.setattr(
        client,
        "rpc",
        lambda name, body, **kw: seen.append(deepcopy(body))
        or {"cost": 40, "discountCost": 0},
    )
    body, _, cost = client.prepare(payload)
    assert cost == 0 and body["recipeCode"] == "multi2video"
    values = body["userInput"]
    assert values["image"] == IMAGE and values["imageTail"].endswith("?tail")
    assert values["webSearch"] is False and values["seed"] == 1 and "refs" not in values
    assert seen[0] == {k: v for k, v in body.items() if k != "projectId"}
    client.close()


@pytest.mark.parametrize(
    "patch",
    [
        {"image_tail_url": IMAGE},
        {"image_url": IMAGE, "image_urls": [IMAGE]},
        {"generation_mode": "text", "image_urls": [IMAGE]},
        {"generation_mode": "image", "image_url": IMAGE, "video_urls": [IMAGE]},
        {"generation_mode": "reference"},
    ],
)
def test_incompatible_modes_rejected(patch):
    with pytest.raises(ValueError):
        normalized(**patch)


@pytest.mark.parametrize(
    "model,patch",
    [
        ("pollo-v2-5", {"duration": 13}),
        ("pollo-v2-5", {"duration": 15, "generate_audio": False}),
        ("pollo-v3-0", {"resolution": "4K", "image_urls": [IMAGE]}),
        ("minimax-h3", {"generate_audio": True}),
        ("minimax-h3", {"seed": 1}),
        ("minimax-h3-max", {"duration": 4}),
    ],
)
def test_per_model_constraints(model, patch):
    with pytest.raises(ValueError):
        normalize_generation_request(
            {"model": model, "prompt": "scene", **patch}, Settings()
        )


def test_wan_duration_hailuo_resolution_and_pollo_pro():
    cases = [
        {"model": "wan-v3-0", "duration": 2, "prompt": "s" * 12000},
        {"model": "minimax-h3", "resolution": "2k", "aspect_ratio": "adaptive"},
        {
            "model": "pollo-v3-0",
            "resolution": "4k",
            "mode": "pro",
            "image_urls": [IMAGE],
        },
        {"model": "sd-2-0-fast", "generation_mode": "text"},
    ]
    results = [
        normalize_generation_request({"prompt": "scene", **case}, Settings())
        for case in cases
    ]
    assert results[0]["duration"] == 2 and results[1]["aspect_ratio"] == "auto"
    assert results[1]["resolution"] == "2K" and results[2]["resolution"] == "4K"
    assert results[3]["_recipe"] == "multi2video"


@pytest.mark.parametrize(
    "alias,resolution",
    [("wan-3.0-480p", "480p"), ("wan-3.0", "720p"), ("wan-3.0-1080p", "1080p")],
)
@pytest.mark.parametrize("mode", ["text", "image", "reference"])
def test_wan_aliases_prepare_each_mode_and_keep_dynamic_quote(
    monkeypatch, alias, resolution, mode
):
    request = {
        "model": alias,
        "prompt": "scene",
        "generation_mode": mode,
        "duration": 4,
    }
    if mode == "image":
        request.update(image_url=IMAGE, image_tail_url=IMAGE + "?tail")
    elif mode == "reference":
        request.update(
            image_urls=[IMAGE],
            video_urls=["https://example.com/ref.mp4"],
            audio_urls=["https://example.com/ref.mp3"],
            prompt="图1 视频1 音频1",
        )
    payload = normalize_generation_request(request, Settings())
    client = PolloClient({"team_id": "project"}, Settings())
    monkeypatch.setattr(client, "manifest", lambda p: RECIPES["wan-v3-0"][p["_recipe"]])
    monkeypatch.setattr(
        client,
        "upload_media",
        lambda src, kind, rule: {kind: src["value"], "type": kind, "metadata": {}},
    )
    calls = []
    monkeypatch.setattr(
        client,
        "rpc",
        lambda name, body, **kw: calls.append((name, deepcopy(body)))
        or {"cost": 999, "discountCost": 7.5},
    )
    try:
        body, _, cost = client.prepare(payload)
    finally:
        client.close()
    assert body["modelKey"] == body["userInput"]["model"] == "wan-v3-0"
    assert body["userInput"]["resolution"] == resolution
    assert body["userInput"]["generateAudio"] is True
    assert cost == 7.5 and calls[0][0] == "recipe.estimateTask"
    if mode == "reference":
        assert body["recipeCode"] == "ref2video"
        assert body["userInput"]["prompt"] == "[@image_1] [@video_1] [@audio_1]"
    else:
        assert body["recipeCode"] == "multi2video" and "refs" not in body["userInput"]


def test_original_and_official_download_precede_preview(monkeypatch):
    client = PolloClient({}, Settings())
    detail = {
        "videoId": "video-string",
        "id": 123,
        "videoUrl": IMAGE + "?preview",
        "mediaUrl": IMAGE + "?original",
    }
    assert result_urls(detail) == [detail["mediaUrl"]]
    calls = []

    def rpc(name, body, **kw):
        calls.append((name, body, kw))
        return {"videoUrlNoWatermark": detail["mediaUrl"]}

    monkeypatch.setattr(client, "rpc", rpc)
    rows = client.downloads(detail)
    assert calls == [
        (
            "video.getVideoNoWatermarkUrl",
            {"videoId": "video-string"},
            {"mutation": True},
        )
    ]
    assert (
        rows[0]["url"] == detail["mediaUrl"] and rows[0]["watermark_verified"] is True
    )
    client.close()


def test_download_denied_preserves_original_without_claiming_watermark_free(
    monkeypatch,
):
    client = PolloClient({}, Settings())

    def denied(*args, **kwargs):
        raise UpstreamError("denied", "FORBIDDEN", 403)

    monkeypatch.setattr(client, "rpc", denied)
    rows = client.downloads(
        {
            "generations": [
                {"videoId": "v1", "mediaUrl": IMAGE},
                {"videoId": "v2", "videoUrl": IMAGE + "?2"},
            ]
        }
    )
    assert len(rows) == 2 and rows[0]["url"] == IMAGE
    assert all(
        not row["watermark_verified"] and not row["no_watermark_url"] for row in rows
    )
    assert rows[0]["download_warning"] == "FORBIDDEN"
    client.close()


def test_cdp_ipv6_fallback(monkeypatch):
    import io
    import json
    from app import cdp

    hosts = []

    class Opener:
        def open(self, url, **kw):
            hosts.append(url)
            if "127.0.0.1" in url:
                raise OSError("IPv6 only")
            return io.StringIO(
                json.dumps(
                    {"webSocketDebuggerUrl": "ws://[::1]:9336/devtools/browser/test"}
                )
            )

    class WS:
        def send(self, value):
            pass

        def close(self):
            pass

        def recv(self):
            return json.dumps(
                {
                    "id": 1,
                    "result": {
                        "cookies": [
                            {
                                "name": "__Secure-next-auth.session-token",
                                "value": "fake",
                                "domain": ".pollo.ai",
                            }
                        ]
                    },
                }
            )

    monkeypatch.setattr(cdp, "build_opener", lambda *a: Opener())
    monkeypatch.setattr(cdp.websocket, "create_connection", lambda *a, **kw: WS())
    assert cdp.read_session(9336)["cookie_records"][0]["value"] == "fake"
    assert len(hosts) == 2 and "[::1]" in hosts[1]
