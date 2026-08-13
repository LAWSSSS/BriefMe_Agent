"""永锋废钢检判原图下载。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from .scrap_client import YongfengScrapClient
from .scrap_models import YongfengRecord

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[str], None]


def _safe(value: str) -> str:
    for char in '\\/:*?"<>|':
        value = value.replace(char, "_")
    return value.strip() or "unknown"


def _log(message: str, callback: ProgressCallback | None = None) -> None:
    logger.info(message)
    if callback:
        callback(message)
    else:
        print(message, flush=True)


def download_truck_images(
    client: YongfengScrapClient,
    record: YongfengRecord,
    detail: dict,
    date_str: str,
    output_root: Path,
    progress_callback: ProgressCallback | None = None,
) -> dict:
    urls = ((detail.get("totalCheckResult") or {}).get("allOriginImageUrls") or [])
    urls = list(dict.fromkeys(url for url in urls if isinstance(url, str) and url))
    directory = output_root / date_str / _safe(
        f"{record.car_number}_{record.station_number}_{record.flow_code}"
    )
    saved = skipped = failed = 0

    for index, url in enumerate(urls, 1):
        name = Path(unquote(urlparse(url).path)).name or f"{index:03d}.jpg"
        dest = directory / name
        if dest.exists() and dest.stat().st_size > 0:
            skipped += 1
            _log(f"      [{index}/{len(urls)}] 已存在: {name}", progress_callback)
            continue
        try:
            directory.mkdir(parents=True, exist_ok=True)
            response = client.session.get(
                url, headers=client._headers(), timeout=60, verify=False
            )
            response.raise_for_status()
            dest.write_bytes(response.content)
            saved += 1
            _log(f"      [{index}/{len(urls)}] 已下载: {name}", progress_callback)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception("永锋图片下载失败: %s", url)
            _log(f"      [{index}/{len(urls)}] 下载失败: {name} ({exc})", progress_callback)

    return {"saved": saved, "skipped": skipped, "failed": failed, "total": len(urls)}


def download_images_by_date_range(
    start_date: str,
    end_date: str,
    output_dir: str | Path | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict:
    """按日期范围下载永锋原图，并输出清晰的逐日、逐车进度。"""
    start, end = sorted((start_date, end_date))
    root = Path(output_dir) if output_dir else Path.cwd() / "downloads" / "yongfeng"
    root.mkdir(parents=True, exist_ok=True)
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    total_days = (end_dt - start_dt).days + 1
    result = {
        "output_dir": str(root),
        "success": 0,
        "failed": 0,
        "skipped_existing": 0,
        "days": [],
    }

    _log("=" * 64, progress_callback)
    _log(f"永锋图片下载开始: {start} 至 {end}（共 {total_days} 天）", progress_callback)
    _log(f"保存目录: {root.resolve()}", progress_callback)
    _log("=" * 64, progress_callback)

    with YongfengScrapClient() as client:
        current = start_dt
        day_index = 0
        while current <= end_dt:
            day_index += 1
            day = current.strftime("%Y-%m-%d")
            _log(f"[{day_index}/{total_days}] 查询 {day} 的车辆记录...", progress_callback)
            records = client.query_list_by_date(day)
            item = {
                "date": day,
                "total_trucks": len(records),
                "processed": 0,
                "saved_files": 0,
                "failed_files": 0,
                "skipped_existing": 0,
            }
            _log(f"[{day_index}/{total_days}] {day} 共 {len(records)} 辆车", progress_callback)

            for truck_index, record in enumerate(records, 1):
                _log(
                    f"  [{truck_index}/{len(records)}] {record.car_number} "
                    f"flow={record.flow_code} 获取详情...",
                    progress_callback,
                )
                try:
                    detail = client.get_detail_by_flow(record.flow_code)
                    stats = download_truck_images(
                        client, record, detail, day, root, progress_callback
                    )
                    item["processed"] += 1
                    item["saved_files"] += stats["saved"]
                    item["failed_files"] += stats["failed"]
                    item["skipped_existing"] += stats["skipped"]
                    _log(
                        f"  完成 {record.car_number}: 新增 {stats['saved']}，"
                        f"已存在 {stats['skipped']}，失败 {stats['failed']}",
                        progress_callback,
                    )
                except Exception as exc:  # noqa: BLE001
                    item["failed_files"] += 1
                    logger.exception("永锋车辆处理失败: %s", record.flow_code)
                    _log(f"  车辆处理失败 {record.car_number}: {exc}", progress_callback)

            result["days"].append(item)
            result["success"] += item["saved_files"]
            result["failed"] += item["failed_files"]
            result["skipped_existing"] += item["skipped_existing"]
            _log(
                f"[{day}] 完成：车辆 {item['processed']}/{item['total_trucks']}，"
                f"新增 {item['saved_files']}，失败 {item['failed_files']}，"
                f"跳过 {item['skipped_existing']}",
                progress_callback,
            )
            current += timedelta(days=1)

    _log(
        f"下载结束：新增 {result['success']}，失败 {result['failed']}，"
        f"已存在 {result['skipped_existing']}",
        progress_callback,
    )
    return result
