"""Model defaults and constraints from the captured Pollo manifests."""

from copy import deepcopy
import json
import math
import re
from typing import Any

from app.recipes import (
    RECIPES,
    enum_values,
    media_rules,
    recipe_capabilities,
    validate_parameters,
)

VIDEO_DIMENSIONS = {}  # Output dimensions come from the completed task, never invented.
RESOLUTION_ALIASES = {
    "4k": "4K",
    "2k": "2K",
    "480p": "480p",
    "720p": "720p",
    "768p": "768p",
    "1080p": "1080p",
}
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
    "wan-3.0": "wan-v3-0",
    "wan-3.0-480p": "wan-v3-0",
    "wan-3.0-1080p": "wan-v3-0",
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
MODELS = {}
for _model, _recipes in RECIPES.items():
    _preferred = "ref2video" if "ref2video" in _recipes else "multi2video"
    _manifest = _recipes[_preferred]
    MODELS[_model] = dict(
        modelKey=_model,
        properties=_manifest["schema"]["properties"],
        reference_config=media_rules(_manifest, _preferred),
        initial_values=_manifest.get("initialValues", {}),
    )
MEDIA_LIMITS = {"images": 30, "videos": 10, "audio": 10}


def model_map_json(raw: Any) -> str:
    source = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw or {}
    if not isinstance(source, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or v not in MODELS
        for k, v in source.items()
    ):
        raise ValueError("model_map 必须将外部名称映射到已接入的 Pollo 模型")
    return json.dumps({**DEFAULT_MODEL_MAP, **source}, ensure_ascii=False)


def public_models(raw=""):
    mapping = json.loads(model_map_json(raw))
    mapping.update({key: key for key in MODELS})
    result = []
    for alias, key in mapping.items():
        variants = {}
        for recipe, manifest in RECIPES[key].items():
            caps = recipe_capabilities(manifest, recipe)
            if recipe == "ref2video":
                variants["reference"] = caps
            else:
                variants["text"] = {
                    **caps,
                    "media_limits": {"images": 0, "videos": 0, "audio": 0},
                    "max_total_references": 0,
                }
                if caps["media_limits"]["images"]:
                    variants["image"] = caps
        primary = (
            "reference"
            if "reference" in variants
            else "image"
            if "image" in variants
            else "text"
        )
        caps = deepcopy(variants[primary])
        if alias in ALIASED_RESOLUTIONS:
            caps["default_resolution"] = ALIASED_RESOLUTIONS[alias]
            if alias.endswith(("-480p", "-1080p", "-4k")):
                for variant in variants.values():
                    variant["resolutions"] = [ALIASED_RESOLUTIONS[alias]]
                    variant["default_resolution"] = ALIASED_RESOLUTIONS[alias]
                caps["resolutions"] = [ALIASED_RESOLUTIONS[alias]]
        caps.update(
            generation_modes=list(variants),
            modes=variants,
            default_generation_mode=primary,
            evidence="server_manifest",
            verified_generation=key
            in {"seedance-2-0-mini", "seedance-2-0", "seedance-2-5"},
        )
        result.append(
            dict(
                id=alias,
                object="model",
                owned_by="pollo",
                meta={"label": alias, "upstream_model": key},
                capabilities=caps,
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
            value = p[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value != int(value)
            ):
                raise ValueError(f"{field} 必须为有限整数")
    if "background" in p and not isinstance(p["background"], bool):
        raise ValueError("background 必须为布尔值")
    alias = str(p.get("model") or "sd-2-0-mini")
    mapping = json.loads(model_map_json(settings.model_map))
    key = mapping.get(alias, alias)
    if key not in MODELS:
        raise ValueError(f"不支持模型 {alias}")
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
    explicit_frames = bool(p.get("image_url") or p.get("image_tail_url"))
    if explicit_frames:
        if media["image"]:
            raise ValueError(
                "image_url/image_tail_url 不能与 image_urls/content 图片混用"
            )
        if not p.get("image_url"):
            raise ValueError("尾帧需要同时提供 image_url 首帧")
        media["image"] = [_source(p["image_url"])]
        if p.get("image_tail_url"):
            media["image"].append(_source(p["image_tail_url"]))
    mode = p.get("generation_mode", "auto")
    if mode not in ("auto", "reference", "image", "text"):
        raise ValueError("generation_mode 必须为 auto/reference/image/text")
    if mode == "auto":
        mode = (
            "image"
            if explicit_frames
            else "reference"
            if (media["image"] or media["video"]) and "ref2video" in RECIPES[key]
            else "image"
            if media["image"]
            else "text"
        )
    recipe = "ref2video" if mode == "reference" else "multi2video"
    if recipe not in RECIPES[key]:
        raise ValueError(f"{key} 的 {mode} 模式尚未从上游配置确认")
    manifest = RECIPES[key][recipe]
    props = manifest["schema"]["properties"]
    rules = media_rules(manifest, recipe)
    if mode == "reference" and explicit_frames:
        raise ValueError("reference 模式请使用 image_urls")
    if mode == "reference" and not (media["image"] or media["video"]):
        raise ValueError("参考模式至少需要图片或视频")
    if mode == "image" and not media["image"]:
        raise ValueError("image 模式需要首帧图片")
    if mode == "text" and any(media.values()):
        raise ValueError("text 模式不接受素材")
    if media["audio"] and not (media["image"] or media["video"]):
        raise ValueError("音频参考必须同时提供图片或视频")
    if media["video"] and not settings.allow_video_reference_inputs:
        raise ValueError("视频参考已被设置禁用")
    for kind, items in media.items():
        maximum = rules["upload"][kind]["maxNum"]
        if len(items) > maximum:
            if (
                settings.excess_media_policy == "ignore"
                and mode == "reference"
                and maximum > 0
            ):
                del items[maximum:]
            else:
                raise ValueError(
                    f"{key} 的 {mode} 模式最多支持 {maximum} 个 {kind} 素材"
                )
        for index, item in enumerate(items, 1):
            if not item["value"].startswith(("http://", "https://", "data:")):
                raise ValueError("素材仅支持 HTTP(S) / data URL")
            item["label"] = f"{kind}_{index}"
    if sum(map(len, media.values())) > rules["maxItems"]:
        raise ValueError("超过总素材数量上限")
    caps = recipe_capabilities(manifest, recipe)
    resolution = RESOLUTION_ALIASES.get(
        str(
            p.get("resolution")
            or ALIASED_RESOLUTIONS.get(alias, caps["default_resolution"])
        ).lower(),
        "",
    )
    fixed = (
        ALIASED_RESOLUTIONS.get(alias)
        if alias.endswith(("-480p", "-1080p", "-4k"))
        else None
    )
    if fixed and resolution != fixed:
        raise ValueError("模型名中的分辨率与 resolution 冲突")
    ratio = str(p.get("aspect_ratio") or p.get("ratio") or "16:9")
    ratios = enum_values(props["aspectRatio"])
    if ratio not in ratios and ratio in ("auto", "adaptive"):
        ratio = "auto" if "auto" in ratios else "adaptive"
    p.update(
        kind="video",
        model=alias,
        upstream_model=key,
        generation_mode=mode,
        prompt=normalize_prompt(
            prompt, media, settings.prompt_media_reference_cleanup_enabled
        )
        if mode == "reference"
        else prompt,
        original_prompt=prompt,
        duration=int(p.get("duration", p.get("seconds", 5))),
        resolution=resolution,
        aspect_ratio=ratio,
        n=int(p.get("n", p.get("num_outputs", 1))),
        _images=media["image"],
        _videos=media["video"],
        _audio=media["audio"],
        _recipe=recipe,
    )
    validate_parameters(p, manifest)
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
