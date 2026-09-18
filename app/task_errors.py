"""Public failure categories, independent of submission outcome and billing."""

from __future__ import annotations

import math
import re
from typing import Any


MESSAGES = {
    "UPSTREAM_MAINTENANCE": "上游维护中，请稍后再试~",
    "QUEUE_INTERRUPTED": "队列排队服务中断，请稍后再试~",
    "MEDIA_DURATION_UNSUPPORTED": "素材时长不支持，请修改后再试~",
    "MEDIA_LIMIT_EXCEEDED": "素材超限，请修改后再试~",
    "MEDIA_FORMAT_UNSUPPORTED": "素材格式不支持，请修改后再试~",
    "MEDIA_DOWNLOAD_FAILED": "素材下载失败，请检查素材链接后重试~",
    "MEDIA_EXTERNAL_URL_REQUIRED": "素材仅支持外链，暂不支持文件流、Base64等~",
}
REFUND_MESSAGES = {
    "REAL_PERSON_DETECTED": "参考图片中检测到可能存在真人，暂不支持，请更换图片后重试，积分已返还~",
    "INPUT_IMAGE_REAL_PERSON": "检测到输入图片可能包含真人，生成失败，请修改后重试，积分已返还~",
    "OUTPUT_MODERATION_FAILED": "生成的视频内容违规，请修改描述后重试，积分已返还~",
    "TEXT_MODERATION_FAILED": "检测到文本有敏感或违规内容，积分已返还，请重试~",
    "IMAGE_MODERATION_FAILED": "检测到图片有敏感或违规内容，积分已返还，请重试~",
    "VIDEO_MODERATION_FAILED": "检测到视频有敏感或违规内容，请修改后重试，积分已返还～",
    "CONTENT_MODERATION_FAILED": "检测到内容有敏感或违规情况，请修改后重试，积分已返还～",
    "GENERATION_FAILED": "生成失败，积分已返还，请重试~",
}
# Preserve supplied wording when upstream already returns one of the approved variants.
MESSAGE_VARIANTS = {
    **{v: k for k, v in MESSAGES.items()},
    **{v: k for k, v in REFUND_MESSAGES.items()},
    "文字违规！请重试，积分已返还～": "TEXT_MODERATION_FAILED",
    "视频违规！请重试，积分已返还～": "VIDEO_MODERATION_FAILED",
    "文字违规！请修改后重试，积分已返还～": "TEXT_MODERATION_FAILED",
    "检测到内容有敏感或违规情况，积分已返还，请重试~": "CONTENT_MODERATION_FAILED",
    "图片违规，请修改后重试~": "IMAGE_MODERATION_FAILED",
}

# Observed terminal Pollo responses with null numeric refund fields. Accept an
# explicit receipt only when one failed output and all parent records agree.
COPYRIGHT_REFUND_MESSAGE = (
    "This output was flagged for potential copyright issues. "
    "Please try a different prompt. Credits refunded."
)
SENSITIVE_INPUT_REFUND_MESSAGE = (
    "Sensitive input flagged by the third-party model. "
    "Please modify your input. Credits refunded."
)
MODERATION_RECEIPTS = {
    "3000": (SENSITIVE_INPUT_REFUND_MESSAGE, "CONTENT_MODERATION_FAILED"),
    "3008": (COPYRIGHT_REFUND_MESSAGE, "OUTPUT_MODERATION_FAILED"),
}


def message_for(category: str, refunded: bool = False, original: str = "") -> str:
    message = (
        original
        if MESSAGE_VARIANTS.get(original) == category
        else (
            MESSAGES.get(category)
            or REFUND_MESSAGES.get(category, REFUND_MESSAGES["GENERATION_FAILED"])
        )
    )
    return message if refunded is True else message.replace("，积分已返还", "")


def classify_failure(code: str = "", message: str = "", *, stage: str = "") -> str:
    raw_code = str(code or "")
    code, message = raw_code.upper(), str(message or "").strip()
    if message in MESSAGE_VARIANTS:
        return MESSAGE_VARIANTS[message]
    if code in MESSAGES or (code in REFUND_MESSAGES and code != "GENERATION_FAILED"):
        return code
    if code == "3008":
        return "OUTPUT_MODERATION_FAILED"
    observed = MODERATION_RECEIPTS.get(code)
    if observed and message == observed[0]:
        return observed[1]
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw_code + " " + message)
    text = re.sub(r"[_-]+", " ", text).lower()
    # Output review takes priority even when its message also mentions input media.
    moderation = bool(
        re.search(
            r"moderation|sensitive\s*(?:content|input)|content.*(?:violat|reject)|policy[. ]*violation"
            r"|violat.*(?:policy|safety)|safety (?:check|filter)|nsfw|违规|敏感|审核.{0,8}(?:失败|拒绝)"
            r"|copyright.{0,30}(?:issues?|restrictions?|violations?)|版权.{0,15}(?:问题|违规|限制)",
            text,
        )
    )
    if moderation and re.search(
        r"(?:output|generated)\s+(?:video|audio)|\b(?:this|the) output\b|生成的视频",
        text,
    ):
        return "OUTPUT_MODERATION_FAILED"
    if re.search(
        r"real (?:person|people|human)|真人|human face|face.{0,15}not (?:allowed|supported)",
        text,
    ):
        return (
            "INPUT_IMAGE_REAL_PERSON"
            if re.search(r"input image|输入图片", text)
            else "REAL_PERSON_DETECTED"
        )
    if moderation:
        for category, pattern in (
            ("TEXT", r"\b(?:text|prompt)\b|文本|文字"),
            ("IMAGE", r"\b(?:image|picture)\b|图片|图像"),
            ("VIDEO", r"\bvideo\b|视频"),
        ):
            if re.search(pattern, text):
                return category + "_MODERATION_FAILED"
        return "CONTENT_MODERATION_FAILED"
    if re.search(r"queue.{0,40}(?:interrupt|unavailable|shutdown)|排队服务中断", text):
        return "QUEUE_INTERRUPTED"
    if re.search(
        r"(?:media|reference|video|audio).*duration.*(?:exceed|unsupported|invalid|between|long|short)"
        r"|duration must be between|素材时长|参考素材时长",
        text,
    ):
        return "MEDIA_DURATION_UNSUPPORTED"
    if re.search(
        r"素材.*(?:超限|过大|大小|数量上限)|最多支持.*素材|参考素材尺寸|像素总数"
        r"|(?:file|image|video|audio|media).*(?:too large|size limit|exceeds.{0,30}(?:mb|gb|bytes|limit))"
        r"|too many (?:images|videos|audio|references)",
        text,
    ):
        return "MEDIA_LIMIT_EXCEEDED"
    if re.search(
        r"素材仅支持外链|media must be an https?\(?s?\)? url|external url required",
        text,
    ):
        return "MEDIA_EXTERNAL_URL_REQUIRED"
    if re.search(
        r"素材下载失败|(?:media|asset|image|video).{0,25}download.{0,20}(?:fail|error)",
        text,
    ):
        return "MEDIA_DOWNLOAD_FAILED"
    if re.search(
        r"素材格式|无法解析音视频|没有 .* 流|参考视频帧率|参考素材画幅"
        r"|unsupported (?:media|image|video|audio|file) (?:format|type)|invalid image|cannot identify image",
        text,
    ):
        return "MEDIA_FORMAT_UNSUPPORTED"
    if (
        code
        in {
            "UPLOAD_FAILED",
            "UPLOAD_REJECTED",
            "UPSTREAM_HTTP_ERROR",
            "RATE_LIMITED",
            "NETWORK_ERROR",
            "AUTH_REQUIRED",
            "TOO_MANY_REQUESTS",
            "ACCOUNT_RESTRICTED",
            "NO_ACCOUNT",
            "QUEUE_FULL",
        }
        or re.search(
            r"maintenance|temporarily unavailable|上游维护|upload.{0,40}(?:reject|denied|forbidden|fail)",
            text,
        )
        or (stage.startswith("upload") and code not in {"MEDIA_DOWNLOAD_FAILED"})
    ):
        return "UPSTREAM_MAINTENANCE"
    return "GENERATION_FAILED"


def failure_diagnostic(detail: Any) -> str:
    if not isinstance(detail, dict):
        return ""
    values = []
    for key in ("failMsg", "failCode", "errorCode", "errorMessage", "message"):
        if isinstance(detail.get(key), (str, int)):
            values.append(str(detail[key]))
    error = detail.get("error")
    if isinstance(error, str):
        values.append(error)
    elif isinstance(error, dict):
        values.extend(
            str(error[k])
            for k in ("code", "message")
            if isinstance(error.get(k), (str, int))
        )
    for item in detail.get("generations") or []:
        values.append(failure_diagnostic(item))
    # Some failed records carry the only reason on the parent generation record.
    if isinstance(detail.get("generateRecord"), dict):
        values.append(failure_diagnostic(detail["generateRecord"]))
    return " ".join(dict.fromkeys(v for v in values if v))[:2000]


def refund_receipt(task: dict[str, Any]) -> dict[str, Any]:
    # A released local reservation or an uncharged rejection is not an upstream refund.
    if task.get("error_code") != "GENERATION_FAILED" or not task.get("generation_id"):
        return {}
    detail = (
        (task.get("upstream_response") or {}).get("detail")
        or task.get("raw_status")
        or {}
    )
    if not isinstance(detail, dict):
        return {}
    try:
        record = detail.get("generateRecord") or {}
        raw_charge = record.get("creditDecimal")
        raw_charge = task.get("estimated_cost") if raw_charge is None else raw_charge
        if isinstance(raw_charge, bool):
            return {}
        charged = float(raw_charge or 0)
        if not math.isfinite(charged) or charged <= 0:
            return {}
        raw_refund = detail.get("refundCreditDecimal")
        if raw_refund is not None:
            if isinstance(raw_refund, bool):
                return {}
            amount = float(raw_refund)
            if not math.isfinite(amount) or amount < 0:
                return {}
            return {
                "credits": min(amount, charged),
                "charged": charged,
                "source": "refundCreditDecimal",
            }
        outputs = detail.get("generations") or []
        request = task.get("request") or {}
        receipt_code = str(detail.get("failCode"))
        expected = MODERATION_RECEIPTS.get(receipt_code)
        if (
            expected is not None
            and isinstance(outputs, list)
            and len(outputs) == 1
            and request.get("n", 1) == 1
            and str(record.get("id") or task["generation_id"])
            == str(task["generation_id"])
            and all(
                isinstance(row, dict)
                and row.get("status") == "failed"
                and str(row.get("failCode")) == receipt_code
                and row.get("failMsg") == expected[0]
                for row in (detail, record, outputs[0])
            )
            and outputs[0].get("refundCreditDecimal") is None
        ):
            return {
                "credits": charged,
                "charged": charged,
                "source": "upstream_failure_message",
            }
    except (ValueError, TypeError, AttributeError):
        return {}
    return {}


def refund_confirmed(task: dict[str, Any]) -> bool:
    receipt = refund_receipt(task)
    return bool(receipt and receipt["credits"] >= receipt["charged"])


def public_failure(task: dict[str, Any]) -> dict[str, Any]:
    audit = task.get("upstream_response") or {}
    diagnostic = audit.get("failure") or {}
    original = str(diagnostic.get("message") or task.get("error_message") or "")
    category = classify_failure(
        task.get("error_code", ""), original, stage=str(diagnostic.get("stage") or "")
    )
    # Failed generation details may contain the specific code below a generic failMsg.
    if category == "GENERATION_FAILED":
        detail = audit.get("detail") or task.get("raw_status") or {}
        category = classify_failure(
            detail.get("failCode", "") if isinstance(detail, dict) else "",
            failure_diagnostic(detail),
        )
    refunded = refund_confirmed(task)
    code = task.get("error_code") or "GENERATION_FAILED"
    result = {
        "code": code,
        "category": category,
        "message": message_for(category, refunded, original),
        "outcome": "failed"
        if code == "GENERATION_FAILED"
        else "unknown"
        if task.get("generation_id") or code == "SUBMISSION_UNKNOWN"
        else "rejected",
        "refunded": refunded,
    }
    if refunded:
        result["refund_source"] = refund_receipt(task)["source"]
    return result
