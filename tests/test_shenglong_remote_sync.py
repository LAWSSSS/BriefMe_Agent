"""盛隆日期文件夹 scp 同步单测（不连真实服务器）。"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.shenglong.remote_sync import format_sync_report, sync_date_folder


def _ok_proc() -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = ""
    proc.stderr = ""
    return proc


def _fail_proc(stderr: str) -> MagicMock:
    proc = MagicMock()
    proc.returncode = 1
    proc.stdout = ""
    proc.stderr = stderr
    return proc


def test_sync_skips_empty_day(tmp_path: Path) -> None:
    day = tmp_path / "2026-08-01"
    day.mkdir()
    result = sync_date_folder(day)
    assert result.ok is True
    assert result.skipped is True


def test_sync_excludes_datasets_and_copies_trucks(tmp_path: Path) -> None:
    day = tmp_path / "2026-08-01"
    truck = day / "桂A00001_重废1(80)、中废(20)"
    truck.mkdir(parents=True)
    (truck / "a.jpg").write_bytes(b"x")
    (day / "datasets").mkdir()
    (day / "datasets" / "平均料型_实例分割数据集.zip").write_bytes(b"z")

    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return _ok_proc()

    with patch("agent.shenglong.remote_sync.subprocess.run", side_effect=fake_run):
        result = sync_date_folder(day)

    assert result.ok is True
    assert result.skipped is False
    assert result.remote_path.endswith("/2026-08-01")
    scp_calls = [c for c in calls if c and c[0] == "scp"]
    assert len(scp_calls) == 1
    assert str(truck) in scp_calls[0]
    joined = " ".join(scp_calls[0])
    assert str(day / "datasets") not in scp_calls[0]
    assert "cisdi@10.180.34.16:" in joined
    assert "/test_images_full_car/2026-08-01/" in joined


def test_sync_failure_does_not_raise(tmp_path: Path) -> None:
    day = tmp_path / "2026-08-02"
    truck = day / "桂B00002_中废(100)"
    truck.mkdir(parents=True)
    (truck / "b.jpg").write_bytes(b"y")

    def fake_run(cmd, **_kwargs):
        if cmd and cmd[0] == "ssh":
            return _ok_proc()
        return _fail_proc("Permission denied")

    with patch("agent.shenglong.remote_sync.subprocess.run", side_effect=fake_run):
        result = sync_date_folder(day)

    assert result.ok is False
    assert result.skipped is False
    assert "Permission denied" in result.error


def test_sync_timeout_is_failure(tmp_path: Path) -> None:
    day = tmp_path / "2026-08-03"
    truck = day / "桂C00003_重废1(100)"
    truck.mkdir(parents=True)
    (truck / "c.jpg").write_bytes(b"z")

    with patch(
        "agent.shenglong.remote_sync.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="scp", timeout=1),
    ):
        result = sync_date_folder(day)

    assert result.ok is False
    assert "超时" in result.error


def test_format_sync_report_lists_failures() -> None:
    text = format_sync_report(
        [
            {"date": "2026-08-01", "scp_ok": True, "scp_skipped": False, "scp_error": ""},
            {
                "date": "2026-08-02",
                "scp_ok": False,
                "scp_skipped": False,
                "scp_error": "Connection refused",
            },
            {"date": "2026-08-03", "scp_ok": True, "scp_skipped": True, "scp_error": ""},
        ]
    )
    assert "成功：2026-08-01" in text
    assert "2026-08-02：Connection refused" in text
    assert "2026-08-03" not in text.split("成功：")[1].split("\n")[0]
    assert "没有中断" in text
