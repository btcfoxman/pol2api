import pytest

from app.task_errors import (
    COPYRIGHT_REFUND_MESSAGE,
    MESSAGE_VARIANTS,
    SENSITIVE_INPUT_REFUND_MESSAGE,
    classify_failure,
    public_failure,
    refund_receipt,
)


def task(message, refund=None, charged="12", **extra):
    return {
        "error_code": "GENERATION_FAILED",
        "generation_id": "123",
        "error_message": message,
        "estimated_cost": 12,
        "raw_status": {
            "refundCreditDecimal": refund,
            "generateRecord": {"creditDecimal": charged},
        },
        **extra,
    }


@pytest.mark.parametrize("message,category", MESSAGE_VARIANTS.items())
def test_approved_variants_keep_exact_wording_only_with_refund(message, category):
    result = public_failure(task(message, "12"))
    assert result["category"] == category
    assert result["message"] == message
    assert result["outcome"] == "failed"
    result = public_failure(task(message))
    assert result["message"] == message.replace("，积分已返还", "")
    assert result["refunded"] is False


@pytest.mark.parametrize("refund", [None, "0", "6", "NaN", "Infinity", "bad", True])
def test_partial_or_missing_refunds_are_not_reported_as_full(refund):
    result = public_failure(task("InputImageSensitiveContentDetected", refund))
    assert result["category"] == "IMAGE_MODERATION_FAILED"
    assert result["refunded"] is False
    assert "积分已返还" not in result["message"]


@pytest.mark.parametrize(
    "message,expected",
    [
        ("InputTextSensitiveContentDetected", "TEXT_MODERATION_FAILED"),
        ("InputImageSensitiveContentDetected", "IMAGE_MODERATION_FAILED"),
        ("InputVideoSensitiveContentDetected", "VIDEO_MODERATION_FAILED"),
        (
            "OutputVideoSensitiveContentDetected (input image provided)",
            "OUTPUT_MODERATION_FAILED",
        ),
        ("Input image contains a real person", "INPUT_IMAGE_REAL_PERSON"),
        ("Reference image contains real people", "REAL_PERSON_DETECTED"),
        ("Image content rejected by moderation", "IMAGE_MODERATION_FAILED"),
        ("queue service interrupted", "QUEUE_INTERRUPTED"),
        ("参考素材时长超出模型范围", "MEDIA_DURATION_UNSUPPORTED"),
        ("File exceeds the 10 MB images limit", "MEDIA_LIMIT_EXCEEDED"),
        ("model 最多支持 9 个 image 素材", "MEDIA_LIMIT_EXCEEDED"),
        ("Unsupported image format", "MEDIA_FORMAT_UNSUPPORTED"),
        ("image download failed", "MEDIA_DOWNLOAD_FAILED"),
        ("Upload was rejected", "UPSTREAM_MAINTENANCE"),
        ("upstream undergoing maintenance", "UPSTREAM_MAINTENANCE"),
    ],
)
def test_classify_observed_protocol_and_validation_shapes(message, expected):
    assert classify_failure("", message) == expected


def test_structured_camel_case_code_is_not_lost():
    assert (
        classify_failure("InputImageSensitiveContentDetected", "Generation failed")
        == "IMAGE_MODERATION_FAILED"
    )


def test_category_cannot_turn_uncertain_submission_into_safe_rejection():
    result = public_failure(
        task("素材超限，请修改后再试~", error_code="SUBMISSION_UNKNOWN")
    )
    assert result["category"] == "MEDIA_LIMIT_EXCEEDED"
    assert result["outcome"] == "unknown"
    result = public_failure(task("temporary outage", error_code="TIMEOUT"))
    assert result["outcome"] == "unknown"


def test_rejected_upload_is_not_a_refund_or_login_failure():
    result = public_failure(
        {
            "error_code": "FORBIDDEN",
            "upstream_response": {
                "failure": {"message": "Denied", "stage": "upload_complete"}
            },
        }
    )
    assert result == {
        "code": "FORBIDDEN",
        "category": "UPSTREAM_MAINTENANCE",
        "message": "上游维护中，请稍后再试~",
        "outcome": "rejected",
        "refunded": False,
    }


def test_nested_generation_error_is_used_without_reading_prompt():
    result = public_failure(
        task(
            "Generation failed",
            "12",
            upstream_response={
                "detail": {
                    "refundCreditDecimal": "12",
                    "generateRecord": {"creditDecimal": "12"},
                    "generations": [{"failMsg": "InputImageSensitiveContentDetected"}],
                    "prompt": "OutputVideoSensitiveContentDetected",
                }
            },
        )
    )
    assert result["category"] == "IMAGE_MODERATION_FAILED"
    assert result["refunded"] is True


def copyright_task():
    failed = {
        "status": "failed",
        "failCode": 3008,
        "failMsg": COPYRIGHT_REFUND_MESSAGE,
        "refundCreditDecimal": None,
    }
    return task(
        COPYRIGHT_REFUND_MESSAGE,
        "0",
        charged="96",
        request={"n": 1},
        upstream_response={
            "detail": {
                **failed,
                "generateRecord": {**failed, "id": "123", "creditDecimal": "96"},
                "generations": [dict(failed)],
            },
        },
    )


def sensitive_input_task():
    value = copyright_task()
    value["error_message"] = "生成失败，请重试~"
    detail = value["upstream_response"]["detail"]
    for row in (detail, detail["generateRecord"], detail["generations"][0]):
        row.update(failCode=3000, failMsg=SENSITIVE_INPUT_REFUND_MESSAGE)
    return value


def test_sensitive_input_receipt_does_not_guess_which_media_was_flagged():
    value = sensitive_input_task()
    value["request"].update(image_urls=["https://example.org/frame.jpg"] * 4)
    result = public_failure(value)
    assert result["category"] == "CONTENT_MODERATION_FAILED"
    assert result["message"] == "检测到内容有敏感或违规情况，请修改后重试，积分已返还～"
    assert result["refunded"] is True
    assert result["refund_source"] == "upstream_failure_message"
    assert refund_receipt(value)["credits"] == 96
    assert (
        classify_failure("GENERATION_FAILED", SENSITIVE_INPUT_REFUND_MESSAGE)
        == result["category"]
    )
    assert classify_failure("3000", "Unknown generation failure") == "GENERATION_FAILED"


def test_observed_copyright_failure_keeps_output_scope_and_explicit_receipt():
    result = public_failure(copyright_task())
    assert result["category"] == "OUTPUT_MODERATION_FAILED"
    assert result["message"] == "生成的视频内容违规，请修改描述后重试，积分已返还~"
    assert result["outcome"] == "failed" and result["refunded"] is True
    assert result["refund_source"] == "upstream_failure_message"
    assert refund_receipt(copyright_task())["credits"] == 96


@pytest.mark.parametrize(
    "message,category",
    [
        (COPYRIGHT_REFUND_MESSAGE, "OUTPUT_MODERATION_FAILED"),
        (
            "The generated video may be related to copyright restrictions and has been blocked.",
            "OUTPUT_MODERATION_FAILED",
        ),
        ("Input image contains copyright violations", "IMAGE_MODERATION_FAILED"),
        ("Text contains copyright violations", "TEXT_MODERATION_FAILED"),
    ],
)
def test_copyright_moderation_does_not_default_to_generic_failure(message, category):
    assert classify_failure("GENERATION_FAILED", message) == category


@pytest.mark.parametrize("factory", [copyright_task, sensitive_input_task])
@pytest.mark.parametrize(
    "field,value",
    [
        ("refundCreditDecimal", "0"),
        ("refundCreditDecimal", "24"),
        ("refundCreditDecimal", "bad"),
        ("status", "processing"),
        ("failCode", 9999),
        ("failMsg", "Credits will be refunded."),
        ("failMsg", "Credits refunded pending review."),
    ],
)
def test_text_receipt_cannot_override_conflicting_or_uncertain_evidence(
    factory, field, value
):
    value_task = factory()
    value_task["upstream_response"]["detail"][field] = value
    error = public_failure(value_task)
    assert error["refunded"] is False
    assert "积分已返还" not in error["message"]


@pytest.mark.parametrize("factory", [copyright_task, sensitive_input_task])
def test_text_receipt_is_not_inferred_from_client_error_message_or_multi_output(
    factory,
):
    value_task = factory()
    value_task["upstream_response"]["detail"]["generations"] *= 2
    assert public_failure(value_task)["refunded"] is False
    value_task = factory()
    value_task["request"]["n"] = 2
    assert public_failure(value_task)["refunded"] is False
    value_task = factory()
    value_task["error_code"] = "SUBMISSION_UNKNOWN"
    assert public_failure(value_task)["refunded"] is False
    assert public_failure(value_task)["outcome"] == "unknown"
    value_task = task(COPYRIGHT_REFUND_MESSAGE)
    assert public_failure(value_task)["refunded"] is False


def test_copyright_code_without_english_message_is_classified():
    value_task = task("Unknown")
    value_task["raw_status"]["failCode"] = 3008
    assert public_failure(value_task)["category"] == "OUTPUT_MODERATION_FAILED"
