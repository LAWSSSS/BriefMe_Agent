"""盛隆废钢智能检判图片下载。

实现思路参考 `slsteel_agent` 的下载链路：
1. 按日期拉取列表
2. 逐条获取详情，提取 `allOriginImageUrls`
3. 按日期/车牌/工位组织目录并下载原图

目录结构：
    <output_root>/YYYY-MM-DD/<车牌>_<工位>_<flowCode>/<图片文件>
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

from agent.shenglong.client import ShenglongClient
from agent.shenglong.models import ShenglongRecord

logger = logging.getLogger(__name__)


@dataclass
class TruckDownloadResult:
    flow_code: str
    car_number: str
    station_number: int
    saved_files: list[Path] = field(default_factory=list)
    skipped_existing: int = 0
    failed_files: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class DayDownloadResult:
    date: str
    total_trucks: int
    processed: int = 0
    saved_files: int = 0
    skipped_existing: int = 0
    failed_files: int = 0
    skipped_no_manual: int = 0
    skipped_no_images: int = 0
    failed_detail: list[tuple[str, str]] = field(default_factory=list)



def _safe_dir_name(value: str) -> str:
    text = (value or "").strip()
    for ch in ["\\", "/", ":", "*", "?", '"', "<", ">", "|"]:
        text = text.replace(ch, "_")
    return text or "unknown"



def _filename_from_url(url: str, fallback: str) -> str:
    try:
        path = urlparse(url).path
        name = Path(unquote(path)).name
        return name or fallback
    except Exception:  # noqa: BLE001
        return fallback



def _download_image(client: ShenglongClient, url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = client._client.get(url, headers=client._auth_headers(), timeout=60.0)  # noqa: SLF001
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return len(resp.content)



def download_truck_images(
    client: ShenglongClient,
    record: ShenglongRecord,
    detail: dict,
    date_str: str,
    output_root: Path,
) -> TruckDownloadResult:
    """下载单车全部原图。"""
    urls = ((detail.get("totalCheckResult") or {}).get("allOriginImageUrls") or [])
    urls = [u for u in urls if isinstance(u, str) and u]
    truck_dir = output_root / date_str / _safe_dir_name(f"{record.car_number}_{record.station_number}_{record.flow_code}")
    truck_dir.mkdir(parents=True, exist_ok=True)

    result = TruckDownloadResult(
        flow_code=record.flow_code,
        car_number=record.car_number,
        station_number=record.station_number,
    )

    if not urls:
        return result

    for idx, url in enumerate(urls, start=1):
        fallback = f"{idx:03d}.jpg"
        filename = _filename_from_url(url, fallback)
        dest = truck_dir / filename
        if dest.exists() and dest.stat().st_size > 0:
            result.skipped_existing += 1
            continue
        try:
            _download_image(client, url, dest)
            result.saved_files.append(dest)
        except Exception as exc:  # noqa: BLE001
            logger.exception("下载失败 %s -> %s", url, exc)
            result.failed_files.append((url, str(exc)))

    return result



def download_images_by_date_range(
    start_date: str,
    end_date: str,
    output_dir: str | Path | None = None,
    *,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    include_missing_manual: bool = True,
) -> dict:
    """按日期范围批量下载盛隆原图。"""
    start = start_date
    end = end_date
    if start > end:
        start, end = end, start

    output_root = Path(output_dir) if output_dir is not None else Path.cwd() / "shenglong_images"
    output_root.mkdir(parents=True, exist_ok=True)

    with ShenglongClient() as client:
        day_results: list[DayDownloadResult] = []
        cur_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")

        while cur_dt <= end_dt:
            cur = cur_dt.strftime("%Y-%m-%d")
            records = client.query_list_by_date(cur)
            day = DayDownloadResult(date=cur, total_trucks=len(records))
            total = len(records)
            done = 0

            for rec in records:
                done += 1
                try:
                    detail = client.get_detail_by_flow(rec.flow_code)
                except Exception as exc:  # noqa: BLE001
                    day.failed_detail.append((rec.flow_code, str(exc)))
                    day.failed_files += 1
                    if progress_callback:
                        progress_callback(done, total, f"{cur} {rec.car_number} 详情获取失败")
                    continue

                manual = detail.get("manualCheckResultVO") or {}
                has_manual = bool(manual.get("avgResult") or manual.get("checkDetails"))
                if not has_manual and not include_missing_manual:
                    day.skipped_no_manual += 1
                    continue

                urls = ((detail.get("totalCheckResult") or {}).get("allOriginImageUrls") or [])
                if not urls:
                    day.skipped_no_images += 1
                    continue

                result = download_truck_images(client, rec, detail, cur, output_root)
                day.processed += 1
                day.saved_files += len(result.saved_files)
                day.skipped_existing += result.skipped_existing
                day.failed_files += len(result.failed_files)
                if progress_callback:
                    progress_callback(done, total, f"{cur} {rec.car_number} 下载中")

            day_results.append(day)
            cur_dt += timedelta(days=1)

    return {
        "output_dir": str(output_root),
        "days": [d.__dict__ for d in day_results],
        "success": sum(d.saved_files for d in day_results),
        "failed": sum(d.failed_files for d in day_results),
        "skipped_existing": sum(d.skipped_existing for d in day_results),
    }
