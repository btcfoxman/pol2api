"""Recipe-specific constraints from captured server manifests."""

from copy import deepcopy
import json
import math
from pathlib import Path

CATALOG = json.loads(
    (Path(__file__).parent / "recipe_manifests.json").read_text(encoding="utf-8")
)
RECIPES = {}
for entry in CATALOG["entries"]:
    RECIPES.setdefault(entry["model"], {})[entry["recipe"]] = entry["manifest"]

PARAMETERS = {
    "duration": "duration",
    "resolution": "resolution",
    "aspect_ratio": "aspectRatio",
    "n": "numOutputs",
    "generate_audio": "generateAudio",
    "seed": "seed",
    "mode": "mode",
    "web_search": "webSearch",
    "published": "published",
    "protection_mode": "protectionMode",
}


def enum_values(prop):
    return [
        item["value"] if isinstance(item, dict) else item
        for item in prop.get("enum", [])
    ]


def duration_values(props):
    rule = props["duration"]
    return enum_values(rule) or list(range(rule["minimum"], rule["maximum"] + 1))


def media_rules(manifest, recipe):
    props = manifest["schema"]["properties"]
    if recipe == "ref2video":
        rules = deepcopy(props["refs"]["x-ui-config"])
        for kind in ("image", "video", "audio"):
            rules["upload"].setdefault(kind, {"maxNum": 0})
        return rules
    count = int("image" in props) + int("imageTail" in props)
    image = deepcopy(
        props.get("image", {}).get("x-ui-config", {}).get("upload", {}).get("image", {})
    )
    image["maxNum"] = count
    return {
        "maxItems": count,
        "upload": {"image": image, "video": {"maxNum": 0}, "audio": {"maxNum": 0}},
    }


def recipe_capabilities(manifest, recipe):
    props = manifest["schema"]["properties"]
    limits = media_rules(manifest, recipe)
    resolutions = enum_values(props["resolution"])
    return dict(
        durations=duration_values(props),
        resolutions=resolutions,
        default_resolution="720p"
        if "720p" in resolutions
        else props["resolution"].get("default", resolutions[0]),
        aspect_ratios=enum_values(props["aspectRatio"]),
        media_limits={
            "images": limits["upload"]["image"]["maxNum"],
            "videos": limits["upload"]["video"]["maxNum"],
            "audio": limits["upload"]["audio"]["maxNum"],
        },
        max_total_references=limits["maxItems"],
        generate_audio="generateAudio" in props,
        max_outputs=props["numOutputs"].get("maximum", 1),
        parameters={
            name: {k: v for k, v in props[field].items() if k != "x-ui-config"}
            for name, field in PARAMETERS.items()
            if field in props
        },
    )


def validate_parameters(values, manifest):
    props = manifest["schema"]["properties"]
    for name, field in PARAMETERS.items():
        if name not in values or values[name] is None:
            continue
        if field not in props:
            raise ValueError(f"该模型和生成模式不支持 {name}")
        value, rule = values[name], props[field]
        kind = rule.get("type")
        if kind == "boolean" and not isinstance(value, bool):
            raise ValueError(f"{name} 必须为布尔值")
        if kind in ("integer", "number"):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} 必须为有限数值")
            if kind == "integer" and value != int(value):
                raise ValueError(f"{name} 必须为整数")
            if (
                not rule.get("minimum", -math.inf)
                <= value
                <= rule.get("maximum", math.inf)
            ):
                raise ValueError(f"{name} 超出模型范围")
        if kind == "string" and not isinstance(value, str):
            raise ValueError(f"{name} 必须为字符串")
        allowed = enum_values(rule)
        if allowed and value not in allowed:
            raise ValueError(f"该模型和生成模式不支持 {name}={value}")
    prompt = values.get("prompt", "")
    if not prompt.strip() or len(prompt) > props["prompt"].get("maxLength", 10000):
        raise ValueError("提示词为空或超过该模型的长度限制")
    model = values["upstream_model"]
    if (
        model == "pollo-v3-0"
        and values.get("mode", "basic") == "basic"
        and values["resolution"] in ("1080p", "4K")
    ):
        raise ValueError("Pollo 3.0 的 1080p/4K 需要 mode=pro")
    if (
        model == "pollo-v2-5"
        and values["duration"] == 15
        and values.get("generate_audio", True) is False
    ):
        raise ValueError("Pollo 2.5 的 15 秒视频要求 generate_audio=true")


def input_values(payload, manifest):
    validate_parameters(payload, manifest)
    props = manifest["schema"]["properties"]
    result = {
        name: value["default"] for name, value in props.items() if "default" in value
    }
    result.update(manifest.get("initialValues", {}))
    for name, field in PARAMETERS.items():
        if payload.get(name) is not None:
            result[field] = payload[name]
    result.update(model=payload["upstream_model"], prompt=payload["prompt"])
    return result
