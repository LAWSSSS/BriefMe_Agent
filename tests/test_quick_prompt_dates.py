from datetime import date, timedelta

from app import _quick_prompts
from agent.core import _normalize_yongfeng_accuracy_args


def test_quick_prompt_week_ranges_use_seven_complete_days():
    current_day = date(2026, 11, 13)
    expected_start = "2026-11-06"
    expected_end = "2026-11-12"

    prompts = _quick_prompts(current_day)

    binxin = prompts["镔鑫钢铁"]["废钢检判"]
    assert expected_start in binxin["近 7 天报表 + 错判图"]
    assert expected_end in binxin["近 7 天报表 + 错判图"]
    assert expected_start in binxin["近 7 天 PPT 汇报页"]
    assert expected_end in binxin["近 7 天 PPT 汇报页"]
    assert expected_start in binxin["近 7 天各料型统计+汇报"]
    assert expected_end in binxin["近 7 天各料型统计+汇报"]

    yongfeng = prompts["永锋钢铁"]["烧结矿颗粒度"]["指定区间准确率报表"]
    assert expected_start in yongfeng
    assert expected_end in yongfeng
    assert "2026-04-01" not in yongfeng

    start = date.fromisoformat(expected_start)
    end = date.fromisoformat(expected_end)
    assert (end - start).days + 1 == 7
    assert end == current_day - timedelta(days=1)


def test_yongfeng_recent_range_is_enforced_when_message_has_no_explicit_date():
    current_day = date(2026, 11, 13)
    normalized = _normalize_yongfeng_accuracy_args(
        {
            "start_time": "2026-04-01 00:00:00",
            "end_time": "2026-04-07 23:59:59",
        },
        "生成永锋烧结矿指定区间准确率报表",
        current_day=current_day,
    )

    assert normalized["start_time"] == "2026-11-06 00:00:00"
    assert normalized["end_time"] == "2026-11-12 23:59:59"


def test_yongfeng_explicit_dates_are_not_overridden():
    args = {
        "start_time": "2026-04-01 00:00:00",
        "end_time": "2026-04-07 23:59:59",
    }
    normalized = _normalize_yongfeng_accuracy_args(
        args,
        "生成 2026-04-01 到 2026-04-07 的永锋烧结矿指定区间准确率报表",
        current_day=date(2026, 11, 13),
    )

    assert normalized == args
