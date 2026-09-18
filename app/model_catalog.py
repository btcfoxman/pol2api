"""Model defaults and constraints from the captured Pollo manifests."""

from copy import deepcopy
import json
import math
from pathlib import Path
import re
from typing import Any

VIDEO_DIMENSIONS = {}  # Output dimensions come from the completed task, never invented.
RESOLUTION_ALIASES = {"4k": "4K", "480p": "480p", "720p": "720p", "1080p": "1080p"}
DEFAULT_MODEL_MAP = {
    "sd-2-0": "seedance-2-0",
    "sd-2-0-1080p": "seedance-2-0",
    "sd-2-0-4k": "seedance-2-0",
    "sd-2-0-fast": "seedance-2-0-fast",
    "sd-2-0-mini": "seedance-2-0-mini",
    "sd-2-0-fast-480p": "seedance-2-0-fast",
    "sd-2-5": "seedance-2-5",
    "sd-2-5-480p": "seedance-2-5",
    "sd-2-5-1080p": "seedance-2-5",
}
ALIASED_RESOLUTIONS = {
    key: (
        "4K"
        if key.endswith("-4k")
        else "1080p"
        if key.endswith("-1080p")
        else "480p"
        if key.endswith("-480p")
        else "720p"
    )
    for key in DEFAULT_MODEL_MAP
}
MODELS = {
    m["modelKey"]: m
    for m in json.loads(
        (Path(__file__).parent / "capabilities.json").read_text(encoding="utf-8")
    )["models"]
    if m["modelKey"].startswith("seedance-2-")
}
MEDIA_LIMITS = {"images": 30, "videos": 10, "audio": 10}


def model_map_json(raw: Any) -> str:
    source = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw or {}
    if not isinstance(source, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or v not in MODELS
        for k, v in source.items()
    ):
        raise ValueError("model_map 必须将外部名称映射到已接入的 Pollo 模型")
    return json.dumps({**DEFAULT_MODEL_MAP, **source}, ensure_ascii=False)


def enum_values(prop):
    return [
        item["value"] if isinstance(item, dict) else item
        for item in prop.get("enum", [])
    ]


def public_models(raw=""):
    mapping = json.loads(model_map_json(raw))
    mapping.update({key: key for key in MODELS})
    result = []
    for alias, key in mapping.items():
        spec = MODELS[key]
        p = spec["properties"]
        upload = spec["reference_config"]["upload"]
        resolutions = (
            [ALIASED_RESOLUTIONS[alias]]
            if alias in ALIASED_RESOLUTIONS
            and alias.endswith(("-480p", "-1080p", "-4k"))
            else enum_values(p["resolution"])
        )
        result.append(
            dict(
                id=alias,
                object="model",
                owned_by="pollo",
                meta={"label": alias, "upstream_model": key},
                capabilities=dict(
                    durations=list(
                        range(p["duration"]["minimum"], p["duration"]["maximum"] + 1)
                    ),
                    resolutions=resolutions,
                    default_resolution=ALIASED_RESOLUTIONS.get(alias, "720p"),
                    aspect_ratios=enum_values(p["aspectRatio"]),
                    media_limits={
                        "images": upload["image"]["maxNum"],
                        "videos": upload["video"]["maxNum"],
                        "audio": upload["audio"]["maxNum"],
                    },
                    max_total_references=spec["reference_config"]["maxItems"],
                    generate_audio=True,
                    max_outputs=4,
                    evidence="manifest",
                    verified_generation=(key == "seedance-2-0-mini"),
                ),
            )
        )
    return result


def _source(item):
    if isinstance(item, str):
        return {"value": item}
    if isinstance(item, dict):
        value = item.get("url") or item.get("value") or item.get("data")
        if isinstance(value, dict):
            value = value.get("url")
        if isinstance(value, str):
            return {"value": value, "name": str(item.get("name") or "")}
    raise ValueError("素材必须为 URL、data URL 或包含 url 的对象")


def normalize_generation_request(payload, settings):
    p = deepcopy(payload)
    for field in ("duration", "seconds", "n", "num_outputs", "seed"):
        if p.get(field) is not None:
            try:
                number = float(p[field])
                if (
                    not math.isfinite(number)
                    or isinstance(p[field], bool)
                    or number != int(number)
                ):
                    raise ValueError()
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{field} 必须为有限整数") from exc
    alias = str(p.get("model") or "sd-2-0-mini")
    mapping = json.loads(model_map_json(settings.model_map))
    key = mapping.get(alias, alias)
    if key not in MODELS:
        raise ValueError(f"不支持模型 {alias}")
    spec = MODELS[key]
    props = spec["properties"]
    config = spec["reference_config"]
    prompt = str(p.get("prompt") or "")
    media = {"image": [], "video": [], "audio": []}
    for kind, plural in (
        ("image", "image_urls"),
        ("video", "video_urls"),
        ("audio", "audio_urls"),
    ):
        items = p.get(plural) or []
        if not isinstance(items, list):
            raise ValueError(f"{plural} 必须为数组")
        media[kind].extend(_source(i) for i in items)
    for item in p.get("content") or []:
        kind = str(item.get("type", ""))
        if kind in ("text", "input_text"):
            prompt += ("\n" if prompt else "") + str(item.get("text") or "")
        else:
            for typ in media:
                if typ in kind:
                    media[typ].append(
                        _source(item.get(typ + "_url") or item.get(typ) or item)
                    )
                    break
    if not prompt.strip() or len(prompt) > 10000:
        raise ValueError("提示词须为 1–10000 字符")
    if media["video"] and not settings.allow_video_reference_inputs:
        raise ValueError("视频参考已被设置禁用")
    for kind, items in media.items():
        maximum = config["upload"][kind]["maxNum"]
        if len(items) > maximum:
            if settings.excess_media_policy == "ignore":
                del items[maximum:]
            else:
                raise ValueError(f"{key} 最多支持 {maximum} 个 {kind} 素材")
        for index, item in enumerate(items, 1):
            if not item["value"].startswith(("http://", "https://", "data:")):
                raise ValueError("素材仅支持 HTTP(S) / data URL")
            item["label"] = f"{kind}_{index}"
    if sum(map(len, media.values())) > config["maxItems"]:
        raise ValueError("超过总素材数量上限")
    if media["audio"] and not (media["image"] or media["video"]):
        raise ValueError("音频参考必须同时提供图片或视频")
    resolution = RESOLUTION_ALIASES.get(
        str(p.get("resolution") or ALIASED_RESOLUTIONS.get(alias, "720p")).lower(), ""
    )
    fixed = (
        ALIASED_RESOLUTIONS.get(alias)
        if alias.endswith(("-480p", "-1080p", "-4k"))
        else None
    )
    if fixed and resolution != fixed:
        raise ValueError("模型名中的分辨率与 resolution 冲突")
    if resolution not in enum_values(props["resolution"]):
        raise ValueError(f"{key} 不支持分辨率 {resolution}")
    ratio = str(p.get("aspect_ratio") or p.get("ratio") or "16:9")
    if ratio == "auto":
        ratio = "adaptive"
    if ratio not in enum_values(props["aspectRatio"]):
        raise ValueError(f"{key} 不支持画幅 {ratio}")
    duration = p.get("duration", p.get("seconds", 5))
    if isinstance(duration, bool) or float(duration) != int(duration):
        raise ValueError("duration 必须为整数秒")
    duration = int(duration)
    if not props["duration"]["minimum"] <= duration <= props["duration"]["maximum"]:
        raise ValueError("视频时长超出模型范围")
    n = p.get("n", p.get("num_outputs", 1))
    if isinstance(n, bool) or int(n) != float(n) or not 1 <= int(n) <= 4:
        raise ValueError("n 必须为 1–4")
    for boolean in ("background", "generate_audio", "published", "protection_mode"):
        if boolean in p and not isinstance(p[boolean], bool):
            raise ValueError(f"{boolean} 必须为布尔值")
    if p.get("seed") is not None and (
        isinstance(p["seed"], bool)
        or int(p["seed"]) != float(p["seed"])
        or not 0 <= int(p["seed"]) <= 2147483647
    ):
        raise ValueError("seed 超出范围")
    p.update(
        kind="video",
        model=alias,
        upstream_model=key,
        prompt=normalize_prompt(
            prompt, media, settings.prompt_media_reference_cleanup_enabled
        ),
        original_prompt=prompt,
        duration=duration,
        resolution=resolution,
        aspect_ratio=ratio,
        n=int(n),
        _images=media["image"],
        _videos=media["video"],
        _audio=media["audio"],
    )
    p["_recipe"] = "ref2video" if media["image"] or media["video"] else "multi2video"
    if p["_recipe"] == "multi2video" and key == "seedance-2-0-mini":
        raise ValueError("Mini 的文生视频未在上游模型列表中确认，请提供图片或视频参考")
    return p


def normalize_prompt(prompt, media, cleanup=False):
    lookup = {x["label"]: x["label"] for items in media.values() for x in items}
    for items in media.values():
        for x in items:
            name = x.get("name")
            if name:
                if name in lookup and lookup[name] != x["label"]:
                    raise ValueError("素材名称重复，请使用唯一名称")
                lookup[name] = x["label"]

    def resolve(kind, number):
        label = f"{kind}_{int(number)}"
        if label not in lookup:
            if cleanup:
                return ""
            raise ValueError(f"提示词引用 {label} 没有对应素材")
        return f"[@{label}]"

    def mention(match):
        name = match.group(1)
        if name in lookup:
            return f"[@{lookup[name]}]"
        parsed = re.fullmatch(
            r"(image|img|图片|图|video|视频|视|v|audio|音频)[ _]*(\d+)", name, re.I
        )
        if parsed:
            return alias(parsed)
        if cleanup:
            return ""
        raise ValueError("提示词存在未匹配的素材名称")

    def alias(match):
        prefix = match.group(1).lower()
        kind = (
            "image"
            if prefix in ("image", "img", "图片", "图")
            else "audio"
            if prefix in ("audio", "音频")
            else "video"
        )
        return resolve(kind, match.group(2))

    # Protect structured references from a second substitution.
    chunks = re.split(r"(\[@[^\]]+\])", prompt)
    pattern = r"(?<![A-Za-z0-9_/@.])(image|img|图片|图|video|视频|视|v|audio|音频)[ _]*(\d+)(?![A-Za-z0-9_])"
    return "".join(
        mention(re.fullmatch(r"\[@([^\]]+)\]", chunk))
        if re.fullmatch(r"\[@([^\]]+)\]", chunk)
        else re.sub(pattern, alias, chunk, flags=re.I)
        for chunk in chunks
    )
