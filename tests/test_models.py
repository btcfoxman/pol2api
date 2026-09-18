import pytest
from app.config import Settings
from app.model_catalog import (
    DEFAULT_MODEL_MAP,
    normalize_generation_request,
    normalize_prompt,
    public_models,
)


@pytest.mark.parametrize("model,target", DEFAULT_MODEL_MAP.items())
def test_all_requested_model_aliases(model, target):
    p = normalize_generation_request(
        {"model": model, "prompt": "图1", "image_urls": ["https://example.com/a.png"]},
        Settings(),
    )
    assert p["upstream_model"] == target
    assert p["resolution"] == (
        "4K"
        if model.endswith("-4k")
        else "480p"
        if model.endswith("-480p")
        else "1080p"
        if model.endswith("-1080p")
        else "720p"
    )
    assert p["prompt"] == "[@image_1]"


def test_references_are_per_kind_and_idempotent():
    media = {
        "image": [{"label": f"image_{i}"} for i in range(1, 4)],
        "video": [{"label": f"video_{i}"} for i in range(1, 5)],
        "audio": [{"label": f"audio_{i}"} for i in range(1, 3)],
    }
    source = "视频1|video2|v3|视4|图片1|图1|img2|Image3|Audio1|音频2"
    output = normalize_prompt(source, media)
    assert output == "|".join(
        f"[@{name}]"
        for name in [
            "video_1",
            "video_2",
            "video_3",
            "video_4",
            "image_1",
            "image_1",
            "image_2",
            "image_3",
            "audio_1",
            "audio_2",
        ]
    )
    assert normalize_prompt(output, media) == output
    assert (
        normalize_prompt("电影video1中的图1", media) == "电影[@video_1]中的[@image_1]"
    )
    assert normalize_prompt("myvideo1.mp4", media) == "myvideo1.mp4"
    with pytest.raises(ValueError):
        normalize_prompt("v5", media)


def test_named_references_and_missing_alias():
    media = {"image": [{"label": "image_1", "name": "storyboard"}]}
    assert normalize_prompt("use [@storyboard]", media) == "use [@image_1]"
    with pytest.raises(ValueError):
        normalize_prompt("[@unknown]", media)
    assert normalize_prompt("remove 图2", media, True) == "remove "


def test_25_media_limits_and_no_fake_support():
    body = {
        "model": "sd-2-5",
        "prompt": "图30 视频10 音频10",
        "image_urls": ["https://example.com/a.png"] * 30,
        "video_urls": ["https://example.com/a.mp4"] * 10,
        "audio_urls": ["https://example.com/a.mp3"] * 10,
        "duration": 30,
    }
    result = normalize_generation_request(body, Settings())
    assert len(result["_images"]) == 30 and len(result["_videos"]) == 10
    with pytest.raises(ValueError):
        normalize_generation_request(
            {**body, "image_urls": body["image_urls"] + body["image_urls"][:1]},
            Settings(),
        )
    with pytest.raises(ValueError):
        normalize_generation_request({**body, "resolution": "4k"}, Settings())
    assert len(public_models()) >= 9


@pytest.mark.parametrize(
    "patch",
    [
        {"resolution": "1080p", "model": "sd-2-0-fast"},
        {"model": "sd-2-0-1080p", "resolution": "720p"},
        {"duration": 3},
        {"n": 5},
        {"n": 1.5},
        {"background": "false"},
        {"image_urls": ["C:/secret.txt"]},
    ],
)
def test_invalid_requests_fail_before_queue(patch):
    with pytest.raises(ValueError):
        normalize_generation_request(
            {"prompt": "a", "image_urls": ["https://example.com/a.png"], **patch},
            Settings(),
        )


def test_content_api_and_text_mode():
    value = normalize_generation_request(
        {
            "model": "sd-2-0",
            "content": [
                {"type": "text", "text": "图1"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/a.png"},
                },
            ],
        },
        Settings(),
    )
    assert value["prompt"] == "[@image_1]"
    assert value["_recipe"] == "ref2video"
    text = normalize_generation_request(
        {"model": "sd-2-5", "prompt": "a landscape"}, Settings()
    )
    assert text["_recipe"] == "multi2video"


def test_wan_public_alias_capabilities_and_resolution_conflicts():
    models = {m["id"]: m for m in public_models()}
    for alias, resolution in (
        ("wan-3.0-480p", "480p"),
        ("wan-3.0", "720p"),
        ("wan-3.0-1080p", "1080p"),
    ):
        caps = models[alias]["capabilities"]
        assert caps["default_resolution"] == resolution
        assert set(caps["generation_modes"]) == {"reference", "text", "image"}
        assert caps["durations"] == list(range(2, 31))
        assert caps["media_limits"] == {"images": 10, "videos": 5, "audio": 5}
        if alias != "wan-3.0":
            assert caps["resolutions"] == [resolution]
            with pytest.raises(ValueError, match="分辨率"):
                normalize_generation_request(
                    {"model": alias, "prompt": "scene", "resolution": "720p"},
                    Settings(),
                )
    with pytest.raises(ValueError):
        normalize_generation_request(
            {"model": "wan-3.0", "prompt": "scene", "duration": 31}, Settings()
        )
