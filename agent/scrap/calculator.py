"""废钢检判业务计算

单车指标：
  - 主料型一致性
  - 差异率（主料型一致=ABS差/人工%，不一致=100%）
  - 扣重差值 ABS(人工kg - 赛迪kg)
  - 扣重占比 赛迪kg / 人工kg

日汇总：
  - 准确率 = 主料型一致车数 / 参与统计车数
  - 平均误差率 = 参与车差异率平均
  - 平均扣重差值、平均扣重占比

过滤：人工主料型 steelType ∈ {2,4} 的车次不参与统计，但保留在表格
人工缺失：不参与统计，保留在表格
"""
from __future__ import annotations

import logging
from typing import List, Optional

from agent.scrap.dict import is_same_material
from agent.scrap.models import DailyScrapStats, MaterialEntry, TruckStat
from agent.scrap.parser import (
    parse_ai_materials,
    parse_manual_results,
    pick_main,
    safe_float,
)

logger = logging.getLogger(__name__)


def calc_truck(
    date_str: str,
    car_number: str,
    station_number: int | str,
    flow_code: str,
    detail_data: dict,
    exclude_steel_types: tuple = (2, 4),
) -> TruckStat:
    """对一辆车的详情 data 做完整计算。

    Args:
        detail_data: getCheckDetail 返回的 data 字段（含 manualCheck、steelTypeRateDTOList 等）
    """
    stat = TruckStat(
        date=date_str,
        car_number=car_number,
        station_number=station_number,
        flow_code=flow_code,
    )

    manual_check = detail_data.get("manualCheck") or {}
    manual_text = manual_check.get("manualResults") or ""
    stat.manual_materials = parse_manual_results(manual_text)
    stat.manual_main = pick_main(stat.manual_materials)
    stat.manual_deduct_kg = safe_float(manual_check.get("deductionResults"))

    stat.ai_materials = parse_ai_materials(detail_data.get("steelTypeRateDTOList"))
    stat.ai_main = pick_main(stat.ai_materials)

    deduct_dto = detail_data.get("deductCalculationResultDTO") or {}
    stat.ai_deduct_kg = safe_float(
        deduct_dto.get("calculatedDeductWeight")
        if deduct_dto.get("calculatedDeductWeight") is not None
        else deduct_dto.get("finalDeduct")
    )

    manual_missing = (
        stat.manual_main is None
        or not stat.manual_materials
        or stat.manual_deduct_kg is None
    )

    manual_type = stat.manual_main.steel_type if stat.manual_main else None
    stat.is_eligible = (
        not manual_missing and manual_type not in exclude_steel_types
    )

    if stat.manual_main and stat.ai_main:
        same = is_same_material(
            (stat.manual_main.steel_type, stat.manual_main.steel_level),
            (stat.ai_main.steel_type, stat.ai_main.steel_level),
        )
        stat.main_same = same
        if same:
            p_man = stat.manual_main.rate
            p_ai = stat.ai_main.rate
            if p_man > 0:
                stat.diff_rate = abs(p_man - p_ai) / p_man * 100.0
            else:
                stat.diff_rate = 100.0 if p_ai > 0 else 0.0
        else:
            stat.diff_rate = 100.0

    if stat.manual_deduct_kg is not None and stat.ai_deduct_kg is not None:
        stat.weight_diff = abs(stat.manual_deduct_kg - stat.ai_deduct_kg)
        if stat.manual_deduct_kg != 0:
            stat.weight_ratio = stat.ai_deduct_kg / stat.manual_deduct_kg
        else:
            stat.weight_ratio = None

    if stat.main_same is False:
        one_check_list = detail_data.get("oneCheckSummaryDTOList") or []
        for o in one_check_list:
            url = o.get("deductWeightUrl") or o.get("originImageUrl")
            if url:
                stat.error_render_images.append(url)

    return stat


def aggregate_daily(date_str: str, trucks: List[TruckStat]) -> DailyScrapStats:
    """把一天的多辆车聚合为日汇总"""
    stats = DailyScrapStats(date=date_str, trucks=trucks)
    stats.total_trucks = len(trucks)

    eligible = [t for t in trucks if t.is_eligible]
    stats.eligible_trucks = len(eligible)

    if not eligible:
        return stats

    stats.main_same_count = sum(1 for t in eligible if t.main_same is True)

    diff_rates = [t.diff_rate for t in eligible if t.diff_rate is not None]
    if diff_rates:
        stats.avg_error_rate = sum(diff_rates) / len(diff_rates)

    weight_diffs = [t.weight_diff for t in eligible if t.weight_diff is not None]
    if weight_diffs:
        stats.avg_weight_diff = sum(weight_diffs) / len(weight_diffs)

    ratios = [t.weight_ratio for t in eligible if t.weight_ratio is not None]
    if ratios:
        stats.avg_weight_ratio = sum(ratios) / len(ratios)

    return stats
