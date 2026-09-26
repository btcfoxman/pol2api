"""Pollo Agent request formatting and completed video normalization."""

from __future__ import annotations

import math
import mimetypes
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit


MODEL_LABELS = {
    "seedance-2-0-mini": "Seedance 2.0 mini",
    "seedance-2-0": "Seedance 2.0",
    "seedance-2-0-fast": "Seedance 2.0 fast",
    "seedance-2-5": "Seedance 2.5",
    "wan-v3-0": "Wan 3.0",
}


def agent_prompt(payload):
    model = MODEL_LABELS.get(payload["upstream_model"], payload["upstream_model"])
    count = int(payload.get("n") or 1)
    quantity = (
        "本次仅发起一次视频生成，最终有且仅生成并交付一个视频；不要拆成多个独立视频"
        if count == 1
        else f"本次最终恰好生成并交付 {count} 个视频"
    )
    resolution = str(payload["resolution"]).upper()
    audio_enabled = bool(payload.get("generate_audio", True))
    tool_rule = (
        "调用 generate_video 工具时，tasks 项的 model 必须是"
        f" {payload['upstream_model']}；任务描述必须明确"
        f" duration={payload['duration']}、resolution={payload['resolution']}、"
        f"aspect_ratio={payload['aspect_ratio']}；"
        "VideoTask 顶层不接受 duration、resolution、aspect_ratio，"
        "不要将这三个字段直接放入 tasks 项；"
        f"仅在模型支持时传入 options.generate_audio={'true' if audio_enabled else 'false'}；"
        "如果模型清单明确表示所选参数组合不可用，停止生成并说明不支持；"
    )
    if payload["aspect_ratio"] not in {"adaptive", "auto"}:
        tool_rule += "不得用 adaptive、auto 或参考图比例代替指定的 aspect_ratio；"
    audio_rule = (
        "必须在生成视频时同步生成与画面内容匹配的声音，并将声音合成进最终视频；"
        "成片必须包含非静音的可听音轨（对白、环境音或音效按内容生成），不得输出无声视频；"
        "对白使用文中语言；"
        if audio_enabled
        else (
            "本次按调用参数生成无声视频，不要添加声音或音轨；"
        )
    )
    rule = (
        f"强制指定 {model} 模型；{quantity}；"
        "不要额外生成候选版本；"
        f"{tool_rule}"
        f"{audio_rule}"
        f"最终视频格式必须是 {payload['duration']}s、{resolution}、{payload['aspect_ratio']}。"
        "若工具不支持上述模型或参数，停止生成并说明不支持；不要自行更换模型或格式。"
        "若视频工具返回内容审核或违规错误，不要重复提交，也不要换模型重试；"
        "立即停止并说明审核失败。"
    )
    original = str(payload.get("prompt") or "").rstrip()
    return original + "\n\n" + rule if original else rule


def agent_files(body):
    user_input = body.get("userInput") or {}
    references = user_input.get("refs") or []
    if not references:
        references = [
            {"type": "image", "image": user_input[field]}
            for field in ("image", "imageTail")
            if user_input.get(field)
        ]
    result = []
    counts = {"image": 0, "video": 0, "audio": 0}
    fallback_mime = {"image": "image/png", "video": "video/mp4", "audio": "audio/mpeg"}
    for reference in references:
        kind = reference.get("type")
        if kind not in counts:
            continue
        url = reference.get(kind)
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        counts[kind] += 1
        mime = mimetypes.guess_type(urlsplit(url).path)[0] or fallback_mime[kind]
        extension = PurePosixPath(urlsplit(url).path).suffix
        if not extension or len(extension) > 8:
            extension = mimetypes.guess_extension(mime) or ".bin"
        result.append(
            {
                "filename": f"{kind}_{counts[kind]}{extension}",
                "url": url,
                "content_type": mime,
                "ref": f"{kind.title()} {counts[kind]}",
            }
        )
    return result


def _normalized_model(value):
    return "".join(char for char in str(value).lower() if char.isalnum())


def agent_failure_reason(messages):
    """Extract a safe category from Agent tool results without storing prompts."""
    unsupported = False
    invalid_params = False
    for message in messages:
        if not isinstance(message, dict) or message.get("type") != "tool":
            continue
        name = message.get("name")
        content = str(message.get("content") or "")
        if name == "list_generation_models" and re.search(
            r"NO model satisfies all requirements", content, re.IGNORECASE
        ):
            unsupported = True
        if message.get("status") != "error" or name not in {"generate", "generate_video"}:
            continue
        if re.search(
            r"OutputVideoSensitiveContentDetected|output video.{0,80}(?:sensitive|copyright|policy violation)",
            content,
            re.IGNORECASE,
        ):
            return "OUTPUT_MODERATION_FAILED", "OutputVideoSensitiveContentDetected"
        if (message.get("additional_kwargs") or {}).get("error_code") == "INVALID_PARAMS":
            invalid_params = True
    if unsupported:
        return "AGENT_PARAMETERS_UNSUPPORTED", "Agent model parameter combination unsupported"
    if invalid_params:
        return "AGENT_TOOL_INVALID_PARAMS", "Agent video tool rejected parameters"
    return "AGENT_NO_VIDEO", "Agent did not produce a video"


def agent_video_detail(artifacts, messages, payload):
    group = (artifacts.get("artifact_groups") or {}).get("video") or {}
    videos = [
        item
        for item in group.get("artifacts") or []
        if isinstance(item, dict)
        and item.get("media_type") == "video"
        and item.get("delivery_role") == "final"
    ]
    requested_count = int(payload.get("n") or 1)
    videos = sorted(videos, key=lambda item: str(item.get("created_at") or ""))
    outputs = []
    rejected = []
    expected_model = MODEL_LABELS.get(
        payload["upstream_model"], payload["upstream_model"]
    )
    for item in videos:
        mismatches = []
        asset = item.get("asset") or {}
        media = item.get("media") or {}
        generation = item.get("generation") or {}
        url = asset.get("no_watermark_url") or asset.get("asset_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            rejected.append("视频地址无效")
            continue
        actual_model = generation.get("model") or ""
        if _normalized_model(actual_model) != _normalized_model(expected_model):
            mismatches.append(f"模型 {actual_model or '未知'}")
        if str(media.get("resolution") or "").lower() != str(
            payload["resolution"]
        ).lower():
            mismatches.append(f"分辨率 {media.get('resolution') or '未知'}")
        if payload["aspect_ratio"] not in {"auto", "adaptive"} and str(
            media.get("aspect_ratio") or ""
        ) != str(payload["aspect_ratio"]):
            mismatches.append(f"比例 {media.get('aspect_ratio') or '未知'}")
        try:
            duration = float(media.get("duration_sec"))
        except (TypeError, ValueError):
            duration = float("nan")
        if not math.isfinite(duration) or abs(duration - int(payload["duration"])) > 0.5:
            mismatches.append(f"时长 {media.get('duration_sec') or '未知'}s")
        if mismatches:
            rejected.extend(mismatches)
            continue
        outputs.append(
            {
                "videoUrlNoWatermark": asset.get("no_watermark_url") or "",
                "mediaUrl": asset.get("asset_url") or "",
                "videoUrl": url,
                "previewUrl": asset.get("cover_url") or "",
                "videoMeta": {
                    "width": media.get("width"),
                    "height": media.get("height"),
                    "duration": media.get("duration_sec"),
                    "aspect_ratio": media.get("aspect_ratio"),
                    "resolution": media.get("resolution"),
                    "model": actual_model,
                },
            }
        )
    outputs = outputs[-requested_count:]
    mismatches = []
    if len(outputs) < requested_count:
        mismatches.append(f"视频数量 {len(outputs)}")
        mismatches.extend(rejected)
    # The final notification carries the cumulative discounted charge.
    cost = None
    for message in reversed(messages):
        billing = (message.get("additional_kwargs") or {}).get("billing") or {}
        if isinstance(billing.get("total"), (int, float)):
            cost = max(float(billing["total"]), 0)
            break
    error_code, error_message = "", ""
    if mismatches and videos:
        error_code = "AGENT_OUTPUT_MISMATCH"
        error_message = "Agent 输出与请求不一致：" + "、".join(
            dict.fromkeys(mismatches)
        )
    elif mismatches:
        error_code, error_message = agent_failure_reason(messages)
    return {
        "status": "failed" if mismatches else "succeed",
        "errorCode": error_code,
        "generations": outputs,
        "generateRecord": {"creditDecimal": cost},
        "thumbnail": (outputs[0].get("previewUrl") if outputs else ""),
        "errorMessage": error_message,
    }
