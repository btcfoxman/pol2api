import pytest

from app.task_errors import MESSAGE_VARIANTS, classify_failure, public_failure


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
