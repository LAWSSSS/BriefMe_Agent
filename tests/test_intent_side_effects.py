from agent.intent_policy import (
    requested_scrap_error_images,
    resolve_scrap_download_images,
)


def test_report_only_does_not_download_error_images():
    message = "导出 2026-10-26 到 2026-11-02 的【镔鑫】废钢检判报表"

    assert requested_scrap_error_images(message) is False
    assert resolve_scrap_download_images(message, True) is False


def test_explicit_error_images_enables_download_even_if_model_omits_it():
    message = (
        "导出 2026-10-26 到 2026-11-02 的【镔鑫】废钢检判报表并下载错判图"
    )

    assert requested_scrap_error_images(message) is True
    assert resolve_scrap_download_images(message, None) is True


def test_negative_image_instruction_overrides_image_keyword():
    message = "导出【镔鑫】废钢检判报表，报表不需要错判图"

    assert requested_scrap_error_images(message) is False
    assert resolve_scrap_download_images(message, True) is False


def test_report_and_error_images_enables_download():
    message = "导出【镔鑫】废钢检判报表和错判图"

    assert requested_scrap_error_images(message) is True


def test_fallback_uses_explicit_tool_argument_without_user_message():
    assert resolve_scrap_download_images("", True) is True
    assert resolve_scrap_download_images("", False) is False
    assert resolve_scrap_download_images("", None) is False
