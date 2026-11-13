"""用户原文优先的可选工具副作用策略。"""
from __future__ import annotations

import re
from typing import Any


def requested_scrap_error_images(message: str) -> bool:
    """按本次用户原文判断是否要下载镔鑫错判图，不沿用快捷指令模板。"""
    text = re.sub(r"\s+", "", str(message or "").lower())
    if not text:
        return False

    negative_patterns = (
        r"(不要|不需要|无需|不用|别|不含|不附带|不下载).{0,10}(错判|判错|渲染图|图片)",
        r"(错判|判错|渲染图|图片).{0,10}(不要|不需要|无需|不用|别|不含|不附带)",
    )
    if any(re.search(pattern, text) for pattern in negative_patterns):
        return False

    image_terms = ("错判图", "判错图", "错判渲染图", "判错渲染图", "渲染图", "错判图片")
    actions = ("下载", "导出", "保存", "附带", "包含", "一起", "同时", "也要", "还要", "给我")
    return any(term in text for term in image_terms) and any(
        action in text for action in actions
    )


def resolve_scrap_download_images(user_message: str, supplied_value: Any) -> bool:
    """用户原文优先；没有原文时才使用模型显式传入的值。"""
    if str(user_message or "").strip():
        return requested_scrap_error_images(user_message)
    if supplied_value is None:
        return False
    if isinstance(supplied_value, str):
        return supplied_value.strip().lower() in {"1", "true", "yes", "y", "是"}
    return bool(supplied_value)
