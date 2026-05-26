"""盛隆废钢检判业务计算

关键规则（已和用户确认）：
  · 主料型正确：**料型名字一致 AND 差异值 ≤ 10%**（差异值任一侧超 10% 算不正确）
  · 差异值(%)：主料名字一致时 = abs(人工主料占比 - AI 对应料型占比)；否则 /
  · 扣重比值：AI/人工（AI、人工单位已统一到吨）
  · 扣重误差：|AI-人工|（取绝对值）
  · 单价差异：|AI-人工|（AI 目前无输出 → None）
  · 扣杂准确：0.5≤比值≤1.5 OR |误差|≤0.15 吨
  · 识别率 R：主料正确 / 可判定车数（双方主料非空） → 目标 ≥92%
  · 扣杂符合率：扣杂准确 / 可评估扣杂车数 → 目标 ≥90%

人工合并口径（关键）：
  · 不直接使用后端 `manualCheckResultVO.avgResult`，而是基于 `checkDetails`
    剔除黑名单后的 operator 重新合并。
  · 黑名单：`agent.shenglong.dict.EXCLUDED_OPERATORS`
    （施宏波 / 冉星明 / 周倩 / 王宇泰 / 王重阳）
  · 合并规则（与后端 avgResult 口径保持一致）：
      manual_rate(steel_type) = sum(operator.rate(steel_type)) / N
        其中 N = 剔除黑名单后的人数；某个 operator 没判某料型则按 0 计入
      manual_deduct_ton = sum(op.deduction_ton 非空) / 非空人数
      manual_steel_price = sum(op.steel_price 非空) / 非空人数
  · 剔除后剩 0 人 → 整辆车人工缺失（manual_main = None） → 不计入任何统计

单位换算：
  · AI totalDeductWeight 单位 kg → /1000 转吨
  · 人工 avgDeduction 已是吨
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Dict, List, Optional, Tuple

from agent.shenglong.dict import (
    EMPTY_TYPES,
    HEAVY_STEEL_TYPES,
    REFERENCE_TYPES,
    STEEL_TYPE_PRICE,
    filter_main_candidates,
    get_main_type_from_list,
    is_excluded_operator,
)
from agent.shenglong.models import (
    DailyShenglongStats,
    ManualOperator,
    MaterialRate,
    TruckStat,
)
from config.settings import ShenglongConfig
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from agent.shenglong.models import PeriodSummary

logger = logging.getLogger(__name__)

# 主料型正确的差异值容忍度（%）：料型一致且 |人工占比-AI占比| ≤ 该值 才算主料正确
MAIN_TYPE_DIFF_TOLERANCE: float = 10.0


def _parse_rate_list(raw_list) -> List[MaterialRate]:
    """统一把 [{steelType, steelRate|avgRate|rate}, ...] 解析为 MaterialRate 列表（rate → %）。"""
    out: List[MaterialRate] = []
    if not raw_list:
        return out
    for d in raw_list:
        if not isinstance(d, dict):
            continue
        st = _first_scalar(d.get("steelType"))
        if "steelRate" in d:
            rate_raw = d.get("steelRate", 0)
        elif "avgRate" in d:
            rate_raw = d.get("avgRate", 0)
        elif "rate" in d:
            rate_raw = d.get("rate", 0)
        else:
            logger.warning("未知料型字段命名，跳过: %s", d)
            continue
        try:
            rate = float(rate_raw) * 100.0
        except (TypeError, ValueError):
            logger.warning("rate 解析失败: %s", d)
            continue
        out.append(
            MaterialRate(
                steel_type=int(st) if st is not None else None,
                rate=rate,
            )
        )
    return out


def _first_scalar(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _safe_float(value) -> Optional[float]:
    value = _first_scalar(value)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _aggregate_manual_from_operators(
    operators: List[ManualOperator],
) -> Tuple[
    List[MaterialRate],
    Optional[MaterialRate],
    Optional[float],
    Optional[float],
    Optional[float],
]:
    """从（已剔除黑名单的）operator 列表重新合并出人工最终结果。"""
    if not operators:
        return [], None, None, None, None

    n = len(operators)

    # ---- 1. 合并料型占比 ----
    type_sum: Dict[int, float] = {}
    for op in operators:
        for m in op.materials:
            if m.steel_type is None:
                continue
            type_sum[m.steel_type] = type_sum.get(m.steel_type, 0.0) + m.rate

    materials: List[MaterialRate] = [
        MaterialRate(steel_type=st, rate=total / n)
        for st, total in type_sum.items()
    ]

    # ---- 2. 主料型 = 占比最大的合法料型（剔除超标/DEFAULT）----
    main_tup = get_main_type_from_list([(m.steel_type, m.rate) for m in materials])
    main_entry: Optional[MaterialRate] = (
        MaterialRate(steel_type=main_tup[0], rate=main_tup[1])
        if main_tup is not None
        else None
    )

    # ---- 3. 扣重：按"非空"求平均 ----
    def _avg_non_none(values: List[Optional[float]]) -> Optional[float]:
        valid = [v for v in values if v is not None]
        if not valid:
            return None
        return sum(valid) / len(valid)

    deduct = _avg_non_none([op.deduction_ton for op in operators])

    # ---- 4. 人工单价：按"非空"求平均 ----
    manual_price = _avg_non_none([op.steel_price for op in operators])

    # ---- 5. 最终结算单价 ----
    def _avg_weighted_price() -> Optional[float]:
        if not operators:
            return None
        op_prices: List[float] = []
        for op in operators:
            price_sum = 0.0
            has_price = False
            for m in op.materials:
                if m.steel_type is None:
                    continue
                unit_price = STEEL_TYPE_PRICE.get(int(m.steel_type))
                if unit_price is None:
                    continue
                has_price = True
                price_sum += (m.rate / 100.0) * unit_price
            if has_price:
                op_prices.append(price_sum)
        if not op_prices:
            return None
        return sum(op_prices) / len(op_prices)

    final_price = _avg_weighted_price()

    return materials, main_entry, deduct, manual_price, final_price


def _judge_deduction_compliance(
    weight_ratio: Optional[float],
    weight_diff_ton: Optional[float],
    cfg: ShenglongConfig,
) -> Optional[bool]:
    """扣杂准确判定：0.5≤ratio≤1.5 OR |误差|≤0.15t"""
    if weight_ratio is None and weight_diff_ton is None:
        return None
    ratio_ok = (
        weight_ratio is not None
        and cfg.deduction_ratio_lower <= weight_ratio <= cfg.deduction_ratio_upper
    )
    err_ok = (
        weight_diff_ton is not None
        and abs(weight_diff_ton) <= cfg.deduction_error_tolerance_ton
    )
    return ratio_ok or err_ok


def calc_truck(
    date_str: str,
    car_number: str,
    station_number: int,
    flow_code: str,
    detail_data: dict,
    cfg: ShenglongConfig,
) -> TruckStat:
    """对一辆车的详情 data 做完整计算。"""
    stat = TruckStat(
        date=date_str,
        car_number=car_number,
        station_number=station_number,
        flow_code=flow_code,
    )

    # -------- 人工（基于 checkDetails 剔除黑名单后重新合并） --------
    mvo = detail_data.get("manualCheckResultVO") or {}
    check_details = mvo.get("checkDetails") or []
    all_operators = [
        ManualOperator.from_dict(d) for d in check_details if isinstance(d, dict)
    ]
    
    eligible_operators = [
        op for op in all_operators if not is_excluded_operator(op.name)
    ]
    stat.manual_operators = eligible_operators

    if not eligible_operators:
        # 全员被排除 → 视为人工缺失
        stat.manual_materials = []
        stat.manual_main = None
        stat.manual_deduct_ton = None
        stat.manual_steel_price = None
        stat.final_steel_price = None
    else:
        (
            stat.manual_materials,
            stat.manual_main,
            stat.manual_deduct_ton,
            stat.manual_steel_price,
            stat.final_steel_price,
        ) = _aggregate_manual_from_operators(eligible_operators)

    # -------- AI（赛迪） --------
    tcr = detail_data.get("totalCheckResult") or {}
    stat.ai_materials = _parse_rate_list(tcr.get("steelTypeRateList"))
    main_a = get_main_type_from_list(
        [(m.steel_type, m.rate) for m in stat.ai_materials]
    )
    stat.ai_main = (
        MaterialRate(steel_type=main_a[0], rate=main_a[1]) if main_a else None
    )

    # AI 扣重 kg → 吨
    ai_kg = _safe_float(tcr.get("totalDeductWeight"))
    stat.ai_deduct_ton = ai_kg / 1000.0 if ai_kg is not None else None

    # AI 单价（目前无输出，留 None）
    stat.ai_steel_price = _safe_float(tcr.get("steelPrice"))

    # -------- 派生：主料一致性 + 差异值 --------
    if stat.manual_main is None or stat.ai_main is None:
        stat.main_name_match = None
        stat.main_same = None
        stat.diff_rate = None
    else:
        name_match = stat.manual_main.steel_type == stat.ai_main.steel_type
        stat.main_name_match = name_match
        if name_match:
            ai_rate = stat.ai_main.rate
            stat.diff_rate = abs(stat.manual_main.rate - ai_rate)
            stat.main_same = stat.diff_rate <= MAIN_TYPE_DIFF_TOLERANCE
        else:
            stat.diff_rate = None  
            stat.main_same = False

    # -------- 派生：扣重比值 / 误差 --------
    if stat.manual_deduct_ton is not None and stat.ai_deduct_ton is not None:
        stat.weight_diff_ton = abs(stat.ai_deduct_ton - stat.manual_deduct_ton)
        if stat.manual_deduct_ton != 0:
            stat.weight_ratio = stat.ai_deduct_ton / stat.manual_deduct_ton
        else:
            stat.weight_ratio = None
    else:
        stat.weight_diff_ton = None
        stat.weight_ratio = None

    stat.deduction_compliant = _judge_deduction_compliance(
        stat.weight_ratio, stat.weight_diff_ton, cfg
    )

    # -------- 派生：单价差异 --------
    if stat.manual_steel_price is not None and stat.ai_steel_price is not None:
        stat.price_diff = abs(stat.ai_steel_price - stat.manual_steel_price)
    else:
        stat.price_diff = None

    return stat


def aggregate_daily(date_str: str, trucks: List[TruckStat]) -> DailyShenglongStats:
    """单日聚合"""
    return DailyShenglongStats(date=date_str, trucks=list(trucks))


# ----------------------------------------------------------------------
#  周期级（多日）聚合 —— 对应参考表 sheet1「统计周期概括」
# ----------------------------------------------------------------------
def _bucket_price_diff(diff: float) -> int:
    """价格差异分桶"""
    if diff < 30:
        return 0
    if diff < 50:
        return 1
    if diff < 100:
        return 2
    return 3


def aggregate_period(
    stats_list: List[DailyShenglongStats],
    start_date: str,
    end_date: str,
) -> "PeriodSummary":
    """跨多日聚合为周期总体统计。"""
    from agent.shenglong.models import PeriodSummary

    summary = PeriodSummary(
        cycle_label=_format_cycle_label(start_date, end_date),
        start_date=start_date,
        end_date=end_date,
    )

    for day in stats_list:
        for t in day.trucks:
            if t.main_same is not None:
                summary.judgable_trucks += 1
            if t.main_name_match is True:
                summary.main_name_match_count += 1
            if t.main_same is True:
                summary.main_within_10pct_count += 1

            if t.deduction_compliant is not None:
                summary.deduction_evaluable += 1
            if t.deduction_compliant is True:
                summary.deduction_compliant_count += 1

            if t.price_diff is not None:
                summary.price_diff_evaluable += 1
                bucket = _bucket_price_diff(t.price_diff)
                if bucket == 0:
                    summary.price_diff_lt30 += 1
                elif bucket == 1:
                    summary.price_diff_30_50 += 1
                elif bucket == 2:
                    summary.price_diff_50_100 += 1
                else:
                    summary.price_diff_gt100 += 1

    return summary


def _normalized_target_rates(
    materials: List[MaterialRate],
    target_types: frozenset[int],
) -> Dict[int, float]:
    """只保留 target_types，并把这些料型占比归一化到 100%"""
    sums: Dict[int, float] = {int(t): 0.0 for t in target_types}
    for m in materials:
        if m.steel_type is None:
            continue
        st = int(m.steel_type)
        if st in target_types:
            sums[st] += float(m.rate)
    total = sum(sums.values())
    if total <= 0:
        return {}
    return {st: rate / total * 100.0 for st, rate in sums.items()}


def _main_from_normalized_rates(
    rates: Dict[int, float],
) -> Optional[Tuple[int, float]]:
    if not rates:
        return None
    return max(rates.items(), key=lambda item: item[1])


def _judge_heavy_normalized_truck(
    truck: TruckStat,
) -> Tuple[Optional[bool], Optional[bool]]:
    """返回 (重废主类是否相同, 重废归一化准确是否正确)"""
    manual_rates = _normalized_target_rates(
        truck.manual_materials, HEAVY_STEEL_TYPES
    )
    ai_rates = _normalized_target_rates(truck.ai_materials, HEAVY_STEEL_TYPES)
    manual_main = _main_from_normalized_rates(manual_rates)
    ai_main = _main_from_normalized_rates(ai_rates)
    if manual_main is None or ai_main is None:
        return None, None

    name_match = manual_main[0] == ai_main[0]
    if not name_match:
        return False, False

    diff = abs(manual_main[1] - ai_main[1])
    return True, diff <= MAIN_TYPE_DIFF_TOLERANCE


def _heavy_normalized_main_material(
    materials: List[MaterialRate],
) -> Optional[MaterialRate]:
    rates = _normalized_target_rates(materials, HEAVY_STEEL_TYPES)
    main = _main_from_normalized_rates(rates)
    if main is None:
        return None
    return MaterialRate(steel_type=main[0], rate=main[1])


def to_heavy_normalized_view(
    stats_list: List[DailyShenglongStats],
) -> List[DailyShenglongStats]:
    """把单车明细切换成“重废1/2/3归一化对比视图”"""
    out_days: List[DailyShenglongStats] = []
    for day in stats_list:
        trucks: List[TruckStat] = []
        for t in day.trucks:
            manual_main = _heavy_normalized_main_material(t.manual_materials)
            ai_main = _heavy_normalized_main_material(t.ai_materials)
            if manual_main is None or ai_main is None:
                trucks.append(
                    replace(
                        t,
                        manual_main=manual_main,
                        ai_main=ai_main,
                        main_name_match=None,
                        main_same=None,
                        diff_rate=None,
                    )
                )
                continue

            name_match = manual_main.steel_type == ai_main.steel_type
            if name_match:
                diff_rate: Optional[float] = abs(manual_main.rate - ai_main.rate)
                main_same: Optional[bool] = (
                    diff_rate <= MAIN_TYPE_DIFF_TOLERANCE
                )
            else:
                diff_rate = None
                main_same = False

            trucks.append(
                replace(
                    t,
                    manual_main=manual_main,
                    ai_main=ai_main,
                    main_name_match=name_match,
                    main_same=main_same,
                    diff_rate=diff_rate,
                )
            )
        out_days.append(DailyShenglongStats(date=day.date, trucks=trucks))
    return out_days


def aggregate_period_heavy_normalized(
    stats_list: List[DailyShenglongStats],
    start_date: str,
    end_date: str,
) -> "PeriodSummary":
    """重废1/2/3归一化口径的周期统计。"""
    from agent.shenglong.models import PeriodSummary

    summary = PeriodSummary(
        cycle_label=_format_cycle_label(start_date, end_date),
        start_date=start_date,
        end_date=end_date,
        recognition_section_title="重废归一化识别率",
        recognition_condition1_label="排除人工无重废后\n主重废类相同车次数",
        recognition_condition2_label=(
            "重废1/2/3归一化后\n主料型占比差异≤10% 车次数"
        ),
        recognition_result_label="重废归一化准确率",
        cumulative_recognition_label="累计准确率",
    )

    for day in stats_list:
        for t in day.trucks:
            name_match, correct = _judge_heavy_normalized_truck(t)
            if correct is not None:
                summary.judgable_trucks += 1
            if name_match is True:
                summary.main_name_match_count += 1
            if correct is True:
                summary.main_within_10pct_count += 1

            if t.deduction_compliant is not None:
                summary.deduction_evaluable += 1
            if t.deduction_compliant is True:
                summary.deduction_compliant_count += 1

            if t.price_diff is not None:
                summary.price_diff_evaluable += 1
                bucket = _bucket_price_diff(t.price_diff)
                if bucket == 0:
                    summary.price_diff_lt30 += 1
                elif bucket == 1:
                    summary.price_diff_30_50 += 1
                elif bucket == 2:
                    summary.price_diff_50_100 += 1
                else:
                    summary.price_diff_gt100 += 1

    return summary


def _format_cycle_label(start_date: str, end_date: str) -> str:
    """把 YYYY-MM-DD 转成参考表的格式"""
    sy, sm, sd = start_date.split("-")
    ey, em, ed = end_date.split("-")
    return (
        f"{int(sy)}.{int(sm)}.{int(sd)} 至 {int(ey)}.{int(em)}.{int(ed)}"
    )
