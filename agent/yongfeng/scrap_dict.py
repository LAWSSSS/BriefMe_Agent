"""永锋废钢料型字典（检判原图命名 / 打包用）。

现场核对自 vision.lg.china-yongfeng.com/srape-steel：steelType 与同产品
「睿视废钢」编码同族（已见 0/1/2）。本文件自维护，禁止 import agent.shenglong.dict。
烧结矿颗粒度字典在同包 dict.py，不要混用。
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

STEEL_TYPE: dict[int, str] = {
    0: "",
    1: "重废1",
    2: "重废2",
    3: "重废3",
    4: "剪料1",
    5: "剪料2",
    6: "剪料3",
    7: "剪料4",
    8: "破碎料1",
    9: "破碎料2",
    10: "破碎料3",
    11: "中废",
    12: "生铁",
    13: "厚剪",
    14: "钢筋切粒",
    15: "汽车壳",
    16: "超标",
}

STEEL_TYPE_EN: dict[int, str] = {
    0: "",
    1: "zhongfei1",
    2: "zhongfei2",
    3: "zhongfei3",
    4: "jianliao1",
    5: "jianliao2",
    6: "jianliao3",
    7: "jianliao4",
    8: "posuiliao1",
    9: "posuiliao2",
    10: "posuiliao3",
    11: "medium",
    12: "shengtie",
    13: "houjian",
    14: "gangjinqieli",
    15: "qicheke",
    16: "chaobiao",
}

REFERENCE_TYPES: frozenset[int] = frozenset({16})
EMPTY_TYPES: frozenset[int] = frozenset({0})

MATERIAL_PRIORITY: dict[int, int] = {
    1: 1,
    2: 2,
    3: 3,
    12: 4,
    11: 5,
    13: 6,
    4: 7,
    5: 8,
    6: 9,
    7: 10,
    8: 11,
    9: 12,
    10: 13,
    14: 14,
    15: 15,
}


def get_material_en(steel_type: Optional[int]) -> str:
    if steel_type is None:
        return "unknown"
    code = int(steel_type)
    if code in EMPTY_TYPES:
        return "unknown"
    return STEEL_TYPE_EN.get(code, f"unknown{code}")


def get_material_name(steel_type: Optional[int]) -> str:
    if steel_type is None:
        return "--"
    code = int(steel_type)
    if code in EMPTY_TYPES:
        return "--"
    return STEEL_TYPE.get(code, f"未知{code}")


def is_valid(steel_type: Optional[int]) -> bool:
    if steel_type is None:
        return False
    code = int(steel_type)
    if code in EMPTY_TYPES or code in REFERENCE_TYPES:
        return False
    return code in STEEL_TYPE


def filter_main_candidates(
    items: Iterable[Tuple[Optional[int], float]],
) -> List[Tuple[int, float]]:
    out: List[Tuple[int, float]] = []
    for st, rate in items:
        if st is None:
            continue
        code = int(st)
        if code in EMPTY_TYPES or code in REFERENCE_TYPES:
            continue
        if code not in STEEL_TYPE:
            continue
        try:
            out.append((code, float(rate)))
        except (TypeError, ValueError):
            continue
    return out
