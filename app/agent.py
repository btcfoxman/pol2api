"""Pollo Agent request formatting and completed video normalization."""

from __future__ import annotations

import math
import mimetypes
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
    rule = (
        f"强制指定 {model} 模型；{quantity}；"
        "不要额外生成候选版本；"
        "对白使用文中语言；"
        f"最终视频格式必须是 {payload['duration']}s、{resolution}、{payload['aspect_ratio']}。"
        "不要自行更换模型或格式。"
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
    error_code = (
        "AGENT_OUTPUT_MISMATCH"
        if mismatches and videos
        else "GENERATION_FAILED"
        if mismatches
        else ""
    )
    return {
        "status": "failed" if mismatches else "succeed",
        "errorCode": error_code,
        "generations": outputs,
        "generateRecord": {"creditDecimal": cost},
        "thumbnail": (outputs[0].get("previewUrl") if outputs else ""),
        "errorMessage": (
            "Agent 输出与请求不一致：" + "、".join(dict.fromkeys(mismatches))
            if error_code == "AGENT_OUTPUT_MISMATCH"
            else "Agent 未生成视频"
            if error_code
            else ""
        ),
    }
