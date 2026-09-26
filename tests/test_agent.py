from copy import deepcopy

from app.agent import agent_files, agent_prompt, agent_video_detail


PAYLOAD = {
    "prompt": "A scene with dialogue",
    "upstream_model": "seedance-2-0-mini",
    "duration": 4,
    "resolution": "480p",
    "aspect_ratio": "21:9",
    "generate_audio": True,
    "n": 1,
}


def test_agent_prompt_uses_normalized_video_parameters():
    prompt = agent_prompt(PAYLOAD)
    assert prompt.startswith("A scene with dialogue\n\n")
    assert "Seedance 2.0 mini" in prompt
    assert "仅发起一次视频生成，最终有且仅生成并交付一个视频" in prompt
    assert "不要拆成多个独立视频" in prompt
    assert "不要额外生成候选版本" in prompt
    assert "4s、480P、21:9" in prompt
    assert "对白使用文中语言" in prompt
    assert "同步生成与画面内容匹配的声音" in prompt
    assert "model=seedance-2-0-mini" in prompt
    assert "duration=4" in prompt
    assert "resolution=480p" in prompt
    assert "aspect_ratio=21:9" in prompt
    assert "options.generate_audio=true" in prompt
    assert "不能省略，也不能只写在描述里" in prompt
    assert "不得用 adaptive、auto" in prompt
    assert "非静音的可听音轨" in prompt
    assert "不得输出无声视频" in prompt


def test_agent_prompt_respects_explicit_silent_video_request():
    prompt = agent_prompt({**PAYLOAD, "generate_audio": False})
    assert "按调用参数生成无声视频" in prompt
    assert "options.generate_audio=false" in prompt
    assert "非静音的可听音轨" not in prompt


def test_agent_prompt_does_not_forbid_requested_adaptive_ratio():
    prompt = agent_prompt({**PAYLOAD, "aspect_ratio": "adaptive"})
    assert "aspect_ratio=adaptive" in prompt
    assert "不得用 adaptive、auto" not in prompt


def test_agent_prompt_allows_requested_multiple_outputs_without_conflicting_rule():
    prompt = agent_prompt({**PAYLOAD, "n": 2})
    assert "恰好生成并交付 2 个视频" in prompt
    assert "不要拆成多个独立视频" not in prompt


def test_agent_files_preserve_uploaded_reference_order():
    body = {
        "userInput": {
            "refs": [
                {"type": "image", "image": "https://cdn.example/one.png"},
                {"type": "video", "video": "https://cdn.example/clip.mp4"},
                {"type": "image", "image": "https://cdn.example/two.jpg"},
            ]
        }
    }
    files = agent_files(body)
    assert [x["ref"] for x in files] == ["Image 1", "Video 1", "Image 2"]
    assert [x["content_type"] for x in files] == [
        "image/png",
        "video/mp4",
        "image/jpeg",
    ]


def test_agent_final_artifact_and_discounted_charge():
    artifacts = {
        "artifact_groups": {
            "video": {
                "artifacts": [
                    {
                        "media_type": "video",
                        "delivery_role": "final",
                        "asset": {
                            "asset_url": "https://cdn.example/video.mp4",
                            "no_watermark_url": "https://cdn.example/clean.mp4",
                            "cover_url": "https://cdn.example/cover.jpg",
                        },
                        "media": {
                            "duration_sec": 4.096,
                            "resolution": "480p",
                            "aspect_ratio": "21:9",
                            "width": 1008,
                            "height": 432,
                        },
                        "generation": {"model": "Seedance 2.0 mini"},
                    }
                ]
            }
        }
    }
    messages = [
        {"additional_kwargs": {"billing": {"total": 10}}},
        {"name": "message_notify_user", "additional_kwargs": {"billing": {"total": 16}}},
    ]
    detail = agent_video_detail(artifacts, messages, PAYLOAD)
    assert detail["status"] == "succeed"
    assert detail["generateRecord"]["creditDecimal"] == 16
    assert detail["generations"][0]["videoUrlNoWatermark"] == (
        "https://cdn.example/clean.mp4"
    )
    assert detail["generations"][0]["videoMeta"]["duration"] == 4.096
    wrong = agent_video_detail(artifacts, messages, {**PAYLOAD, "aspect_ratio": "16:9"})
    assert wrong["status"] == "failed"
    assert wrong["errorCode"] == "AGENT_OUTPUT_MISMATCH"
    assert "比例 21:9" in wrong["errorMessage"]
    empty = agent_video_detail({"artifact_groups": {}}, messages, PAYLOAD)
    assert empty["status"] == "failed"
    assert empty["errorCode"] == "GENERATION_FAILED"
    extra = deepcopy(artifacts)
    extra_video = deepcopy(extra["artifact_groups"]["video"]["artifacts"][0])
    extra_video["media"]["aspect_ratio"] = "3:4"
    extra["artifact_groups"]["video"]["artifacts"].append(extra_video)
    selected = agent_video_detail(extra, messages, PAYLOAD)
    assert selected["status"] == "succeed"
    assert len(selected["generations"]) == 1
    assert selected["generations"][0]["videoMeta"]["aspect_ratio"] == "21:9"
