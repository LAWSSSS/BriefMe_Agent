"""BriefMe · 多场景数据统计助手（by 中冶赛迪） —— Gradio 入口

界面结构（浅色企业工作台）：
  L0 页面   浅灰底，只做背景
  L1 卡片   白底 + 1px 灰实线 + 12 圆角，标题行在卡片内部
  L2 分区   卡内分隔线 / 浅底内嵌块，自带 11.5px 分区小标题
  L3 控件   项目分段（2px）/ 业务·指令选项列表（2px 描边圆角项）

  应用栏：品牌块 + 当日日期 + 四个 VPN 状态灯 + 刷新操作按钮
  左栏  ：指令台卡片（标题·项目·业务·指令·待发送内容·填入按钮同框）
         + 文件下载路径卡片 + 文件上传卡片（均为常展开，可交互强调）
  右栏  ：对话卡片（标题行 / 气泡区 / 输入区）+ 输出中心卡片（产物·图片·日志三标签）

Agent 业务逻辑完全不改，仅重写 app.py 的 UI 层。
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import gradio as gr
import httpx
from starlette import status as _starlette_status
from starlette.responses import JSONResponse

# Gradio 6.x 每次入队都会读 Starlette 已弃用的 HTTP_422_UNPROCESSABLE_ENTITY，
# 终端刷 StarletteDeprecationWarning，用户易当成上传报错。
# 在模块上写入同值新常量，避免走 __getattr__ 弃用分支；并过滤残留警告。
_starlette_status.HTTP_422_UNPROCESSABLE_ENTITY = (
    _starlette_status.HTTP_422_UNPROCESSABLE_CONTENT
)
try:
    from starlette.exceptions import StarletteDeprecationWarning as _StarletteDeprecationWarning
except ImportError:  # pragma: no cover
    _StarletteDeprecationWarning = UserWarning  # type: ignore[misc, assignment]
warnings.filterwarnings(
    "ignore",
    message=".*HTTP_422_UNPROCESSABLE_ENTITY.*",
    category=_StarletteDeprecationWarning,
)

logger = logging.getLogger(__name__)

from agent.core import SteelCoilAgent, _parse_save_path
from agent.artifacts import (
    append_step,
    artifact_files_by_kind,
    commit_run,
    finalize_run,
    image_items,
    load_artifact_history,
    new_run,
    output_dirs,
    register_artifact,
    register_artifacts_from_result,
    run_from_dict,
    scan_new_artifacts,
    scan_new_images,
    sync_current_run,
)
from agent.image_download_route import (
    is_ambiguous_multilabel_pack,
    is_dual_plant_image_or_pack_request,
    is_shenglong_image_download,
    is_shenglong_multilabel_pack,
    is_yongfeng_dataset_upload,
    is_yongfeng_image_download,
    is_yongfeng_multilabel_pack,
)
from agent.shenglong.calculator import last_complete_7_days
from agent.shenglong.downloader import (
    iter_download_images as iter_shenglong_images,
    parse_requested_dates as parse_shenglong_dates,
)
from agent.shenglong.packager import pack_multilabel_from_disk as pack_shenglong_multilabel
from agent.yongfeng.downloader import (
    iter_download_images as iter_yongfeng_images,
    parse_requested_dates as parse_yongfeng_dates,
)
from agent.yongfeng.scrap_align import (
    format_align_summary,
    is_yongfeng_manual_align,
    iter_align_manual,
    resolve_manual_path,
    uploaded_to_path,
)
from agent.yongfeng.scrap_packager import pack_multilabel_from_disk as pack_yongfeng_multilabel
from agent.yongfeng.scrap_uploader import iter_upload_zips_for_dates
from config.settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

agent = SteelCoilAgent()

DOWNLOADS_ROOT = Path("downloads")
DEFAULT_DOWNLOAD_DIR = "./downloads"
_preferences_override = os.getenv("BRIEFME_PREFERENCES_PATH")
UI_PREFERENCES_PATH = (
    Path(_preferences_override).expanduser()
    if _preferences_override
    else Path.home() / ".briefme" / "preferences.json"
)
SCRAP_ROOT = DOWNLOADS_ROOT / "scrap"
SHENGLONG_ROOT = DOWNLOADS_ROOT / "shenglong"
YONGFENG_ROOT = DOWNLOADS_ROOT / "yongfeng"
BXSTEEL_ROOT = DOWNLOADS_ROOT / "bxsteel"
YONGYOU_ROOT = DOWNLOADS_ROOT / "yongyou"
DOWNLOAD_HISTORY_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".webp",
    ".zip",
}
_FILE_SELECTION_TARGETS: Dict[str, str] = {}
_FILE_SELECTION_LOCK = threading.Lock()
_DOWNLOAD_ALLOWED_DIRS: set[str] = set()


# =====================================================================
# VPN 状态探测
# =====================================================================
def _check_packing_vpn() -> bool:
    """永锋专网是否连通（探测打包带业务域名，与烧结矿/检判原图同一 VPN）"""
    try:
        return agent.vpn.check_connectivity()
    except Exception:
        return False


def _check_scrap_vpn() -> bool:
    """镔鑫废钢 VPN 是否连通（3s 内能访问前端页）"""
    try:
        r = httpx.get(
            f"{settings.scrap.base_url}/fcs-web/",
            timeout=3.0,
            follow_redirects=True,
        )
        return r.status_code < 500
    except Exception:
        return False


def _check_shenglong_vpn() -> bool:
    """盛隆废钢 VPN 是否连通（3s 内能访问前端根页）"""
    try:
        r = httpx.get(
            f"{settings.shenglong.base_url}/",
            timeout=3.0,
            follow_redirects=True,
        )
        return r.status_code < 500
    except Exception:
        return False


def _check_yongyou_vpn() -> bool:
    """用友 VPN 是否连通（3s 内能访问前端页）"""
    try:
        r = httpx.get(
            "http://172.26.46.12:8890/",
            timeout=3.0,
            follow_redirects=True,
        )
        return r.status_code < 500
    except Exception:
        return False


def _status_html() -> str:
    today_str = date.today().strftime("%Y-%m-%d")
    pt_ok = _check_packing_vpn()
    sc_ok = _check_scrap_vpn()
    sl_ok = _check_shenglong_vpn()
    yy_ok = _check_yongyou_vpn()
    pt_badge = _badge("永锋 VPN", pt_ok)
    sc_badge = _badge("镔鑫 VPN", sc_ok)
    sl_badge = _badge("盛隆 VPN", sl_ok)
    yy_badge = _badge("用友 VPN", yy_ok)
    return (
        f'<div class="status-bar">'
        f'<span class="today-chip">{today_str}</span>'
        f"{pt_badge}{sc_badge}{sl_badge}{yy_badge}"
        f"</div>"
    )


def _badge(label: str, ok: bool) -> str:
    cls = "ok" if ok else "fail"
    text = "已连接" if ok else "未连接"
    return (
        f'<span class="status-badge {cls}">'
        f'<span class="status-dot {cls}"></span>'
        f"{label}<b>{text}</b>"
        f"</span>"
    )


def _resolve_download_dir(raw_path: Optional[str]) -> Path:
    text = os.path.expandvars(
        os.path.expanduser((raw_path or "").strip() or DEFAULT_DOWNLOAD_DIR)
    )
    path = Path(text)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _load_download_dir_preference() -> str:
    try:
        payload = json.loads(UI_PREFERENCES_PATH.read_text(encoding="utf-8"))
        value = payload.get("download_dir")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (OSError, ValueError, TypeError):
        pass
    return DEFAULT_DOWNLOAD_DIR


def _save_download_dir_preference(raw_path: Optional[str]) -> None:
    value = (raw_path or "").strip() or DEFAULT_DOWNLOAD_DIR
    payload: Dict = {}
    try:
        if UI_PREFERENCES_PATH.is_file():
            current = json.loads(UI_PREFERENCES_PATH.read_text(encoding="utf-8"))
            if isinstance(current, dict):
                payload.update(current)
        payload["download_dir"] = value
        UI_PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = UI_PREFERENCES_PATH.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(UI_PREFERENCES_PATH)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("保存下载路径偏好失败: %s", exc)


def _render_live_status(steps: List[str], elapsed: float = 0.0) -> str:
    lines = ["🧠 **BriefMe 正在处理**", ""]
    if not steps:
        steps = ["正在提交请求…"]
    for index, step in enumerate(steps):
        marker = "✓" if index < len(steps) - 1 else "◌"
        lines.append(f"{marker} {step}")
    if elapsed >= 1.0:
        lines.extend(["", f"⏱ 已用时 {elapsed:.0f} 秒"])
    return "\n".join(lines)


def _open_download_dir(raw_path: Optional[str]) -> None:
    folder = _resolve_download_dir(raw_path)
    _save_download_dir_preference(raw_path)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise gr.Error(f"无法创建保存目录：{folder}\n{exc}") from exc

    try:
        if sys.platform == "win32":
            startfile = getattr(os, "startfile", None)
            if startfile is None:
                raise OSError("Windows 文件管理器调用不可用")
            startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["open", str(folder)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        elif os.name == "posix":
            subprocess.Popen(
                ["xdg-open", str(folder)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            raise OSError(f"暂不支持当前系统：{sys.platform}")
    except OSError as exc:
        raise gr.Error(f"无法打开文件夹：{folder}\n{exc}") from exc


def _format_file_size(size: int) -> str:
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _remember_download_dir(raw_path: str | Path) -> None:
    path = Path(raw_path).expanduser()
    directory = path if path.is_dir() else path.parent
    try:
        _DOWNLOAD_ALLOWED_DIRS.add(str(directory.resolve()))
    except OSError:
        return


def _gradio_download_url(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve()
    _remember_download_dir(resolved)
    return f"/gradio_api/file={quote(str(resolved), safe='/:')}"


def _register_file_selection_target(path: str) -> str:
    resolved = str(Path(path).resolve())
    token = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:20]
    with _FILE_SELECTION_LOCK:
        _FILE_SELECTION_TARGETS[token] = resolved
        if len(_FILE_SELECTION_TARGETS) > 2000:
            oldest = next(iter(_FILE_SELECTION_TARGETS))
            _FILE_SELECTION_TARGETS.pop(oldest, None)
    return token


def _resolve_file_selection_target(token: str) -> Optional[Path]:
    with _FILE_SELECTION_LOCK:
        raw_path = _FILE_SELECTION_TARGETS.get(token or "")
    if not raw_path:
        return None
    path = Path(raw_path)
    return path if path.is_file() else None


def _reveal_file_in_folder(path: Path) -> None:
    resolved = path.resolve()
    if not resolved.is_file():
        raise OSError(f"文件不存在：{resolved}")
    quiet_options = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        subprocess.Popen(
            ["explorer.exe", f"/select,{resolved}"],
            **quiet_options,
        )
    elif sys.platform == "darwin":
        subprocess.Popen(
            ["open", "-R", str(resolved)],
            start_new_session=True,
            **quiet_options,
        )
    elif os.name == "posix":
        subprocess.Popen(
            ["xdg-open", str(resolved.parent)],
            start_new_session=True,
            **quiet_options,
        )
    else:
        raise OSError(f"暂不支持当前系统：{sys.platform}")


def _enable_high_dpi_for_tk() -> None:
    """让 Windows 原生文件夹对话框按显示器 DPI 渲染，而不是被系统位图拉伸。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
    except ImportError:
        return

    user32 = ctypes.windll.user32
    try:
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if set_context(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass

    try:
        set_awareness = ctypes.windll.shcore.SetProcessDpiAwareness
        set_awareness.argtypes = [ctypes.c_int]
        set_awareness.restype = ctypes.c_long
        if set_awareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            return
    except (AttributeError, OSError):
        pass

    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def _configure_tk_dpi_scaling(root) -> None:
    """按当前窗口所在显示器的实际 DPI 设置 Tk 字体与控件缩放。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        get_dpi = ctypes.windll.user32.GetDpiForWindow
        get_dpi.argtypes = [ctypes.c_void_p]
        get_dpi.restype = ctypes.c_uint
        dpi = int(get_dpi(root.winfo_id())) or 96
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        # DPI 优化失败时保留 Tk 默认缩放，不影响目录选择功能。
        return


def _choose_download_dir(raw_path: Optional[str]) -> str:
    _enable_high_dpi_for_tk()
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise gr.Error("当前 Python 环境缺少文件夹选择组件。") from exc

    initial_dir = _resolve_download_dir(raw_path)
    while not initial_dir.exists() and initial_dir != initial_dir.parent:
        initial_dir = initial_dir.parent

    try:
        root = tk.Tk()
    except Exception as exc:
        raise gr.Error(f"无法初始化文件夹选择窗口：{exc}") from exc
    try:
        root.withdraw()
        _configure_tk_dpi_scaling(root)
        root.attributes("-topmost", True)
        root.update_idletasks()
        selected = filedialog.askdirectory(
            parent=root,
            initialdir=str(initial_dir),
            title="选择检判原图保存目录",
            mustexist=False,
        )
    except Exception as exc:
        raise gr.Error(f"无法打开文件夹选择窗口：{exc}") from exc
    finally:
        root.destroy()

    if not selected:
        return (raw_path or DEFAULT_DOWNLOAD_DIR).strip() or DEFAULT_DOWNLOAD_DIR
    selected_path = str(Path(selected).resolve())
    _save_download_dir_preference(selected_path)
    return selected_path


BRAND_HTML = (
    '<div class="brand">'
    '<div class="brand-text">'
    '<div class="brand-title">'
    '<span class="brand-accent">BriefMe</span>'
    '<span class="brand-sep">·</span>多场景数据统计助手'
    "</div>"
    '<div class="brand-sub">'
    '<span class="brand-org">中冶赛迪（重庆）信息技术有限公司</span>'
    "</div>"
    "</div>"
    "</div>"
)


# =====================================================================
# 产物扫描（产物停靠面板）
# =====================================================================
IMG_EXTS = {".jpg", ".jpeg", ".png"}


def _is_visible_file(p: Path) -> bool:
    name = p.name
    if name.startswith(".") or name.startswith("~$"):
        return False
    return True


def _scan_latest_artifacts() -> Tuple[Optional[str], Optional[str], List[str]]:
    roots = [r for r in (SCRAP_ROOT, SHENGLONG_ROOT, YONGFENG_ROOT, BXSTEEL_ROOT, YONGYOU_ROOT) if r.exists()]
    if not roots:
        return None, None, []

    xlsxs: List[Path] = []
    pptxs: List[Path] = []
    imgs: List[Path] = []
    for root in roots:
        xlsxs.extend(p for p in root.rglob("*.xlsx") if _is_visible_file(p))
        pptxs.extend(p for p in root.rglob("*.pptx") if _is_visible_file(p))
        for ext in IMG_EXTS:
            imgs.extend(p for p in root.rglob(f"*{ext}") if _is_visible_file(p))

    xlsxs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest_xlsx = str(xlsxs[0]) if xlsxs else None

    pptxs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest_pptx = str(pptxs[0]) if pptxs else None

    imgs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    img_paths = [str(p) for p in imgs[:20]]

    return latest_xlsx, latest_pptx, img_paths


def _scan_recent_zip_files(limit: int = 20) -> List[str]:
    roots = [
        root
        for root in (
            SCRAP_ROOT,
            SHENGLONG_ROOT,
            YONGFENG_ROOT,
            BXSTEEL_ROOT,
            YONGYOU_ROOT,
        )
        if root.exists()
    ]
    zips = [
        path
        for root in roots
        for path in root.rglob("*.zip")
        if _is_visible_file(path)
    ]
    zips.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [str(path) for path in zips[:limit]]


def _legacy_run_from_scan() -> Optional[Dict]:
    xlsx, pptx, images = _scan_latest_artifacts()
    zip_files = _scan_recent_zip_files()
    if not any((xlsx, pptx, images, zip_files)):
        return None
    run = new_run("历史扫描", "本地目录", "上次生成的产物")
    if xlsx:
        register_artifact(run, xlsx, kind="xlsx", source_tool="legacy_scan")
    if pptx:
        register_artifact(run, pptx, kind="pptx", source_tool="legacy_scan")
    for path in images:
        register_artifact(run, path, kind="image", source_tool="legacy_scan")
    for path in zip_files:
        register_artifact(run, path, kind="zip", source_tool="legacy_scan")
    finalize_run(run, "success")
    return run.to_dict()


def _active_run(session: Optional[Dict]) -> Optional[Dict]:
    if not isinstance(session, dict):
        return None
    current = session.get("current_run")
    if isinstance(current, dict):
        return current
    recent = session.get("recent_runs")
    if isinstance(recent, list) and recent and isinstance(recent[0], dict):
        return recent[0]
    return None


def _status_label(status: str) -> str:
    return {
        "running": "运行中",
        "success": "已完成",
        "failed": "失败",
        "cancelled": "已终止",
    }.get(status, status or "未知")


def _artifact_summary_html(run: Optional[Dict]) -> str:
    if not run:
        return '<div class="output-empty">本次任务暂未生成报表或 PPT</div>'
    files = artifact_files_by_kind(run, {"xlsx", "pptx"})
    if not files:
        return '<div class="output-empty">本次任务暂未生成报表或 PPT</div>'
    kinds = {}
    for item in run.get("artifacts", []):
        if item.get("path") not in files:
            continue
        kind = item.get("kind") or "other"
        kinds[kind] = kinds.get(kind, 0) + 1
    kind_text = " · ".join(f"{name.upper()} {count}" for name, count in kinds.items())
    project = html.escape(str(run.get("project") or "未指定项目"))
    business = html.escape(str(run.get("business") or "未指定业务"))
    status = html.escape(_status_label(str(run.get("status") or "")))
    return (
        '<div class="output-summary">'
        '<div class="output-summary-main">'
        f"<strong>{project}</strong><span>{business}</span>"
        "</div>"
        f'<div class="output-summary-meta"><span>{status}</span><span>{html.escape(kind_text)}</span></div>'
        "</div>"
    )


def _image_summary_html(run: Optional[Dict]) -> str:
    if not run:
        return '<div class="output-empty">本次任务暂未生成错判图或图片包</div>'
    images = image_items(run, limit=20)
    packages = artifact_files_by_kind(run, {"zip"})
    if not images and not packages:
        return '<div class="output-empty">本次任务暂未生成错判图或图片包</div>'
    detail = []
    if images:
        detail.append(f"{len(images)} 张错判图")
    if packages:
        detail.append(f"{len(packages)} 个图片包")
    return (
        '<div class="output-summary">'
        '<div class="output-summary-main">'
        f"<strong>{html.escape(str(run.get('project') or '未指定项目'))}</strong>"
        f"<span>{' · '.join(detail)}</span>"
        "</div>"
        "<div class=\"output-summary-meta\"><span>当前任务</span></div>"
        "</div>"
    )


def _tail_text_file(path: str, max_lines: int = 400) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except OSError:
        return ""


def _run_log_text(run: Optional[Dict]) -> str:
    if not run:
        return "暂无运行日志"
    log_path = run.get("log_path")
    if log_path:
        content = _tail_text_file(log_path)
        if content.strip():
            return content
    steps = run.get("steps") or []
    if steps:
        return "\n".join(
            f"{item.get('time', '')}  {item.get('message', '')}".strip()
            for item in steps[-400:]
        )
    return "暂无运行日志"


def _download_status_meta(status: str) -> Tuple[str, str]:
    normalized = (status or "unknown").lower()
    return {
        "running": ("下载中", "running"),
        "success": ("已完成", "success"),
        "failed": ("失败", "failed"),
        "cancelled": ("已终止", "cancelled"),
    }.get(normalized, ("已写入", "written"))


def _history_runs(session: Optional[Dict]) -> List[Dict]:
    runs: List[Dict] = []
    seen = set()
    sources: List[Dict] = []
    if isinstance(session, dict):
        current = session.get("current_run")
        if isinstance(current, dict):
            sources.append(current)
        sources.extend(
            item for item in session.get("recent_runs", []) if isinstance(item, dict)
        )
    sources.extend(load_artifact_history())
    for run in sources:
        run_id = str(run.get("run_id") or "")
        if run_id and run_id in seen:
            continue
        if run_id:
            seen.add(run_id)
        runs.append(run)
    return runs


def _format_history_minute(raw_value: object, fallback: Optional[float] = None) -> str:
    try:
        value = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        if fallback is None:
            return "—"
        value = datetime.fromtimestamp(fallback).astimezone()
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def _collect_download_history(
    session: Optional[Dict],
    *,
    limit: int = 300,
) -> List[Dict[str, object]]:
    rows: Dict[str, Dict[str, object]] = {}
    runs = _history_runs(session)
    for run in runs:
        status_label, status_code = _download_status_meta(str(run.get("status") or ""))
        file_status_label = "下载中" if status_code == "running" else "已完成"
        file_status_code = "running" if status_code == "running" else "success"
        run_id = str(run.get("run_id") or "")
        run_command = str(run.get("command") or "下载任务")
        run_project = str(run.get("project") or "")
        run_business = str(run.get("business") or "")
        run_time = _format_history_minute(run.get("started_at"))
        for artifact in run.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            raw_path = str(artifact.get("path") or "")
            if not raw_path:
                continue
            path = Path(raw_path)
            if (
                not path.is_file()
                or path.suffix.lower() not in DOWNLOAD_HISTORY_EXTS
            ):
                continue
            resolved = str(path.resolve())
            if resolved in rows:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rows[resolved] = {
                "path": resolved,
                "name": path.name,
                "time": _format_history_minute(artifact.get("created_at"), stat.st_mtime),
                "mtime": stat.st_mtime,
                "status": file_status_label,
                "status_code": file_status_code,
                "size": _format_file_size(stat.st_size),
                "location": str(path.parent),
                "run_id": run_id,
                "command": run_command,
                "project": run_project,
                "business": run_business,
                "run_time": run_time,
                "run_status": status_label,
                "run_status_code": status_code,
            }

    # 所有已持久化但仍处于 running 的任务都要扫描，进程重启后也能找回部分文件。
    for run in runs:
        if str(run.get("status") or "") != "running":
            continue
        started = 0.0
        try:
            started = datetime.fromisoformat(
                str(run.get("started_at") or "").replace("Z", "+00:00")
            ).timestamp() - 2
        except (TypeError, ValueError):
            pass
        for raw_dir in run.get("output_dirs") or []:
            directory = Path(raw_dir)
            if not directory.is_dir():
                continue
            for path in directory.rglob("*"):
                if (
                    not path.is_file()
                    or path.suffix.lower() not in DOWNLOAD_HISTORY_EXTS
                ):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < started:
                    continue
                resolved = str(path.resolve())
                rows.setdefault(
                    resolved,
                    {
                        "path": resolved,
                        "name": path.name,
                        "time": _format_history_minute(None, stat.st_mtime),
                        "mtime": stat.st_mtime,
                        "status": "下载中",
                        "status_code": "running",
                        "size": _format_file_size(stat.st_size),
                        "location": str(path.parent),
                        "run_id": str(run.get("run_id") or ""),
                        "command": str(run.get("command") or "下载任务"),
                        "project": str(run.get("project") or ""),
                        "business": str(run.get("business") or ""),
                        "run_time": _format_history_minute(run.get("started_at")),
                        "run_status": "下载中",
                        "run_status_code": "running",
                    },
                )

    # 首次升级或任务异常退出、历史未提交时，回退扫描默认下载目录。
    if not rows and DOWNLOADS_ROOT.is_dir():
        candidates = [
            path
            for path in DOWNLOADS_ROOT.rglob("*")
            if path.is_file()
            and path.suffix.lower() in DOWNLOAD_HISTORY_EXTS
            and _is_visible_file(path)
        ]
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for path in candidates[:limit]:
            try:
                stat = path.stat()
            except OSError:
                continue
            resolved = str(path.resolve())
            rows[resolved] = {
                "path": resolved,
                "name": path.name,
                "time": _format_history_minute(None, stat.st_mtime),
                "mtime": stat.st_mtime,
                "status": "已完成",
                "status_code": "success",
                "size": _format_file_size(stat.st_size),
                "location": str(path.parent),
                "run_id": "",
                "command": "本地下载记录",
                "project": "",
                "business": "",
                "run_time": _format_history_minute(None, stat.st_mtime),
                "run_status": "已写入",
                "run_status_code": "written",
            }

    ordered = sorted(rows.values(), key=lambda row: float(row["mtime"]), reverse=True)
    return ordered[:limit]


def _download_history_html(session: Optional[Dict]) -> str:
    rows = _collect_download_history(session)
    files_by_run: Dict[str, List[Dict[str, object]]] = {}
    for row in rows:
        run_id = str(row.get("run_id") or "")
        if run_id:
            files_by_run.setdefault(run_id, []).append(row)

    tasks: Dict[str, Dict[str, object]] = {}

    def is_download_run(run: Dict) -> bool:
        text = f"{run.get('command') or ''} {run.get('business') or ''}"
        return any(keyword in text for keyword in ("下载", "检判原图", "图片包"))

    for run in _history_runs(session):
        if not is_download_run(run):
            continue
        run_id = str(run.get("run_id") or "")
        status_label, status_code = _download_status_meta(
            str(run.get("status") or "")
        )
        tasks[run_id or f"run-{len(tasks)}"] = {
            "run_id": run_id,
            "command": run.get("command") or "下载任务",
            "project": run.get("project") or "",
            "business": run.get("business") or "",
            "run_time": _format_history_minute(run.get("started_at")),
            "status": status_label,
            "status_code": status_code,
            "files": files_by_run.get(run_id, []),
        }

    matched_run_ids = {
        str(task.get("run_id") or "")
        for task in tasks.values()
        if task.get("run_id")
    }
    remaining_rows = [
        row
        for row in rows
        if not row.get("run_id") or str(row.get("run_id")) not in matched_run_ids
    ]
    if remaining_rows:
        tasks["__local_history__"] = {
            "command": "本地下载记录",
            "project": "",
            "business": "",
            "run_time": "",
            "status": "已完成",
            "status_code": "success",
            "files": remaining_rows,
        }

    if not tasks:
        return '<div class="download-history-empty">暂无下载任务记录</div>'

    task_cards = []
    for task in tasks.values():
        task_files = sorted(
            task["files"], key=lambda file: float(file["mtime"]), reverse=True
        )
        locations = sorted({str(file["location"]) for file in task_files})
        if not locations:
            location_text = "未留下文件"
        elif len(locations) == 1:
            location_text = locations[0]
        else:
            location_text = f"{len(locations)} 个位置"
        subtitle = " · ".join(
            part
            for part in (
                str(task["project"] or ""),
                str(task["business"] or ""),
                str(task["run_time"] or ""),
            )
            if part
        )
        rendered_files = []
        for file in task_files:
            token = _register_file_selection_target(str(file["path"]))
            suffix = Path(str(file["name"])).suffix.lstrip(".").upper() or "FILE"
            rendered_files.append(
                '<div class="download-file-item">'
                '<div class="download-file-main">'
                f'<span class="download-file-kind">{html.escape(suffix)}</span>'
                f'<strong title="{html.escape(str(file["name"]), quote=True)}">'
                f'{html.escape(str(file["name"]))}</strong>'
                f'<span class="download-status {html.escape(str(file["status_code"]))}">'
                f'{html.escape(str(file["status"]))}</span>'
                "</div>"
                '<div class="download-file-meta">'
                f'<span>{html.escape(str(file["time"]))}</span>'
                f'<span>{html.escape(str(file["size"]))}</span>'
                "</div>"
                f'<div class="download-file-location" title="{html.escape(str(file["location"]), quote=True)}">'
                f'{html.escape(str(file["location"]))}</div>'
                '<button type="button" class="download-open-folder" '
                f'data-briefme-file-token="{html.escape(token, quote=True)}">'
                "定位文件</button>"
                "</div>"
            )
        file_content = (
            f'<div class="download-task-files">{"".join(rendered_files)}</div>'
            if rendered_files
            else '<div class="download-task-empty">该任务未留下下载文件</div>'
        )
        task_cards.append(
            '<article class="download-task-card">'
            '<header class="download-task-head">'
            '<div class="download-task-title">'
            f"<strong>{html.escape(str(task['command']))}</strong>"
            f"<span>{html.escape(subtitle)}</span>"
            "</div>"
            f'<span class="download-status {html.escape(str(task["status_code"]))}">'
            f'{html.escape(str(task["status"]))}</span>'
            "</header>"
            '<div class="download-task-meta">'
            f"<span>{len(task_files)} 个文件</span>"
            f'<span title="{html.escape("; ".join(locations), quote=True)}">'
            f"{html.escape(location_text)}</span>"
            "</div>"
            f"{file_content}"
            "</article>"
        )

    return (
        '<div class="download-history">'
        '<div class="download-history-head">'
        '<div class="download-history-title">'
        "<strong>下载任务记录</strong>"
        f"<span>{len(tasks)} 个任务 · {sum(len(task['files']) for task in tasks.values())} 个文件</span>"
        "</div>"
        '<button type="button" class="download-history-expand" '
        'data-briefme-expand-history="true">'
        '<span class="expand-label">展开查看</span>'
        '<span class="collapse-label">退出全屏</span>'
        "</button>"
        "</div>"
        '<div class="download-history-scroll">'
        f"{''.join(task_cards)}"
        "</div>"
        "</div>"
    )


def _download_progress_text(run: Optional[Dict]) -> str:
    if not isinstance(run, dict):
        return "等待运行状态更新"
    for raw_dir in run.get("output_dirs") or []:
        progress_path = Path(raw_dir) / "download_progress.json"
        try:
            payload = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        parts = []
        if payload.get("current_date"):
            parts.append(f"日期 {payload['current_date']}")
        if payload.get("current_truck"):
            parts.append(f"车辆 {payload['current_truck']}")
        for key, label in (
            ("saved", "已保存"),
            ("skipped", "跳过"),
            ("failed", "失败"),
        ):
            if payload.get(key) is not None:
                parts.append(f"{label} {payload.get(key)}")
        if parts:
            return " · ".join(parts)
    steps = run.get("steps") or []
    if steps and isinstance(steps[-1], dict):
        return str(steps[-1].get("message") or "等待运行状态更新")
    return "等待运行状态更新"


def _log_summary_html(run: Optional[Dict]) -> str:
    if not run:
        return (
            '<div class="current-task-status">'
            '<div class="current-task-status-title">当前任务状态</div>'
            '<div class="current-task-item current-task-item-empty">暂无下载任务</div>'
            "</div>"
        )
    current_step = _download_progress_text(run)
    command = str(run.get("command") or "下载任务")
    return (
        '<div class="current-task-status">'
        '<div class="current-task-status-title">当前任务状态</div>'
        '<div class="current-task-item">'
        '<div class="current-task-item-head">'
        f"<strong>{html.escape(command)}</strong>"
        f'<span class="download-status {html.escape(_download_status_meta(str(run.get("status") or ""))[1])}">'
        f"{html.escape(_status_label(str(run.get('status') or '')))}</span>"
        "</div>"
        '<div class="current-task-item-progress">'
        f"<span>{html.escape(current_step)}</span>"
        "</div>"
        "</div>"
        "</div>"
    )


def _artifact_history_html(
    run: Optional[Dict],
    files: List[str],
    *,
    title: str,
    empty_text: str,
    status_label: str,
) -> str:
    rendered_files = []
    for raw_path in files:
        path = Path(raw_path)
        try:
            stat = path.stat()
        except OSError:
            continue
        selection_token = _register_file_selection_target(str(path.resolve()))
        download_url = _gradio_download_url(path)
        suffix = path.suffix.lstrip(".").upper() or "FILE"
        rendered_files.append(
            '<div class="artifact-history-file download-file-item">'
            '<div class="download-file-main">'
            f'<span class="download-file-kind">{html.escape(suffix)}</span>'
            f'<strong title="{html.escape(path.name, quote=True)}">'
            f"{html.escape(path.name)}</strong>"
            f'<span class="download-status success">{html.escape(status_label)}</span>'
            "</div>"
            '<div class="download-file-meta">'
            f"<span>{_format_history_minute(None, stat.st_mtime)}</span>"
            f"<span>{html.escape(_format_file_size(stat.st_size))}</span>"
            "</div>"
            f'<div class="download-file-location" title="{html.escape(str(path.parent), quote=True)}">'
            f"{html.escape(str(path.parent))}</div>"
            '<div class="artifact-history-actions">'
            '<a class="artifact-download-link" '
            f'href="{html.escape(download_url, quote=True)}">'
            "下载文件</a>"
            '<button type="button" class="download-open-folder" '
            f'data-briefme-file-token="{html.escape(selection_token, quote=True)}">'
            "定位文件</button>"
            "</div>"
            "</div>"
        )
    body = (
        f'<div class="artifact-history-list">{"".join(rendered_files)}</div>'
        if rendered_files
        else f'<div class="download-history-empty">{html.escape(empty_text)}</div>'
    )
    return (
        '<div class="download-history artifact-history">'
        '<div class="download-history-head">'
        '<div class="download-history-title">'
        f"<strong>{html.escape(title)}</strong>"
        f"<span>{len(rendered_files)} 个文件</span>"
        "</div>"
        "</div>"
        f'<div class="download-history-scroll">{body}</div>'
        "</div>"
    )


def _output_view(
    session: Optional[Dict],
) -> Tuple[str, str, str, List[Tuple[str, str]], str, str, str]:
    run = _active_run(session)
    product_files = artifact_files_by_kind(run, {"xlsx", "pptx"}) if run else []
    package_files = artifact_files_by_kind(run, {"zip"}) if run else []
    product_html = _artifact_history_html(
        run,
        product_files,
        title="最近生成的报表 / PPT",
        empty_text="本次任务暂未生成报表或 PPT",
        status_label="已生成",
    )
    package_html = _artifact_history_html(
        run,
        package_files,
        title="下载图片包",
        empty_text="本次任务暂未生成图片包",
        status_label="已完成",
    )
    return (
        _artifact_summary_html(run),
        product_html,
        _image_summary_html(run),
        image_items(run, limit=20) if run else [],
        package_html,
        _log_summary_html(run),
        _download_history_html(session),
    )


def _open_run_folder(session: Optional[Dict]) -> None:
    run = _active_run(session)
    if not run:
        raise gr.Error("当前还没有可打开的输出目录。")
    dirs = output_dirs(run)
    if not dirs:
        raise gr.Error("当前任务没有可用输出目录。")
    _open_download_dir(dirs[0])


# =====================================================================
# 聊天主回调
# =====================================================================
def _normalize_chat_history(history: list):
    normalized = []
    for item in history or []:
        if isinstance(item, dict) and "role" in item and "content" in item:
            normalized.append({"role": item["role"], "content": item.get("content") or ""})
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            user_text = (item[0] or "").strip()
            assistant_text = (item[1] or "").strip()
            if user_text:
                normalized.append({"role": "user", "content": user_text})
            if assistant_text:
                normalized.append({"role": "assistant", "content": assistant_text})
    return normalized


def append_message(message: str, history: list):
    history = _normalize_chat_history(history)
    message = (message or "").strip()
    if not message:
        return "", history, ""
    return "", history + [{"role": "user", "content": message}], message


def clear_chat(session: Optional[Dict] = None):
    next_session = copy.deepcopy(session or {})
    next_session["vpn_state"] = "unknown"
    next_session["messages"] = []
    return "", [], next_session


# 首次启动时只提示一次保存路径（同一进程内不重复打扰）
_FIRST_RUN_NOTICED = False


def first_run_path_notice(history: list):
    """初次运行：告知默认下载路径，并提示可按需自定义。"""
    global _FIRST_RUN_NOTICED
    history = _normalize_chat_history(history)
    if _FIRST_RUN_NOTICED:
        return history
    _FIRST_RUN_NOTICED = True

    saved_dir = _load_download_dir_preference()
    default_path = _resolve_download_dir(saved_dir)
    example_download_path = Path.home() / "Downloads" / "检判原图"
    history.append(
        {
            "role": "assistant",
            "content": (
                f"📁 **图像保存路径**：`{saved_dir}`\n\n"
                f"当前解析为：`{default_path}`\n\n"
                "该路径会被记住，下次打开时自动恢复。\n\n"
                "**是否需要自定义下载路径？**\n"
                "- 用默认：无需任何操作，直接发送任务即可\n"
                "- 要自定义：在左侧「图像保存路径」里填入目标目录（如 "
                f"`{example_download_path}`），再发送任务"
            ),
        }
    )
    return history


# =====================================================================
# 快捷 prompt（左侧指令台）—— 点选后预览，再「填入输入框」由用户回车发送
# =====================================================================
def _quick_prompts(
    current_day: Optional[date] = None,
) -> Dict[str, Dict[str, Dict[str, str]]]:
    day = current_day or date.today()
    yesterday = (day - timedelta(days=1)).strftime("%Y-%m-%d")
    week_start = (day - timedelta(days=7)).strftime("%Y-%m-%d")
    today_s = day.strftime("%Y-%m-%d")
    complete_week_start, complete_week_end = last_complete_7_days(day)
    return {
        "永锋钢铁": {
            "打包带钢卷": {
                "昨日打包带情况": "发昨天的打包带情况",
                "下载昨日打包带异常图": "下载昨天打包带的异常图片",
            },
            "烧结矿颗粒度": {
                "昨日准确率报表": f"生成 {yesterday} 的烧结矿颗粒度人工筛分 vs 视觉准确率报表",
                "指定区间准确率报表": (
                    f"生成 {complete_week_start} 到 {complete_week_end} 的"
                    "烧结矿颗粒度人工筛分 vs 视觉准确率报表"
                ),
                "支持哪些指令": (
                    "请列出你支持的所有功能和典型用法示例。"
                    "分【打包带钢卷 @ 永锋】【烧结矿颗粒度 @ 永锋】【检判原图上传下载 @ 永锋】"
                    "【废钢检判 @ 镔鑫】【球机图像下载 @ 镔鑫】【用友检判统计 @ 镔鑫】【废钢检判 @ 盛隆】回答。"
                ),
            },
            "检判原图上传下载": {
                "下载昨日检判原图": f"下载 {yesterday} 的【永锋】检判原图",
                "下载近 7 天检判原图": (
                    f"下载 {complete_week_start} 到 {complete_week_end} 的【永锋】检判原图"
                ),
                "指定日期下载": "下载 2026-09-01 的【永锋】检判原图",
                "指定多个日期下载": (
                    "下载 2026-08-31、2026-09-01 的【永锋】检判原图"
                ),
                "上传昨日数据集": f"上传 {yesterday} 的【永锋】数据集",
                "上传指定日期数据集": "上传 2026-09-01 的【永锋】数据集",
                "上传多个日期数据集": (
                    "上传 2026-08-31、2026-09-01 的【永锋】数据集"
                ),
            },
            "废钢视觉人工对齐": {
                "对齐人工检判表": "对齐【永锋】废钢视觉质检实绩和人工检判结果",
            },
        },
        "镔鑫钢铁": {
            "废钢检判": {
                "昨日废钢检判情况": f"发 {yesterday} 的【镔鑫】废钢检判情况",
                "近 7 天报表 + 错判图": (
                    f"导出 {complete_week_start} 到 {complete_week_end} 的"
                    "【镔鑫】废钢检判报表并下载错判图"
                ),
                "近 7 天 PPT 汇报页": (
                    f"按 {complete_week_start} 到 {complete_week_end} 的"
                    "【镔鑫】检判结果生成对应的 PPT 汇报页"
                ),
                "近 7 天各料型统计+汇报": (
                    f"统计 {complete_week_start} 到 {complete_week_end} 的"
                    "【镔鑫】各料型周度数据并导出报表"
                ),
                "支持哪些指令": (
                    "请列出你支持的所有功能和典型用法示例。"
                    "分【打包带钢卷 @ 永锋】【废钢检判 @ 镔鑫】【球机图像下载 @ 镔鑫】【用友检判统计 @ 镔鑫】【废钢检判 @ 盛隆】五节回答。"
                ),
            },
            "球机图像下载+重命名": {
                "下载昨日球机图像": f"下载 {yesterday} 的镔鑫球机图像",
                "下载球机图像": f"下载 {week_start} 到 {today_s} 的镔鑫球机图像",
            },
            "用友检判统计": {
                "下载昨日检判结果图": f"下载 {yesterday} 的用友检判结果图片并生成统计 Excel",
                "下载用友检判结果图": f"下载 {week_start} 到 {today_s} 的用友检判结果图片并生成统计 Excel",
            },
        },
        "盛隆钢铁": {
            "检判原图下载": {
                "下载昨日检判原图": f"下载 {yesterday} 的【盛隆】检判原图",
                "下载近 7 天检判原图": (
                    f"下载 {complete_week_start} 到 {complete_week_end} 的【盛隆】检判原图"
                ),
                "指定日期下载": "下载 2026-08-01 的【盛隆】检判原图",
                "指定多个日期下载": (
                    "下载 2026-08-01、2026-08-03、2026-08-05 的【盛隆】检判原图"
                ),
                "确认打包多标签（全部已筛日期）": (
                    "确认打包保存目录下已筛完的【盛隆】废钢多标签分类数据集"
                ),
                "确认打包指定日期多标签": (
                    "确认打包 2026-08-01、2026-08-03 的【盛隆】废钢多标签分类数据集"
                ),
            },
            "废钢检判": {
                "昨日废钢检判情况": f"发 {yesterday} 的【盛隆】废钢检判情况",
                "近 7 天报表": (
                    f"导出 {complete_week_start} 到 {complete_week_end} 的"
                    "【盛隆】废钢检判报表"
                ),
                "主表（多周期累积）": (
                    "生成【盛隆】主表，把这两个周期累积到一个 xlsx："
                    "2026-04-14 至 2026-04-22、2026-04-23 至 2026-04-29"
                ),
                "重废归一化主表": (
                    "生成【盛隆】重废1/2/3归一化准确率主表，把这几个周期累积到一个 xlsx："
                    "准确率统计时排除人工检判结果中没有任意重废1/2/3料型的车次；"
                    "2026-04-14 至 2026-04-22、2026-04-23 至 2026-04-29、"
                    "2026-04-30 至 2026-05-06、2026-05-07 至 2026-05-13；"
                    "其中 2026-04-30 至 2026-05-06、2026-05-07 至 2026-05-13 "
                    "当作一个统计周期进行统计"
                ),
            }
        },
    }


# =====================================================================
# 设计令牌 + 主题 + CSS（浅色企业工作台）
#   L0 页面灰底 / L1 白底描边卡片 / L2 卡内分区 / L3 控件
# =====================================================================
THEME = gr.themes.Soft(
    primary_hue="blue",
    secondary_hue="orange",
    neutral_hue="slate",
    # 现场多为内网 / VPN，GoogleFont 取不到会静默回退，直接使用系统字体栈
    font=[
        "Source Han Sans SC",
        "Microsoft YaHei",
        "PingFang SC",
        "system-ui",
        "sans-serif",
    ],
    font_mono=["JetBrains Mono", "Consolas", "Menlo", "monospace"],
).set(
    body_background_fill="#f4f6f9",
    body_text_color="#0f172a",
    background_fill_primary="#ffffff",
    background_fill_secondary="#f8fafc",
    border_color_primary="#e3e8ef",
    border_color_accent="#1983F0",
    color_accent_soft="#eff6ff",
    panel_background_fill="#ffffff",
    block_background_fill="#ffffff",
    block_border_color="#e3e8ef",
    block_border_width="1px",
    block_label_background_fill="#ffffff",
    block_label_text_color="#475569",
    block_title_text_color="#0f172a",
    block_radius="12px",
    # 组件一律不投影：白框内部的按钮/选项下方不该出现深色阴影
    block_shadow="none",
    input_background_fill="#ffffff",
    input_border_color="#d8dee9",
    input_border_color_focus="#1983F0",
    input_border_width="1px",
    input_radius="10px",
    input_shadow="none",
    input_shadow_focus="0 0 0 3px rgba(25,131,240,.12)",
    button_primary_background_fill="#1983F0",
    button_primary_background_fill_hover="#0F6FD6",
    button_primary_border_color="#1983F0",
    button_primary_text_color="#ffffff",
    button_secondary_background_fill="#ffffff",
    button_secondary_background_fill_hover="#f1f5f9",
    button_secondary_border_color="#e3e8ef",
    button_secondary_text_color="#1e293b",
    button_large_radius="10px",
    button_small_radius="10px",
    shadow_drop="none",
    shadow_drop_lg="none",
)

CUSTOM_CSS = """
/* =========================================================
   BriefMe · 浅色企业工作台
   L0 页面灰底 / L1 白底描边卡片 / L2 卡内分区 / L3 控件
   ========================================================= */
:root {
    --bm-bg: #f4f6f9;
    --bm-card: #ffffff;
    --bm-border: #e3e8ef;
    --bm-divider: #eef1f5;
    --bm-soft: #f8fafc;
    --bm-navy: #1983F0;
    --bm-blue: #1983F0;
    --bm-blue-dark: #0F6FD6;
    --bm-blue-soft: #eff6ff;
    --bm-amber: #e08a1e;
    --bm-ok: #16a34a;
    --bm-fail: #dc2626;
    --bm-warn: #d97706;
    --bm-text: #0f172a;
    --bm-text-2: #475569;
    --bm-text-3: #94a3b8;
    --bm-radius: 12px;
    --bm-shadow: 0 1px 2px rgba(15,23,42,.05), 0 8px 24px rgba(15,23,42,.06);
}

/* ---------- L0 页面 ---------- */
.gradio-container {
    background: var(--bm-bg) !important;
    max-width: 1680px !important;
    width: 100% !important;
    margin: 0 auto !important;
    padding: 0 20px 28px !important;
    box-sizing: border-box !important;
    /* Gradio 默认 overflow:hidden，会让应用栏的 sticky 失效 */
    overflow: visible !important;
    font-family: "Source Han Sans SC", "Microsoft YaHei", "PingFang SC",
        system-ui, -apple-system, "Segoe UI", sans-serif !important;
    color: var(--bm-text);
}
footer { display: none !important; }

/* 纯 HTML 标签块：不占位、不加内边距 */
.gradio-container .bare {
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    min-height: 0 !important;
}
.gradio-container .bare .prose {
    padding: 0 !important;
    max-width: 100% !important;
    color: inherit !important;
    line-height: 1.5 !important;
}
.gradio-container .bare .html-container {
    padding: 0 !important;
    margin: 0 !important;
}
.gradio-container .bare .prose p { margin: 0 !important; }

/* ---------- 顶部应用栏 ---------- */
.app-bar {
    position: sticky;
    top: 0;
    z-index: 30;
    align-items: center !important;
    gap: 16px !important;
    padding: 12px 18px !important;
    margin: 0 0 16px !important;
    flex-wrap: wrap !important;
    width: 100% !important;
    box-sizing: border-box !important;
    border: 0;
    border-bottom: 1px solid var(--bm-border);
    border-radius: 0;
    background: rgba(244, 246, 249, .92);
    box-shadow: none;
    -webkit-backdrop-filter: blur(10px);
    backdrop-filter: blur(10px);
}
/* 状态区整体不压缩：放不下时整列换行，而不是把 badge 折成两行 */
.app-bar .brand-col { flex: 1 1 0 !important; min-width: 280px !important; }
.app-bar .status-col {
    flex: 0 0 auto !important;
    width: max-content !important;
    max-width: 100% !important;
    min-width: 0 !important;
    margin-left: auto;
}
.app-bar .brand-col,
.app-bar .status-col {
    gap: 0 !important;
    padding: 0 !important;
    box-sizing: border-box !important;
}
.app-bar .brand-col { justify-content: flex-start; }
.app-bar .status-col { justify-content: flex-end; }
.brand { display: flex; align-items: center; gap: 0; min-width: 0; }
.brand-text {
    display: flex;
    flex-direction: column;
    gap: 5px;
    min-width: 0;
    transform: translateY(-2px);
}
.brand-title {
    font-size: 19px;
    font-weight: 700;
    line-height: 1.25;
    color: var(--bm-text);
    letter-spacing: .2px;
    white-space: nowrap;
}
.brand-title .brand-accent { color: var(--bm-navy); }
.brand-title .brand-sep { color: var(--bm-text-3); font-weight: 400; margin: 0 7px; }
.brand-sub { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.brand-org {
    font-size: 11.5px;
    font-weight: 600;
    color: var(--bm-navy);
    background: var(--bm-blue-soft);
    border: 1px solid #dbeafe;
    border-radius: 999px;
    padding: 1px 9px;
}
/* ---------- 状态灯 ---------- */
.status-row {
    align-items: center !important;
    justify-content: flex-end !important;
    gap: 8px !important;
    width: 100% !important;
    flex-wrap: nowrap !important;
}
.status-row .status-html { flex: 0 0 auto !important; min-width: 0 !important; }
.status-row .ghost-btn { flex: 0 0 auto !important; min-width: 0 !important; }
.status-bar {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 6px;
    flex-wrap: nowrap;
    white-space: nowrap;
}
.today-chip {
    padding: 3px 9px;
    border-radius: 999px;
    background: #eaf4fe;
    color: var(--bm-blue-dark);
    font-size: 12px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}
.status-badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 8px;
    border-radius: 999px;
    background: var(--bm-card);
    border: 1px solid var(--bm-border);
    color: var(--bm-text-2);
    font-size: 12px;
    font-weight: 500;
    white-space: nowrap;
}
.status-badge b { font-weight: 600; }
.status-badge.ok b { color: var(--bm-ok); }
.status-badge.fail b { color: var(--bm-fail); }
.status-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    display: inline-block;
    background: var(--bm-text-3);
}
.status-dot.ok {
    background: var(--bm-ok);
    box-shadow: 0 0 0 3px rgba(22, 163, 74, .14);
    animation: bm-pulse 2.4s ease-in-out infinite;
}
.status-dot.fail { background: var(--bm-fail); box-shadow: 0 0 0 3px rgba(220, 38, 38, .12); }
.status-dot.warn { background: var(--bm-warn); box-shadow: 0 0 0 3px rgba(217, 119, 6, .14); }
@keyframes bm-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: .42; }
}
/* 统一按钮动效由 CHAT_KEYBOARD_JS 的单一状态机驱动。
   CSS 只负责颜色、边框和阴影，避免位移被多套动画同时控制。 */
.gradio-container button {
    transform-origin: center !important;
    box-shadow:
        0 1px 2px rgba(15, 23, 42, .06),
        0 2px 6px rgba(15, 23, 42, .035) !important;
    transition: box-shadow .18s ease;
}
.gradio-container button:where(:hover:not(:disabled)) {
    box-shadow:
        0 3px 10px rgba(15, 23, 42, .09),
        0 1px 3px rgba(15, 23, 42, .06) !important;
}
/* 操作按钮（刷新状态 / 清空对话 / 刷新日志）：描边 + 悬停 + 按压回弹 */
.ghost-btn {
    background: #ffffff !important;
    border: 1.5px solid #cbd8e6 !important;
    color: var(--bm-text-2) !important;
    font-weight: 600 !important;
    height: 32px !important;
    padding: 0 12px !important;
    border-radius: 10px !important;
    cursor: pointer !important;
    transition: border-color .15s, background .15s, color .15s,
        box-shadow .15s;
}
.ghost-btn::before {
    content: "\\21BB";
    margin-right: 6px;
    font-size: 13px;
    color: var(--bm-navy);
    opacity: .9;
}
.ghost-btn.refresh-btn::before {
    content: "";
    width: 14px;
    height: 14px;
    flex: 0 0 14px;
    margin-right: 6px;
    background-color: var(--bm-navy);
    opacity: .92;
    -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M3 12a9 9 0 0 1 15-6.7L21 8'/%3E%3Cpath d='M21 3v5h-5'/%3E%3Cpath d='M21 12a9 9 0 0 1-15 6.7L3 16'/%3E%3Cpath d='M3 21v-5h5'/%3E%3C/svg%3E") center / contain no-repeat;
    mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M3 12a9 9 0 0 1 15-6.7L21 8'/%3E%3Cpath d='M21 3v5h-5'/%3E%3Cpath d='M21 12a9 9 0 0 1-15 6.7L3 16'/%3E%3Cpath d='M3 21v-5h5'/%3E%3C/svg%3E") center / contain no-repeat;
    transition: transform .18s ease, opacity .18s ease;
}
.ghost-btn.refresh-btn:hover::before {
    opacity: 1;
    transform: rotate(18deg);
}
.ghost-btn.head-btn::before {
    content: "";
    width: 14px;
    height: 14px;
    flex: 0 0 14px;
    margin-right: 6px;
    background-color: var(--bm-navy);
    opacity: .92;
    -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M3 6h18'/%3E%3Cpath d='M8 6V4h8v2'/%3E%3Cpath d='M19 6l-1 14H6L5 6'/%3E%3Cpath d='M10 11v5'/%3E%3Cpath d='M14 11v5'/%3E%3C/svg%3E") center / contain no-repeat;
    mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M3 6h18'/%3E%3Cpath d='M8 6V4h8v2'/%3E%3Cpath d='M19 6l-1 14H6L5 6'/%3E%3Cpath d='M10 11v5'/%3E%3Cpath d='M14 11v5'/%3E%3C/svg%3E") center / contain no-repeat;
}
.ghost-btn:hover {
    border-color: var(--bm-navy) !important;
    color: var(--bm-navy) !important;
    background: #f5f9ff !important;
    box-shadow: 0 2px 8px rgba(25, 131, 240, .16) !important;
}
.ghost-btn:active {
    background: #eaf4fe !important;
    box-shadow: none !important;
}
.ghost-btn:focus-visible {
    outline: none !important;
    box-shadow: 0 0 0 3px rgba(25, 131, 240, .22) !important;
}
/* 刷新状态时隐藏 Gradio 外置进度条，将等待动效收进按钮内部 */
.status-row .status-html [data-testid="status-tracker"] {
    display: none !important;
}
.status-row .status-html .html-container.pending {
    opacity: 1 !important;
}
.status-row:has(.status-html .html-container.pending) .ghost-btn {
    cursor: progress !important;
    color: var(--bm-navy) !important;
    border-color: rgba(25, 131, 240, .55) !important;
    background: #f5f9ff !important;
    box-shadow: 0 0 0 3px rgba(25, 131, 240, .08) !important;
}
.status-row:has(.status-html .html-container.pending) .ghost-btn::before,
.log-refresh-btn.is-refreshing::before {
    content: "";
    width: 14px;
    height: 14px;
    flex: 0 0 14px;
    margin-right: 7px;
    border-radius: 50%;
    background: conic-gradient(
        from 20deg,
        rgba(25, 131, 240, .06) 0deg,
        rgba(25, 131, 240, .22) 90deg,
        rgba(25, 131, 240, .58) 190deg,
        var(--bm-navy) 285deg,
        rgba(25, 131, 240, .08) 360deg
    );
    -webkit-mask: radial-gradient(
        farthest-side,
        transparent calc(100% - 2.5px),
        #000 calc(100% - 2px)
    );
    mask: radial-gradient(
        farthest-side,
        transparent calc(100% - 2.5px),
        #000 calc(100% - 2px)
    );
    animation: bm-win-spin .78s linear infinite;
    opacity: 1;
}
.log-refresh-btn.is-refreshing {
    cursor: progress !important;
    color: var(--bm-navy) !important;
    border-color: rgba(25, 131, 240, .55) !important;
    background: #f5f9ff !important;
}
@keyframes bm-win-spin {
    to { transform: rotate(360deg); }
}
@media (prefers-reduced-motion: reduce) {
    .status-dot.ok { animation: none; }
    .gradio-container button:where(:hover:not(:disabled)),
    .gradio-container .project-seg label:hover {
        translate: none !important;
        transition: none !important;
    }
    .status-row:has(.status-html .html-container.pending) .ghost-btn::before,
    .log-refresh-btn.is-refreshing::before {
        animation-duration: 1.8s;
    }
}

/* ---------- L1 卡片 ---------- */
.workspace {
    width: 100% !important;
    min-width: 0 !important;
    gap: 16px !important;
    align-items: flex-start !important;
    box-sizing: border-box !important;
}
.side-col, .main-col { gap: 16px !important; }
.gradio-container .card {
    background: var(--bm-card) !important;
    border: 1px solid var(--bm-border) !important;
    border-radius: var(--bm-radius) !important;
    box-shadow: var(--bm-shadow) !important;
    padding: 0 !important;
    gap: 0 !important;
    overflow: hidden;
}
.gradio-container .card.card-flat { box-shadow: 0 1px 2px rgba(15, 23, 42, .04) !important; }
/* Gradio Group 内部用描边色铺了一层底，卡片已自带 1px 边框，这里清掉 */
.gradio-container .card .styler {
    background: transparent !important;
    border: 0 !important;
    border-radius: inherit !important;
    padding: 0 !important;
    box-shadow: none !important;
}
/* Group 在 DOM 里是「外层 + 内层」两层同名容器，避免双重描边与阴影 */
.gradio-container .card .card {
    border: none !important;
    box-shadow: none !important;
}
/* 常展开的设置卡：卡内自带标题栏，无折叠箭头 */
.gradio-container .setting-card .card-body {
    padding: 12px 16px 14px !important;
    gap: 10px !important;
}
/* 上传投放区（Gradio 6 空态 = 整块可点击的 button） */
.gradio-container .card .block.upload-zone {
    border: 2px dashed #bfdbfe !important;
    border-radius: 12px !important;
    background: #f7fbff !important;
    min-height: 120px !important;
    height: 120px !important;
    padding: 0 !important;
    overflow: hidden !important;
    box-shadow:
        0 1px 3px rgba(15, 23, 42, .07),
        0 2px 7px rgba(25, 131, 240, .06) !important;
    cursor: pointer !important;
    transition: border-color .15s ease, background .15s ease, box-shadow .15s ease;
}
.gradio-container .card .block.upload-zone:hover {
    border-color: var(--bm-navy) !important;
    background: #eff7ff !important;
    box-shadow: 0 0 0 3px rgba(25, 131, 240, .10) !important;
}
.gradio-container .card .block.upload-zone:active { background: #e6f2ff !important; }
.gradio-container .card .block.upload-zone:focus-within {
    border-color: var(--bm-navy) !important;
    box-shadow: 0 0 0 3px rgba(25, 131, 240, .20) !important;
}
/* 空态按钮铺满投放区，内容居中 */
.gradio-container .upload-zone > button {
    width: 100% !important;
    min-height: 118px !important;
    height: 100% !important;
    padding: 16px 12px !important;
    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    justify-content: center !important;
    background: transparent !important;
    border: none !important;
    box-shadow:
        0 1px 3px rgba(15, 23, 42, .07),
        0 2px 7px rgba(25, 131, 240, .06) !important;
    cursor: pointer !important;
}
/* 隐藏默认英文提示，换成中文（不改 Python 结构） */
.gradio-container .upload-zone > button .wrap {
    font-size: 0 !important;
    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    justify-content: center !important;
    gap: 6px !important;
    line-height: 1.5 !important;
    min-height: 0 !important;
    height: auto !important;
}
.gradio-container .upload-zone > button .wrap .or { display: none !important; }
.gradio-container .upload-zone > button .wrap::before {
    content: "拖拽文件到此处";
    font-size: 13px !important;
    font-weight: 600 !important;
    color: var(--bm-text) !important;
}
.gradio-container .upload-zone > button .wrap::after {
    content: "或点击选择 .xlsx 文件";
    font-size: 12px !important;
    color: var(--bm-text-3) !important;
}
.gradio-container .upload-zone > button .icon-wrap {
    width: 26px !important;
    height: 26px !important;
    color: var(--bm-navy) !important;
}
.gradio-container .upload-zone > button .icon-wrap svg {
    stroke: var(--bm-navy) !important;
}
/* 已选文件态：收起虚线大框，改为紧凑行式预览 */
.gradio-container .card .block.upload-zone:has(.file-preview) {
    border-style: solid !important;
    border-color: var(--bm-border) !important;
    background: var(--bm-soft) !important;
    min-height: 0 !important;
    height: auto !important;
    box-shadow: none !important;
    cursor: default !important;
}
.gradio-container .upload-zone .file-preview,
.gradio-container .upload-zone .file-preview-holder {
    background: transparent !important;
    border: none !important;
}
/* 保存路径输入框 */
.gradio-container .card .form { background: transparent !important; }
.gradio-container .path-input { padding: 0 !important; }
.gradio-container .path-input textarea,
.gradio-container .path-input input {
    height: 38px !important;
    min-height: 38px !important;
    max-height: 38px !important;
    padding: 7px 10px !important;
    border: 2px solid var(--bm-border) !important;
    border-radius: 10px !important;
    background: var(--bm-card) !important;
    box-shadow: none !important;
    font-size: 13px !important;
    font-weight: 500 !important;
    line-height: 20px !important;
    color: var(--bm-text-2) !important;
    -webkit-text-fill-color: var(--bm-text-2) !important;
    caret-color: var(--bm-text-2) !important;
    color-scheme: light !important;
    cursor: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='20' viewBox='0 0 16 20'%3E%3Cpath d='M4 2h8M8 2v16M4 18h8' fill='none' stroke='%230f172a' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E") 8 10, text !important;
    box-sizing: border-box !important;
    white-space: pre !important;
    overflow-x: auto !important;
    overflow-y: hidden !important;
    overflow-wrap: normal !important;
    word-break: normal !important;
    resize: none !important;
    scrollbar-width: none !important;
    transition: border-color .15s ease, background .15s ease, box-shadow .15s ease;
}
.gradio-container .path-input textarea::placeholder,
.gradio-container .path-input input::placeholder {
    color: #8a9aab !important;
    -webkit-text-fill-color: #8a9aab !important;
    opacity: 1 !important;
}
.gradio-container .path-input textarea::-webkit-scrollbar,
.gradio-container .path-input input::-webkit-scrollbar {
    display: none !important;
    width: 0 !important;
    height: 0 !important;
}
.gradio-container .path-input textarea:hover,
.gradio-container .path-input input:hover {
    border-color: #bfdbfe !important;
    background: #f8fbff !important;
}
.gradio-container .path-input textarea:focus,
.gradio-container .path-input input:focus {
    border-color: var(--bm-navy) !important;
    background: var(--bm-card) !important;
    box-shadow: 0 0 0 3px rgba(25, 131, 240, .12) !important;
}
.path-actions {
    gap: 8px !important;
    flex-wrap: nowrap !important;
    margin-top: 2px !important;
}
.path-actions .path-action-btn {
    flex: 1 1 0 !important;
    min-width: 0 !important;
    height: 34px !important;
    padding: 0 10px !important;
    font-size: 12.5px !important;
    font-weight: 600 !important;
    transition: background .15s ease, border-color .15s ease,
        color .15s ease, box-shadow .15s ease;
}
.path-actions .path-action-btn.secondary {
    background: var(--bm-card) !important;
    border: 1.5px solid #cbd8e6 !important;
    color: var(--bm-text-2) !important;
    box-shadow:
        0 1px 3px rgba(15, 23, 42, .08),
        0 2px 7px rgba(25, 131, 240, .07) !important;
}
.path-actions .path-action-btn.secondary:hover {
    border-color: var(--bm-navy) !important;
    background: #f5f9ff !important;
    color: var(--bm-navy) !important;
}
/* 卡片内部一律不投影：选项、按钮、文件表格、输入框都不该在白框里压出深色影 */
.gradio-container .card label,
.gradio-container .card .block,
.gradio-container .card .wrap,
.gradio-container .card textarea,
.gradio-container .card input,
.gradio-container .card table,
.gradio-container .card tr,
.gradio-container .card .file-preview-holder,
.gradio-container .card .file-preview {
    box-shadow: none !important;
}
/* 路径操作按钮与快捷填入按钮：常驻轻阴影，悬停时增强 */
.gradio-container .card .path-actions button.path-action-btn,
.gradio-container .card .fill-btn {
    box-shadow:
        0 1px 3px rgba(15, 23, 42, .08),
        0 2px 7px rgba(25, 131, 240, .07) !important;
}
.gradio-container .card .path-actions button.path-action-btn:hover,
.gradio-container .card .fill-btn:hover {
    box-shadow: 0 3px 10px rgba(25, 131, 240, .18) !important;
}
.gradio-container .card .path-actions button.path-action-btn.secondary:hover {
    box-shadow: 0 2px 8px rgba(25, 131, 240, .14) !important;
}

.card-head {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 16px !important;
    margin: 0 !important;
    min-height: 0 !important;
    border-bottom: 1px solid var(--bm-divider) !important;
    border-radius: 0 !important;
    background: transparent !important;
}
.card-head .head-left { flex: 1 1 0 !important; min-width: 0 !important; }
.card-head .head-btn {
    flex: 0 0 auto !important;
    min-width: 0 !important;
    width: auto !important;
    white-space: nowrap;
}
.card-title {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 14px;
    font-weight: 700;
    color: var(--bm-text);
    letter-spacing: .3px;
}
.card-title::before {
    content: "";
    width: 3px;
    height: 14px;
    border-radius: 2px;
    background: var(--bm-navy);
}
.card-note {
    margin-left: auto;
    font-size: 11.5px;
    color: var(--bm-text-3);
    letter-spacing: .3px;
}

/* ---------- L2 卡内分区 ---------- */
.card-body {
    display: flex;
    flex-direction: column;
    gap: 14px !important;
    padding: 14px 16px 16px !important;
    border: none !important;
    background: transparent !important;
}
.card-section {
    display: flex;
    flex-direction: column;
    gap: 6px !important;
    padding: 0 !important;
    border: none !important;
    background: transparent !important;
}
.card-section + .card-section {
    border-top: 1px solid var(--bm-divider) !important;
    padding-top: 12px !important;
}
.section-label {
    font-size: 11.5px;
    font-weight: 600;
    letter-spacing: .4px;
    color: var(--bm-text-2);
}
/* ---------- L3 控件：项目分段（2px）+ 选项列表（2px 描边圆角） ---------- */
.gradio-container .project-seg .wrap,
.gradio-container .opt-list .wrap {
    display: flex !important;
    gap: 8px !important;
    padding: 0 !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
}
/* 保留 input 以便键盘与读屏可用，只做视觉隐藏 */
.gradio-container .project-seg input[type="radio"],
.gradio-container .opt-list input[type="radio"] {
    position: absolute !important;
    width: 1px !important;
    height: 1px !important;
    opacity: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    pointer-events: none !important;
}
.gradio-container .project-seg label:has(input:focus-visible),
.gradio-container .opt-list label:has(input:focus-visible) {
    outline: 2px solid #93c5fd;
    outline-offset: 1px;
}

/* 项目：三段式，2px 描边（比选项行细一档） */
.gradio-container .project-seg {
    overflow: visible !important;
    scrollbar-width: none !important;
    -ms-overflow-style: none !important;
}
.gradio-container .project-seg .wrap {
    flex-wrap: nowrap !important;
    overflow: visible !important;
    scrollbar-width: none !important;
    -ms-overflow-style: none !important;
}
.gradio-container .project-seg::-webkit-scrollbar,
.gradio-container .project-seg .wrap::-webkit-scrollbar {
    display: none !important;
    width: 0 !important;
    height: 0 !important;
}
.gradio-container .project-seg label {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 8px 4px !important;
    border: 2px solid var(--bm-border) !important;
    border-radius: 10px !important;
    background: var(--bm-card) !important;
    color: var(--bm-text-2) !important;
    font-size: 13px !important;
    font-weight: 600 !important;
    text-align: center;
    cursor: pointer;
    box-shadow:
        0 1px 3px rgba(15, 23, 42, .07),
        0 2px 7px rgba(25, 131, 240, .06) !important;
    transition: background .15s ease, color .15s ease, border-color .15s ease,
        box-shadow .15s ease;
}
.gradio-container .project-seg label:hover {
    border-color: var(--bm-navy) !important;
    background: #f5f9ff !important;
    color: var(--bm-navy) !important;
    box-shadow: 0 2px 8px rgba(25, 131, 240, .16) !important;
}
.gradio-container .project-seg label:active {
    background: #eaf4fe !important;
    box-shadow: none !important;
}
.gradio-container .project-seg label.selected,
.gradio-container .project-seg label:has(input:checked) {
    background: #f5f9ff !important;
    border-color: var(--bm-navy) !important;
    color: var(--bm-navy) !important;
    font-weight: 700 !important;
}
/* 分段项内部文本严格居中（含选中态加粗，避免字重变化导致视觉偏移） */
.gradio-container .project-seg label,
.gradio-container .project-seg label > span {
    text-align: center !important;
    justify-content: center !important;
    padding-left: 0 !important;
    padding-right: 0 !important;
    margin: 0 !important;
}
.gradio-container .project-seg label > span {
    display: block !important;
    width: 100% !important;
    line-height: 1.3 !important;
}

/* 业务 / 指令：与项目区使用同一套字号和控件语言 */
.gradio-container .task-dropdown {
    min-height: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}
.gradio-container .task-dropdown .wrap {
    min-height: 36px !important;
    height: 36px !important;
    padding: 0 6px 0 13px !important;
    border: 2px solid var(--bm-border) !important;
    border-radius: 10px !important;
    background: var(--bm-card) !important;
    box-shadow: none !important;
    transition: background .15s ease, border-color .15s ease, box-shadow .15s ease;
}
.gradio-container .task-dropdown .wrap:hover {
    border-color: #bfdbfe !important;
    background: #f8fbff !important;
}
.gradio-container .task-dropdown .wrap:focus-within {
    border-color: var(--bm-navy) !important;
    box-shadow: 0 0 0 3px rgba(25, 131, 240, .12) !important;
}
.gradio-container .task-dropdown .wrap-inner {
    width: 100% !important;
    height: 100% !important;
    padding: 0 !important;
    gap: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}
.gradio-container .task-dropdown .secondary-wrap {
    display: flex !important;
    flex: 1 1 auto !important;
    width: 100% !important;
    min-width: 0 !important;
    align-items: center !important;
    position: relative !important;
    gap: 0 !important;
    padding: 0 0 0 4px !important;
    margin: 0 !important;
}
.gradio-container .task-dropdown input {
    width: 100% !important;
    min-width: 0 !important;
    flex: 1 1 auto !important;
    margin: 0 !important;
    padding: 0 22px 0 0 !important;
    border: 0 !important;
    font-size: 13px !important;
    font-weight: 600 !important;
    color: var(--bm-text-2) !important;
    background: transparent !important;
    box-shadow: none !important;
    caret-color: transparent !important;
}
.gradio-container .task-dropdown .icon-wrap {
    position: absolute !important;
    display: grid !important;
    top: 50% !important;
    right: 0 !important;
    width: 14px !important;
    height: 14px !important;
    flex: 0 0 14px !important;
    place-items: center !important;
    margin: 0 !important;
    transform: translateY(-50%);
    pointer-events: none !important;
    color: var(--bm-text-2) !important;
}
.gradio-container .task-dropdown .icon-wrap svg {
    display: none !important;
}
.gradio-container .task-dropdown .icon-wrap::after {
    content: "";
    display: block !important;
    width: 14px;
    height: 14px;
    background: currentColor !important;
    -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E") center / 14px 14px no-repeat;
    mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E") center / 14px 14px no-repeat;
    transform: rotate(0deg);
    transition: transform .16s ease;
}
.gradio-container .task-dropdown:has(input[aria-expanded="true"]) .icon-wrap::after {
    transform: rotate(180deg);
}
.gradio-container .task-dropdown,
.gradio-container .task-dropdown * {
    -webkit-user-select: none !important;
    user-select: none !important;
    -webkit-user-drag: none !important;
}
.gradio-container .task-dropdown input::selection,
.gradio-container .task-dropdown *::selection {
    color: inherit !important;
    background: transparent !important;
}
.gradio-container .task-dropdown input::-moz-selection,
.gradio-container .task-dropdown *::-moz-selection {
    color: inherit !important;
    background: transparent !important;
}
.gradio-container .task-dropdown .token-remove {
    display: none !important;
}
.gradio-container .task-dropdown .options {
    max-height: min(330px, calc(100vh - 32px)) !important;
    margin-top: 12px !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 4px !important;
    padding: 5px !important;
    overflow-y: auto !important;
    border: 1px solid rgba(203, 213, 225, .92) !important;
    border-radius: 10px !important;
    background: rgba(255, 255, 255, .98) !important;
    box-shadow:
        0 14px 38px rgba(15, 23, 42, .18),
        0 2px 8px rgba(15, 23, 42, .08) !important;
    box-sizing: border-box !important;
    -webkit-backdrop-filter: blur(18px);
    backdrop-filter: blur(18px);
}
.gradio-container .task-dropdown .options.bm-dropdown-unrolling {
    overflow-y: hidden !important;
}
.gradio-container .task-dropdown .options .item {
    display: flex !important;
    width: 100% !important;
    align-items: center !important;
    gap: 10px !important;
    min-height: 36px !important;
    margin: 0 !important;
    padding: 6px 9px !important;
    border-radius: 7px !important;
    color: #24364b !important;
    background: transparent !important;
    box-shadow: none !important;
    font-size: 12.5px !important;
    text-align: left !important;
    cursor: pointer !important;
    transition: background .12s ease, color .12s ease;
    box-sizing: border-box !important;
}
.gradio-container .task-dropdown .options .item:hover {
    background: #f1f3f5 !important;
}
.gradio-container .task-dropdown .options .item.selected,
.gradio-container .task-dropdown .options .item[aria-selected="true"] {
    color: #1f2937 !important;
    background: #f3f4f6 !important;
    font-weight: 600 !important;
}
.gradio-container .task-dropdown .options .item.active:not(.selected):not([aria-selected="true"]):not(:hover) {
    color: #24364b !important;
    background: transparent !important;
    font-weight: 400 !important;
}
.gradio-container .task-dropdown .options .inner-item {
    display: block !important;
    width: 3px !important;
    height: 18px !important;
    flex: 0 0 3px !important;
    padding: 0 !important;
    overflow: hidden !important;
    border-radius: 3px !important;
    color: transparent !important;
    background: transparent !important;
    font-size: 0 !important;
}
.gradio-container .task-dropdown .options .item.selected .inner-item,
.gradio-container .task-dropdown .options .item[aria-selected="true"] .inner-item {
    background: var(--bm-navy) !important;
}
.gradio-container .task-dropdown .options .item.active:not(.selected):not([aria-selected="true"]) .inner-item {
    background: transparent !important;
}
.gradio-container .task-dropdown .options::-webkit-scrollbar { width: 7px; }
.gradio-container .task-dropdown .options::-webkit-scrollbar-thumb {
    border-radius: 10px;
    background: #cfd7e2;
}
.gradio-container .task-dropdown .options::-webkit-scrollbar-track { background: transparent; }

/* 业务 / 指令：同一套「一行一个 + 2px 描边圆角项」，行高更紧凑 */
.gradio-container .opt-list {
    padding: 0 !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    gap: 0 !important;
}
.gradio-container .opt-list > div { padding: 0 !important; }
.gradio-container .opt-list .wrap {
    flex-direction: column !important;
    flex-wrap: nowrap !important;
    gap: 6px !important;
    max-height: 232px;
    overflow-y: auto;
}
.gradio-container .opt-list label {
    display: flex;
    align-items: center;
    width: 100%;
    min-height: 0 !important;
    padding: 6px 10px !important;
    border: 2px solid var(--bm-border) !important;
    border-radius: 9px !important;
    background: var(--bm-card) !important;
    color: var(--bm-text-2) !important;
    font-size: 12.5px !important;
    font-weight: 500 !important;
    line-height: 1.35;
    cursor: pointer;
    transition: background .15s ease, color .15s ease, border-color .15s ease;
}
.gradio-container .opt-list label:hover {
    border-color: #bfdbfe !important;
    background: #f8fbff !important;
    color: var(--bm-navy) !important;
}
.gradio-container .opt-list label.selected,
.gradio-container .opt-list label:has(input:checked) {
    background: #f5f9ff !important;
    border-color: var(--bm-navy) !important;
    color: var(--bm-navy) !important;
    font-weight: 600 !important;
}
.gradio-container .opt-list .wrap::-webkit-scrollbar { width: 6px; }
.gradio-container .opt-list .wrap::-webkit-scrollbar-thumb {
    background: #d8dee9;
    border-radius: 3px;
}
.gradio-container .opt-list .wrap::-webkit-scrollbar-track { background: transparent; }

/* 预览 + 填入按钮 */
.preview-box textarea {
    font-size: 13px !important;
    line-height: 1.6 !important;
    color: var(--bm-text) !important;
    background: var(--bm-soft) !important;
    border: 1px solid #d8dee9 !important;
    border-radius: 10px !important;
    resize: vertical;
    box-shadow: none !important;
}
.gradio-container .preview-box,
.gradio-container .preview-box * {
    cursor: not-allowed !important;
}
.fill-btn {
    width: 100% !important;
    min-height: 36px !important;
    margin-top: 2px !important;
    font-weight: 600 !important;
    transition: background .15s ease, border-color .15s ease,
        color .15s ease, box-shadow .15s ease;
}

/* ---------- 对话卡片 ---------- */
.chatbot {
    border: none !important;
    background: transparent !important;
    padding: 0 !important;
    box-shadow: none !important;
}
/* Gradio 6 的真正气泡是 .message（bot / user 两种角色） */
.chatbot .message {
    font-size: 13.5px !important;
    line-height: 1.7 !important;
    padding: 10px 14px !important;
    border: 1px solid var(--bm-divider) !important;
    border-radius: 12px !important;
    background: var(--bm-soft) !important;
    color: var(--bm-text) !important;
}
.chatbot .message.bot,
.chatbot .message.assistant {
    border-radius: 12px 12px 12px 4px !important;
}
.chatbot .message.user {
    background: #eff6ff !important;
    border: 1px solid #8fc4f4 !important;
    color: var(--bm-text) !important;
    border-radius: 12px 12px 4px 12px !important;
    box-shadow: none !important;
}
/* Gradio 的内层 .message 只承载 Markdown，不再重复绘制第二层气泡 */
.chatbot .message > .message {
    padding: 0 !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}
.chatbot .message.user > .message,
.chatbot .message.user [data-testid="user"],
.chatbot .message.user .prose {
    color: var(--bm-text) !important;
    background: transparent !important;
}
.chatbot .prose { color: inherit; }
.chat-empty { text-align: center; padding: 28px 12px; color: var(--bm-text-2); }
.chat-empty-title { font-size: 16px; font-weight: 700; color: var(--bm-text); margin-bottom: 6px; }
.chat-empty-sub { font-size: 13px; line-height: 1.7; margin-bottom: 10px; }
.chat-empty-eg {
    display: inline-block;
    font-size: 12.5px;
    color: var(--bm-navy);
    background: var(--bm-blue-soft);
    border: 1px solid #dbeafe;
    border-radius: 8px;
    padding: 6px 12px;
}
/* 终止分割线只做覆盖提示，不参与卡片高度计算，避免切换时推动输入区。 */
.gradio-container .block.chat-stop-divider-wrap {
    position: relative !important;
    display: block !important;
    width: 100% !important;
    height: 0 !important;
    min-height: 0 !important;
    max-height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    overflow: visible !important;
    background: transparent !important;
    pointer-events: none !important;
    z-index: 4;
}
.gradio-container .block.chat-stop-divider-wrap > .html-container {
    height: 0 !important;
    min-height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: visible !important;
}
.gradio-container .block.chat-stop-divider-wrap .prose.chat-stop-divider-wrap {
    position: static !important;
    display: block !important;
    width: auto !important;
    height: 0 !important;
    min-height: 0 !important;
    max-height: none !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: visible !important;
    opacity: 0;
    transform: translateY(4px);
    transition: opacity .18s ease, transform .18s ease;
}
.gradio-container .block.chat-stop-divider-wrap.is-visible .prose.chat-stop-divider-wrap {
    opacity: 1;
    transform: translateY(0);
}
.gradio-container .block.chat-stop-divider-wrap .chat-stop-divider {
    position: absolute;
    top: 0;
    left: 16px;
    right: 16px;
    display: flex;
    align-items: center;
    gap: 12px;
    color: var(--bm-text-3);
    font-size: 11.5px;
    line-height: 1;
    white-space: nowrap;
    transform: translateY(-50%);
}
.gradio-container .block.chat-stop-divider-wrap .chat-stop-divider::before,
.gradio-container .block.chat-stop-divider-wrap .chat-stop-divider::after {
    content: "";
    flex: 1 1 auto;
    height: 1px;
    background: #e2e8f0;
}

/* ---------- 输入区 ---------- */
.composer {
    align-items: flex-end !important;
    gap: 10px !important;
    padding: 12px 16px 14px !important;
    margin: 0 !important;
    border-top: 1px solid var(--bm-divider) !important;
    border-bottom-left-radius: inherit;
    border-bottom-right-radius: inherit;
    box-shadow: none !important;
}
.composer-input,
.composer-input .block,
.composer-input .wrap,
.composer-input .input-container,
.composer-input textarea {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    border-radius: 10px !important;
}
.composer-input textarea {
    font-size: 13.5px !important;
    line-height: 1.6 !important;
    padding: 10px 12px !important;
    border: 1px solid var(--bm-border) !important;
    background: var(--bm-soft) !important;
    color: var(--bm-text) !important;
    caret-color: var(--bm-text) !important;
    color-scheme: light !important;
    cursor: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='20' viewBox='0 0 16 20'%3E%3Cpath d='M4 2h8M8 2v16M4 18h8' fill='none' stroke='%230f172a' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E") 8 10, text !important;
    transition: background .15s ease, border-color .15s ease, box-shadow .15s ease;
}
.composer-input textarea:focus {
    background: var(--bm-card) !important;
    border-color: var(--bm-blue) !important;
    box-shadow: 0 0 0 3px rgba(25, 131, 240, .15) !important;
}
.send-btn {
    flex: 0 0 46px !important;
    width: 100% !important;
    min-width: 0 !important;
    height: 46px !important;
    font-weight: 600 !important;
    transition: background .15s ease, border-color .15s ease,
        box-shadow .15s ease;
}
.send-btn:hover {
    background: var(--bm-blue-dark) !important;
    border-color: var(--bm-blue-dark) !important;
    box-shadow: 0 3px 10px rgba(25, 131, 240, .22) !important;
}
.send-btn:active {
    background: var(--bm-blue-dark) !important;
    box-shadow: none !important;
}
.send-actions {
    flex: 0 0 104px !important;
    min-width: 104px !important;
    gap: 6px !important;
    padding: 0 !important;
    justify-content: flex-start !important;
    align-self: stretch !important;
}
.send-actions .stop-btn {
    flex: 1 1 auto !important;
    width: 100% !important;
    height: auto !important;
    min-height: 0 !important;
    padding: 0 8px !important;
    border: 1px solid #D73800 !important;
    border-radius: 9px !important;
    background: #D73800 !important;
    color: #ffffff !important;
    font-size: 12px !important;
    font-weight: 600 !important;
    box-shadow:
        0 1px 3px rgba(15, 23, 42, .08),
        0 2px 7px rgba(215, 56, 0, .09) !important;
    transition: background .15s ease, border-color .15s ease,
        box-shadow .15s ease;
}
.send-actions .stop-btn:hover {
    border-color: #bd3100 !important;
    background: #bd3100 !important;
    color: #ffffff !important;
    box-shadow: 0 3px 10px rgba(215, 56, 0, .2) !important;
}
.send-actions .stop-btn:active {
    background: #a92c00 !important;
    border-color: #a92c00 !important;
    box-shadow: none !important;
}
.send-actions .stop-btn.is-stopping,
.send-actions .stop-btn.is-stopping:hover,
.send-actions .stop-btn.is-stopping:active {
    opacity: 1 !important;
    transform: none !important;
    translate: none !important;
    animation: none !important;
    border: 1px solid #D73800 !important;
    background: #fff5f2 !important;
    color: #D73800 !important;
    box-shadow: none !important;
    cursor: not-allowed !important;
    transition: none !important;
}
.send-actions .stop-btn.is-stopping::before {
    animation: none !important;
    scale: 1 !important;
    background: currentColor !important;
}
.send-actions .stop-btn::before {
    content: "";
    width: 8px;
    height: 8px;
    margin-right: 5px;
    border-radius: 2px;
    background: currentColor;
}

/* ---------- 底部停靠面板 ---------- */
.dock-card,
.dock,
.dock .tab-wrapper,
.dock .tabitem {
    width: 100% !important;
    max-width: 100% !important;
    min-width: 0 !important;
    box-sizing: border-box !important;
}
.dock { padding: 0 !important; background: transparent !important; overflow: hidden !important; }
.dock .tab-wrapper {
    display: flex !important;
    align-items: center !important;
    height: auto !important;
    min-height: 62px !important;
    max-height: none !important;
    padding: 12px 16px 16px !important;
    margin: 0 !important;
    border-bottom: 0 !important;
    box-shadow: none !important;
    box-sizing: border-box !important;
}
.dock .tab-wrapper::before,
.dock .tab-wrapper::after {
    display: none !important;
}
.dock .tabitem {
    border-top: 0 !important;
    box-shadow: none !important;
}
.dock .tab-container:not(.visually-hidden) {
    position: relative;
    isolation: isolate;
    display: inline-flex !important;
    align-items: center !important;
    flex: 0 0 auto !important;
    width: min(250px, 100%) !important;
    height: 36px !important;
    min-height: 36px !important;
    max-height: 36px !important;
    min-width: 0 !important;
    gap: 2px !important;
    padding: 3px !important;
    margin: 0 !important;
    overflow: hidden !important;
    border: 0 !important;
    border-radius: 11px !important;
    background: #dde2e8 !important;
    box-sizing: border-box !important;
}
.dock .tab-container:not(.visually-hidden)::before {
    content: "";
    position: absolute;
    z-index: 0;
    top: 4px;
    left: 3px;
    width: calc((100% - 10px) / 3);
    height: 28px;
    border-radius: 8px;
    background: var(--bm-navy);
    box-shadow: 0 2px 7px rgba(25, 131, 240, .22);
    pointer-events: none;
    transition: transform .28s cubic-bezier(.2, .8, .2, 1);
}
.dock .tab-container:not(.visually-hidden):has(button:nth-child(2).selected)::before {
    transform: translateX(calc(100% + 2px));
}
.dock .tab-container:not(.visually-hidden):has(button:nth-child(3).selected)::before {
    transform: translateX(calc(200% + 4px));
}
.dock .tab-container:not(.visually-hidden) button {
    position: relative;
    z-index: 1;
    display: inline-flex !important;
    flex: 1 1 0 !important;
    align-items: center !important;
    justify-content: center !important;
    width: auto !important;
    height: 28px !important;
    min-height: 28px !important;
    max-height: 28px !important;
    padding: 0 !important;
    margin: 0 !important;
    font-size: 12.5px !important;
    font-weight: 600 !important;
    line-height: 1.2 !important;
    color: var(--bm-text-2) !important;
    border: 0 !important;
    border-radius: 8px !important;
    background: transparent !important;
    box-shadow: none !important;
    transition: color .15s ease, background .15s ease, box-shadow .15s ease;
}
.dock .tab-container:not(.visually-hidden) button:hover {
    color: var(--bm-navy) !important;
    background: rgba(255, 255, 255, .72) !important;
}
.dock .tab-container:not(.visually-hidden) button.selected {
    color: #ffffff !important;
    background: transparent !important;
    box-shadow: none !important;
}
.dock .tab-container:not(.visually-hidden) button::before,
.dock .tab-container:not(.visually-hidden) button::after {
    display: none !important;
}
.dock .tabitem {
    height: 444px !important;
    min-height: 444px !important;
    max-height: 444px !important;
    padding: 14px 16px !important;
    overflow: hidden !important;
    box-sizing: border-box !important;
}
#log_textbox {
    height: 250px !important;
    min-height: 250px !important;
    max-height: 250px !important;
}
.artifact-grid { gap: 12px !important; }
.output-content-panel {
    width: 100% !important;
    height: 416px !important;
    max-height: 416px !important;
    gap: 10px !important;
    padding: 0 !important;
    box-sizing: border-box !important;
}
.output-summary-html {
    padding: 0 !important;
    margin: 0 !important;
}
.output-content-panel > .output-summary-html {
    flex: 0 0 58px !important;
    width: 100% !important;
    height: 58px !important;
    min-height: 58px !important;
}
.output-content-panel > .output-summary-html .html-container,
.output-content-panel > .output-summary-html .prose {
    width: 100% !important;
    height: 100% !important;
    padding: 0 !important;
    margin: 0 !important;
}
.output-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    min-height: 38px;
    width: 100%;
    padding: 7px 10px;
    border: 1px solid #e8edf3;
    border-radius: 9px;
    background: #f8fafc;
    box-sizing: border-box;
}
.output-summary-main,
.output-summary-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
}
.output-summary-main strong {
    overflow: hidden;
    color: var(--bm-text);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.output-summary-main span,
.output-summary-meta span {
    color: var(--bm-text-3);
    font-size: 10.5px;
}
.output-summary-meta span:first-child {
    padding: 2px 7px;
    border-radius: 999px;
    color: var(--bm-blue-dark);
    background: var(--bm-blue-soft);
}
.output-empty {
    display: flex;
    min-height: 38px;
    align-items: center;
    justify-content: center;
    color: var(--bm-text-3);
    font-size: 11px;
}
.output-artifact-files,
.output-zip-files {
    display: flex !important;
    min-height: 0 !important;
    flex: 1;
    padding: 0 !important;
    overflow: hidden !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}
.output-artifact-files .html-container,
.output-artifact-files .prose,
.output-zip-files .html-container,
.output-zip-files .prose {
    display: flex !important;
    flex-direction: column !important;
    width: 100% !important;
    height: 100% !important;
    max-width: none !important;
    min-height: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
}
.output-action-btn {
    width: max-content !important;
    min-width: 104px !important;
    height: 32px !important;
    align-self: flex-end !important;
    padding: 0 12px !important;
    font-size: 11.5px !important;
    font-weight: 600 !important;
}
.output-gallery {
    flex: 1;
    min-height: 120px !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}
.output-image-panel {
    gap: 10px !important;
}
.output-image-content-row {
    flex: 1 1 auto !important;
    width: 100% !important;
    min-height: 0 !important;
    gap: 10px !important;
    align-items: stretch !important;
}
.output-gallery-column,
.output-zip-column {
    height: 100% !important;
    min-height: 0 !important;
    padding: 0 !important;
}
.output-gallery-column {
    display: flex !important;
    flex-direction: column !important;
    flex-wrap: nowrap !important;
    align-items: stretch !important;
    overflow: hidden !important;
    border: 1px solid #e4eaf1 !important;
    border-radius: 10px !important;
    background: #f8fafc !important;
}
.artifact-panel-head-html {
    flex: 0 0 40px !important;
    min-height: 40px !important;
    padding: 0 !important;
    border: 0 !important;
    background: var(--bm-card) !important;
    box-shadow: none !important;
}
.artifact-panel-head-html .html-container,
.artifact-panel-head-html .prose {
    width: 100% !important;
    height: 100% !important;
    padding: 0 !important;
    margin: 0 !important;
}
.artifact-panel-head {
    display: flex;
    align-items: center;
    width: 100%;
    height: 100%;
    padding: 0 12px;
    border-bottom: 1px solid var(--bm-divider);
    color: var(--bm-text);
    font-size: 12px;
    box-sizing: border-box;
}
.artifact-panel-head strong {
    font-weight: 700;
}
.output-gallery-column .output-gallery,
.output-zip-column .output-zip-files {
    width: 100% !important;
    min-height: 0 !important;
}
.output-gallery-column .output-gallery {
    flex: 1 1 auto !important;
    height: auto !important;
    border-radius: 0 0 10px 10px !important;
}
.output-gallery-column .output-gallery .gallery-container,
.output-gallery-column .output-gallery .grid-wrap {
    width: 100% !important;
    height: 100% !important;
    max-height: 100% !important;
    min-height: 0 !important;
}
.output-gallery-column .output-gallery .caption-label {
    display: block !important;
    overflow: visible !important;
    white-space: normal !important;
    text-overflow: clip !important;
    word-break: break-all !important;
    font-size: 11px !important;
    line-height: 1.3 !important;
}
.output-zip-column .output-zip-files {
    height: 100% !important;
}
.output-zip-files {
    min-height: 120px !important;
}
.artifact-history {
    min-height: 0 !important;
}
.artifact-history-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    width: 100%;
}
.artifact-history-actions {
    grid-area: action;
    display: flex;
    align-items: center;
    gap: 6px;
}
.artifact-download-link {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    height: 24px;
    padding: 0 9px;
    border: 1px solid var(--bm-blue);
    border-radius: 7px;
    background: var(--bm-blue);
    color: #ffffff !important;
    font-size: 10.5px;
    font-weight: 600;
    line-height: 1;
    text-decoration: none !important;
    white-space: nowrap;
}
.artifact-download-link:hover {
    border-color: var(--bm-blue-dark);
    background: var(--bm-blue-dark);
}
}
.dock-card .generating,
.dock-card .eta-bar,
.dock-card .progress-text,
.dock-card .streaming-bar,
.dock-card [class*="streaming-bar"],
.dock-card [data-testid="status-tracker"] {
    display: none !important;
}
.dock-card .output-artifact-files .wrap,
.dock-card .output-gallery .wrap,
.dock-card .output-zip-files .wrap {
    border: 0 !important;
    outline: none !important;
    box-shadow: none !important;
}
.output-log-panel {
    display: grid !important;
    grid-template-rows: auto minmax(0, 1fr) 32px;
    gap: 8px !important;
    height: 416px !important;
    max-height: 416px !important;
    min-height: 0 !important;
}
.output-log-history {
    overflow: hidden !important;
    min-height: 0 !important;
    height: 100% !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}
.output-log-panel .output-summary-html {
    width: 100% !important;
    height: auto !important;
    min-height: 0 !important;
    padding: 0 !important;
}
.output-log-panel .output-summary-html .html-container,
.output-log-panel .output-summary-html .prose {
    width: 100% !important;
    height: 100% !important;
    padding: 0 !important;
    margin: 0 !important;
}
.current-task-status {
    display: flex;
    flex-direction: column;
    width: 100%;
    height: auto;
    min-height: 102px;
    gap: 0;
    padding: 0;
    overflow: hidden;
    border: 1px solid #e4eaf1;
    border-radius: 10px;
    background: var(--bm-card);
    box-sizing: border-box;
}
.current-task-status-title {
    display: flex;
    align-items: center;
    flex: 0 0 30px;
    padding: 0 12px;
    border-bottom: 0;
    color: var(--bm-text);
    font-size: 11.5px;
    font-weight: 700;
}
.current-task-item {
    display: flex;
    flex-direction: column;
    justify-content: center;
    flex: 0 0 auto;
    min-width: 0;
    width: auto;
    min-height: 56px;
    margin: 12px !important;
    gap: 4px;
    padding: 6px 10px;
    border: 1px solid #e1e8f0;
    border-radius: 10px;
    background: #f8fafc;
    box-sizing: border-box;
}
.current-task-item-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
}
.current-task-item-head strong {
    overflow: hidden;
    color: var(--bm-text);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.current-task-item-progress {
    overflow: hidden;
    color: var(--bm-text-3);
    font-size: 10px;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.current-task-item-progress span {
    overflow: hidden;
    color: var(--bm-text-3);
    font-size: 10px;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.current-task-item-empty {
    align-items: center;
    color: var(--bm-text-3);
    font-size: 11px;
}
.output-log-history .html-container {
    display: flex !important;
    flex-direction: column !important;
    width: 100% !important;
    height: 100% !important;
    max-width: none !important;
    padding: 0 !important;
    margin: 0 !important;
}
.output-log-history .prose {
    flex: 1 !important;
    width: 100% !important;
    height: 100% !important;
    max-width: none !important;
    min-height: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
}
.download-history {
    display: flex;
    flex-direction: column;
    width: 100%;
    height: 100%;
    min-height: 0;
    overflow: hidden;
    border: 1px solid #e4eaf1;
    border-radius: 10px;
    background: #f8fafc;
}
.download-history-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex: 0 0 40px;
    padding: 0 12px;
    border-bottom: 1px solid var(--bm-divider);
    background: var(--bm-card);
    color: var(--bm-text);
    font-size: 12px;
}
.download-history-title {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
}
.download-history-title span {
    color: var(--bm-text-3);
    font-size: 10.5px;
}
.download-history-expand {
    height: 26px !important;
    padding: 0 9px !important;
    border: 1px solid #d7e1ec !important;
    border-radius: 7px !important;
    background: #ffffff !important;
    color: var(--bm-text-2) !important;
    font-size: 10.5px !important;
    font-weight: 600 !important;
    white-space: nowrap;
    transition: border-color .15s ease, background .15s ease,
        color .15s ease, box-shadow .15s ease;
}
.download-history-expand:hover {
    border-color: var(--bm-blue) !important;
    background: #eff6ff !important;
    color: var(--bm-blue-dark) !important;
    box-shadow: 0 2px 8px rgba(25, 131, 240, .16) !important;
}
.download-history-expand .collapse-label {
    display: none;
}
.download-history-expand .expand-label,
.download-history-expand .collapse-label {
    color: inherit !important;
    transition: color .15s ease;
}
.output-log-history.bm-log-expanded .download-history-expand .expand-label {
    display: none;
}
.output-log-history.bm-log-expanded .download-history-expand .collapse-label {
    display: inline;
}
.output-log-history.bm-log-expanded {
    position: fixed !important;
    inset: 20px !important;
    z-index: 10001 !important;
    width: auto !important;
    height: auto !important;
    padding: 14px !important;
    overflow: hidden !important;
    border: 1px solid #dbe4ee !important;
    border-radius: 14px !important;
    background: #f4f6f9 !important;
    box-shadow: 0 24px 70px rgba(15, 23, 42, .28) !important;
}
.output-log-history.bm-log-expanded .html-container,
.output-log-history.bm-log-expanded .prose,
.output-log-history.bm-log-expanded .download-history {
    height: 100% !important;
    min-height: 0 !important;
}
body.bm-log-expanded-open {
    overflow: hidden !important;
}
body.bm-log-expanded-open::before {
    content: "";
    position: fixed;
    inset: 0;
    z-index: 10000;
    background: rgba(15, 23, 42, .18);
    pointer-events: none;
}
.download-history-scroll {
    display: flex;
    flex-direction: column;
    gap: 10px;
    flex: 1;
    min-height: 0;
    padding: 10px;
    overflow-x: hidden !important;
    overflow-y: auto !important;
    scrollbar-width: thin;
    scrollbar-color: #c3ceda transparent;
    scrollbar-gutter: stable;
    overscroll-behavior: contain;
}
.download-history-scroll::-webkit-scrollbar {
    width: 8px !important;
}
.download-history-scroll::-webkit-scrollbar-track {
    background: transparent;
}
.download-history-scroll::-webkit-scrollbar-thumb {
    border: 2px solid transparent;
    border-radius: 999px;
    background: #c3ceda;
    background-clip: padding-box;
}
.download-task-card {
    flex: 0 0 auto;
    width: 100%;
    box-sizing: border-box;
    overflow: hidden;
    border: 1px solid #e4eaf1;
    border-radius: 11px;
    background: var(--bm-card);
    box-shadow: 0 1px 2px rgba(15, 23, 42, .035);
}
.download-task-empty {
    padding: 14px 12px;
    color: var(--bm-text-3);
    background: var(--bm-card);
    font-size: 10.5px;
    text-align: center;
}
.download-task-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 12px;
    border-bottom: 1px solid #eef2f6;
    background: #ffffff;
}
.download-task-title {
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
}
.download-task-title strong {
    overflow: hidden;
    color: var(--bm-text);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.download-task-title span {
    overflow: hidden;
    color: var(--bm-text-3);
    font-size: 10.5px;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.download-task-meta {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 7px 12px;
    border-bottom: 1px solid #f1f4f8;
    color: var(--bm-text-3);
    background: #f8fafc;
    font-size: 10.5px;
}
.download-task-meta span:last-child {
    overflow: hidden;
    min-width: 0;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.download-task-files {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 8px;
}
.download-file-item {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(180px, .8fr) auto;
    grid-template-areas:
        "main meta action"
        "location location action";
    align-items: center;
    gap: 4px 12px;
    min-width: 0;
    padding: 8px 9px;
    border: 1px solid #edf1f5;
    border-radius: 9px;
    background: #ffffff;
    transition: border-color .15s ease, background .15s ease;
}
.download-file-item:hover {
    border-color: #d8e6f5;
    background: #fbfdff;
}
.download-file-main {
    grid-area: main;
    display: flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
}
.download-file-main strong {
    overflow: hidden;
    color: var(--bm-text-2);
    font-size: 11.5px;
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.download-file-kind {
    flex: 0 0 auto;
    padding: 2px 5px;
    border-radius: 5px;
    color: #58708a;
    background: #eef3f8;
    font-size: 9px;
    font-weight: 700;
    line-height: 1.2;
}
.download-file-meta {
    grid-area: meta;
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    color: var(--bm-text-3);
    font-size: 10px;
    white-space: nowrap;
}
.download-file-location {
    grid-area: location;
    overflow: hidden;
    color: #8a9aab;
    font-size: 9.5px;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.download-status {
    display: inline-flex;
    align-items: center;
    height: 20px;
    padding: 0 7px;
    border-radius: 999px;
    font-size: 10px;
    white-space: nowrap;
}
.download-status.success { color: #15803d; background: #ecfdf3; }
.download-status.running { color: var(--bm-blue-dark); background: var(--bm-blue-soft); }
.download-status.failed { color: #b42318; background: #fff1f0; }
.download-status.cancelled { color: #b45309; background: #fff7e8; }
.download-status.written { color: var(--bm-text-2); background: #f1f3f5; }
.download-open-folder {
    grid-area: action;
    position: relative;
    height: 24px !important;
    padding: 0 9px 0 24px !important;
    border: 1px solid #d7e1ec !important;
    border-radius: 7px !important;
    background: var(--bm-card) !important;
    color: var(--bm-blue-dark) !important;
    font-size: 10.5px !important;
    font-weight: 600 !important;
    white-space: nowrap;
}
.download-open-folder::before {
    content: "";
    position: absolute;
    left: 8px;
    top: 50%;
    width: 11px;
    height: 11px;
    translate: 0 -50%;
    background: currentColor;
    -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M3 7h7l2 2h9v9H3z'/%3E%3Cpath d='M3 7V5h7l2 2'/%3E%3C/svg%3E") center / contain no-repeat;
    mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M3 7h7l2 2h9v9H3z'/%3E%3Cpath d='M3 7V5h7l2 2'/%3E%3C/svg%3E") center / contain no-repeat;
}
.output-log-panel .ghost-btn {
    align-self: flex-end;
}
.download-history-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 100%;
    min-height: 120px;
    border: 1px solid #e8edf3;
    border-radius: 10px;
    color: var(--bm-text-3);
    background: #f8fafc;
    font-size: 11px;
}
.artifact-block {
    background: var(--bm-soft) !important;
    border: 1px solid var(--bm-divider) !important;
    border-radius: 10px !important;
    padding: 10px 12px !important;
    gap: 8px !important;
    align-items: stretch !important;
    text-align: center !important;
}
/* 产物卡片内部：标题与文件单元格一律水平、垂直居中 */
.artifact-block .section-label {
    text-align: center !important;
    justify-content: center !important;
    align-self: center !important;
    width: 100% !important;
}
.artifact-block .file-preview-holder,
.artifact-block .file-preview,
.artifact-block .file,
.artifact-block .file-preview table,
.artifact-block .file-preview tbody,
.artifact-block .file-preview tr,
.artifact-block .file-preview td,
.artifact-block .file-preview td.filename,
.artifact-block .file-preview td.download,
.artifact-block .file-preview .stem,
.artifact-block .file-preview span,
.artifact-block .file-preview a {
    text-align: center !important;
    justify-content: center !important;
    align-items: center !important;
    background: transparent !important;
}
.artifact-block .file-preview table {
    width: 100% !important;
    margin: 0 auto !important;
}
.artifact-block .file-preview td.filename,
.artifact-block .file-preview td.download {
    padding-left: 0 !important;
    padding-right: 0 !important;
}
.empty-hint { font-size: 12.5px; color: var(--bm-text-3); padding: 6px 0; }

/* ---------- 圆角统一：消灭残留方角 ---------- */
.gradio-container .card .block,
.gradio-container .card .wrap { border-radius: 10px !important; }
.gradio-container .file-preview-holder,
.gradio-container .file-preview,
.gradio-container .file {
    border-radius: 10px !important;
    overflow: hidden;
}
.preview-box textarea,
#log_textbox textarea { border-radius: 10px !important; }
.gradio-container .chat-empty-eg { border-radius: 10px !important; }
.gradio-container .card .label-wrap { border-radius: 10px !important; }

/* ---------- 按钮文字居中 ---------- */
.gradio-container button:not(.label-wrap) {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    text-align: center !important;
    line-height: 1.2 !important;
    border-radius: 10px !important;
}
.gradio-container .card .label-wrap {
    display: flex !important;
    align-items: center !important;
    justify-content: space-between !important;
    line-height: 1.2 !important;
}

/* ---------- 响应式 ---------- */
@media (max-width: 1180px) {
    .workspace {
        flex-direction: column !important;
        align-items: stretch !important;
    }
    .side-col,
    .main-col {
        width: 100% !important;
        min-width: 0 !important;
        flex: 1 1 auto !important;
    }
    .app-bar { flex-wrap: wrap !important; }
    .app-bar .status-col {
        flex-basis: 100% !important;
        width: 100% !important;
        max-width: 100% !important;
    }
    .status-row { flex-wrap: wrap !important; }
}
@media (max-width: 720px) {
    .gradio-container { padding: 0 12px 20px !important; }
    .app-bar {
        top: 0;
        gap: 16px !important;
        padding: 12px 14px !important;
    }
    .app-bar .brand-col {
        flex-basis: 100% !important;
        min-width: 0 !important;
    }
    .status-bar {
        justify-content: flex-end;
        flex-wrap: wrap;
        row-gap: 6px;
    }
    .brand-title { font-size: 17px; }
    .project-seg label { font-size: 12px !important; }
    .send-actions {
        flex-basis: 88px !important;
        min-width: 88px !important;
    }
    .send-btn { min-width: 72px !important; }
}
"""


REFRESH_LOG_JS = r"""
() => {
    const button = document.querySelector(".log-refresh-btn");
    if (!button) return;
    button.classList.add("is-refreshing");
    window.clearTimeout(button.__briefMeRefreshTimer);
    button.__briefMeRefreshTimer = window.setTimeout(() => {
        button.classList.remove("is-refreshing");
    }, 900);
}
"""


CHAT_KEYBOARD_JS = r"""
(() => {
    const installKey = "__briefMeComposerKeysInstalled";
    if (window[installKey]) return;
    window[installKey] = true;

    // Gradio 下拉菜单展开后，再次点击箭头不会关闭。捕获本次点击并转为 Esc。
    document.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        const dropdown = event.target.closest(".task-dropdown");
        if (!dropdown || event.target.closest(".options")) return;

        const input = dropdown.querySelector('input[role="listbox"]');
        if (!input) return;

        event.preventDefault();
        event.stopImmediatePropagation();
        input.focus();
        const expanded = input.getAttribute("aria-expanded") === "true";
        setTimeout(() => {
            input.dispatchEvent(new KeyboardEvent("keydown", {
                key: expanded ? "Escape" : "ArrowDown",
                code: expanded ? "Escape" : "ArrowDown",
                bubbles: true,
                cancelable: true,
            }));
        }, 0);
    }, true);

    const alignDropdownMenu = (dropdown) => {
        const field = dropdown?.querySelector(".wrap:not(.hide)");
        const menu = dropdown?.querySelector(".options");
        if (!field || !menu) return;
        const rect = field.getBoundingClientRect();
        menu.style.left = `${rect.left}px`;
        menu.style.width = `${rect.width}px`;
        menu.style.minWidth = `${rect.width}px`;
    };

    const alignVisibleDropdownMenus = () => {
        document
            .querySelectorAll('.task-dropdown input[aria-expanded="true"]')
            .forEach((input) => alignDropdownMenu(input.closest(".task-dropdown")));
    };

    const dropdownOpenStates = new WeakMap();
    const dropdownOpenAnimations = new WeakMap();
    const dropdownReducedMotion = window.matchMedia(
        "(prefers-reduced-motion: reduce)"
    );

    const stopDropdownOpenAnimation = (dropdown) => {
        const animation = dropdownOpenAnimations.get(dropdown);
        if (animation) {
            animation.cancel();
            dropdownOpenAnimations.delete(dropdown);
        }
        const menu = dropdown.querySelector(".options");
        if (menu) menu.classList.remove("bm-dropdown-unrolling");
    };

    const playDropdownOpenAnimation = (dropdown) => {
        const menu = dropdown.querySelector(".options");
        if (!menu) return;

        // 取消 Gradio 默认的「从下向上滑入」，避免两套位移叠加。
        menu.getAnimations().forEach((animation) => animation.cancel());
        if (dropdownReducedMotion.matches) return;
        const menuHeight = menu.offsetHeight;
        if (!menuHeight) return;

        menu.classList.add("bm-dropdown-unrolling");
        const animation = menu.animate(
            [
                {
                    opacity: 0,
                    height: "0px",
                    transform: "translateY(-4px)",
                },
                {
                    opacity: 1,
                    height: `${menuHeight}px`,
                    transform: "translateY(0)",
                },
            ],
            {
                duration: 180,
                easing: "cubic-bezier(.2, .8, .2, 1)",
                fill: "both",
            }
        );
        animation.id = "briefme-dropdown-unroll";
        dropdownOpenAnimations.set(dropdown, animation);
        animation.finished.catch(() => {}).finally(() => {
            if (dropdownOpenAnimations.get(dropdown) !== animation) return;
            dropdownOpenAnimations.delete(dropdown);
            animation.cancel();
            menu.classList.remove("bm-dropdown-unrolling");
        });
    };

    const syncDropdownStates = () => {
        document.querySelectorAll(".task-dropdown").forEach((dropdown) => {
            const input = dropdown.querySelector('input[role="listbox"]');
            if (!input) return;
            const isOpen = input.getAttribute("aria-expanded") === "true";
            const wasOpen = dropdownOpenStates.get(dropdown) ?? false;
            if (isOpen === wasOpen) {
                if (isOpen) alignDropdownMenu(dropdown);
                return;
            }

            dropdownOpenStates.set(dropdown, isOpen);
            if (isOpen) {
                alignDropdownMenu(dropdown);
                playDropdownOpenAnimation(dropdown);
            } else {
                stopDropdownOpenAnimation(dropdown);
            }
        });
    };

    const dropdownMenuObserver = new MutationObserver(() => {
        requestAnimationFrame(syncDropdownStates);
    });
    dropdownMenuObserver.observe(document.body, {
        subtree: true,
        attributes: true,
        attributeFilter: ["aria-expanded", "style", "class"],
    });
    window.addEventListener("resize", alignVisibleDropdownMenus);

    // 原生 input 仍可能绕过 user-select 产生选区，这里强制清理。
    const clearDropdownSelection = (target) => {
        const dropdown = target?.closest?.(".task-dropdown");
        if (!dropdown) return;
        const selection = window.getSelection();
        if (selection && selection.rangeCount) selection.removeAllRanges();
        const input = dropdown.querySelector('input[role="listbox"]');
        if (input?.setSelectionRange && input.selectionStart !== input.selectionEnd) {
            input.setSelectionRange(input.value.length, input.value.length);
        }
    };
    document.addEventListener("selectstart", (event) => {
        if (event.target.closest?.(".task-dropdown")) event.preventDefault();
    }, true);
    document.addEventListener("dragstart", (event) => {
        if (event.target.closest?.(".task-dropdown")) event.preventDefault();
    }, true);
    document.addEventListener("contextmenu", (event) => {
        if (event.target.closest?.(".task-dropdown")) event.preventDefault();
    }, true);
    document.addEventListener("pointerup", (event) => {
        if (event.target.closest?.(".task-dropdown")) {
            requestAnimationFrame(() => clearDropdownSelection(event.target));
        }
    }, true);
    document.addEventListener("selectionchange", () => {
        clearDropdownSelection(document.activeElement);
    }, true);

    // Gradio 用 textarea 承载单行路径，必须显式关闭软换行才能横向查看完整内容。
    const enforceSingleLinePathInput = (root = document) => {
        root.querySelectorAll?.(".path-input textarea").forEach((textarea) => {
            textarea.setAttribute("wrap", "off");
            textarea.rows = 1;
        });
    };
    enforceSingleLinePathInput();
    document.addEventListener("focusin", (event) => {
        if (event.target.matches?.(".path-input textarea")) {
            event.target.setAttribute("wrap", "off");
            event.target.rows = 1;
        }
    }, true);

    const setElementText = (root, text) => {
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let node = walker.nextNode();
        while (node) {
            if (node.nodeValue.trim()) {
                node.nodeValue = text;
                return;
            }
            node = walker.nextNode();
        }
    };

    const setStopButtonState = (stopping, { deferDisable = false } = {}) => {
        const button = document.querySelector(".send-actions .stop-btn");
        if (!button) return;
        button.style.removeProperty("translate");
        button.style.removeProperty("scale");
        button.classList.toggle("is-stopping", stopping);
        button.disabled = stopping && !deferDisable;
        button.setAttribute("aria-busy", stopping ? "true" : "false");
        setElementText(button, stopping ? "对话已终止" : "终止对话");
        document
            .querySelector(".chat-stop-divider-wrap")
            ?.classList.toggle("is-visible", stopping);
    };

    document.addEventListener("click", (event) => {
        if (event.target.closest?.(".send-actions .send-btn")) {
            setStopButtonState(false);
            return;
        }
        const stopButton = event.target.closest?.(".send-actions .stop-btn");
        if (!stopButton || stopButton.disabled) return;
        // 视觉反馈立即生效；下一次事件循环再锁定按钮，确保本次取消请求仍能发出。
        setStopButtonState(true, { deferDisable: true });
        window.setTimeout(() => {
            if (stopButton.isConnected) stopButton.disabled = true;
        }, 0);
    }, true);

    const setLogExpanded = (host, expanded) => {
        if (!host) return;
        host.classList.toggle("bm-log-expanded", expanded);
        document.body.classList.toggle("bm-log-expanded-open", expanded);
    };

    document.addEventListener("click", (event) => {
        const button = event.target.closest?.("[data-briefme-expand-history]");
        if (!button) return;
        event.preventDefault();
        event.stopPropagation();
        const host = button.closest(".output-log-history");
        setLogExpanded(host, !host?.classList.contains("bm-log-expanded"));
    }, true);

    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        const host = document.querySelector(".output-log-history.bm-log-expanded");
        if (!host) return;
        event.preventDefault();
        setLogExpanded(host, false);
    }, true);

    document.addEventListener("click", (event) => {
        const button = event.target.closest?.("[data-briefme-file-token]");
        if (!button) return;
        event.preventDefault();
        event.stopPropagation();
        const token = button.getAttribute("data-briefme-file-token");
        if (!token || button.disabled) return;
        button.disabled = true;
        button.classList.add("is-busy");
        const originalText = button.textContent;
        button.textContent = "正在打开…";
        fetch(`/briefme/open-file?token=${encodeURIComponent(token)}`)
            .then((response) => {
                if (!response.ok) throw new Error("open failed");
            })
            .catch(() => {
                button.textContent = "打开失败";
                button.title = "文件可能已被移动、删除，或当前系统不支持定位。";
                setTimeout(() => {
                    button.textContent = originalText;
                }, 1200);
            })
            .finally(() => {
                setTimeout(() => {
                    button.disabled = false;
                    button.classList.remove("is-busy");
                    if (button.textContent === "正在打开…") {
                        button.textContent = originalText;
                    }
                }, 650);
            });
    }, true);

    // 统一按钮动效状态机：
    // hover 只抬升，pointerdown 只压缩，松开只执行一次短回弹。
    // 同一时刻每个控件最多保留一个 Web Animation，避免 CSS/JS 位移互相抢占。
    const motionSelector = "button, .project-seg label";
    const motionStates = new WeakMap();
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let pressedControl = null;
    let pressedPointerId = null;

    const getMotionState = (control) => {
        let state = motionStates.get(control);
        if (!state) {
            state = {
                hovered: false,
                pressed: false,
                rebounding: false,
                animation: null,
            };
            motionStates.set(control, state);
        }
        return state;
    };

    const canAnimateControl = (control) => {
        if (!(control instanceof HTMLElement) || !control.isConnected) return false;
        if (reducedMotion.matches) return false;
        if (control.matches(":disabled, [aria-disabled='true'], .is-stopping")) {
            return false;
        }
        if (control.closest(".task-dropdown .options")) return false;
        const rect = control.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };

    const motionMetrics = (control) => {
        const height = control.getBoundingClientRect().height;
        const compact = height <= 34 || Boolean(control.closest(".dock"));
        return {
            lift: compact ? .5 : 1,
            pressY: compact ? .35 : .65,
            pressScale: compact ? .993 : .986,
            bounce: compact ? .10 : .18,
            overshootScale: compact ? 1.0006 : 1.0012,
        };
    };

    const targetPose = (control, state) => {
        const metrics = motionMetrics(control);
        if (state.pressed) {
            return { y: metrics.pressY, scale: metrics.pressScale };
        }
        if (state.hovered) {
            return { y: -metrics.lift, scale: 1 };
        }
        return { y: 0, scale: 1 };
    };

    const applyPose = (control, pose) => {
        if (Math.abs(pose.y) < .01) {
            control.style.removeProperty("translate");
        } else {
            control.style.translate = `0 ${pose.y}px`;
        }
        if (Math.abs(pose.scale - 1) < .0001) {
            control.style.removeProperty("scale");
        } else {
            control.style.scale = String(pose.scale);
        }
    };

    const clearMotion = (control) => {
        const state = getMotionState(control);
        state.animation?.cancel();
        state.animation = null;
        state.pressed = false;
        state.rebounding = false;
        state.hovered = false;
        control.style.removeProperty("translate");
        control.style.removeProperty("scale");
    };

    const animatePose = (
        control,
        state,
        keyframes,
        options,
        finalPose,
        onFinish
    ) => {
        const computed = getComputedStyle(control);
        const fromTranslate = computed.translate || "none";
        const fromScale = computed.scale || "none";
        const frames = keyframes.map((frame, index) => {
            if (index !== 0) return frame;
            return {
                translate: fromTranslate,
                scale: fromScale,
                ...frame,
            };
        });
        state.animation?.cancel();
        const animation = control.animate(frames, {
            ...options,
            fill: "both",
        });
        animation.id = "briefme-control-motion";
        state.animation = animation;
        const finish = () => {
            if (state.animation !== animation) return;
            state.animation = null;
            applyPose(control, finalPose);
            animation.cancel();
            onFinish?.();
        };
        animation.addEventListener("finish", finish, { once: true });
    };

    const animateHover = (control) => {
        if (!canAnimateControl(control)) return;
        const state = getMotionState(control);
        const pose = targetPose(control, state);
        animatePose(
            control,
            state,
            [
                { offset: 0 },
                {
                    translate: `0 ${pose.y}px`,
                    scale: String(pose.scale),
                },
            ],
            {
                duration: 150,
                easing: "cubic-bezier(.2, .72, .2, 1)",
            },
            pose
        );
    };

    const animateRest = (control) => {
        if (!canAnimateControl(control)) return;
        const state = getMotionState(control);
        animatePose(
            control,
            state,
            [
                { offset: 0 },
                { offset: 1, translate: "0 0px", scale: "1" },
            ],
            {
                duration: 170,
                easing: "cubic-bezier(.2, .72, .2, 1)",
            },
            { y: 0, scale: 1 }
        );
    };

    const releaseControl = (control) => {
        if (!(control instanceof HTMLElement) || !control.isConnected) return;
        const state = getMotionState(control);
        if (!state.pressed) return;

        state.pressed = false;
        state.rebounding = true;
        const pose = targetPose(control, state);
        const metrics = motionMetrics(control);

        animatePose(
            control,
            state,
            [
                { offset: 0 },
                {
                    offset: .62,
                    translate: `0 ${pose.y - metrics.bounce}px`,
                    scale: String(metrics.overshootScale),
                },
                {
                    offset: 1,
                    translate: `0 ${pose.y}px`,
                    scale: String(pose.scale),
                },
            ],
            {
                duration: 200,
                easing: "cubic-bezier(.2, .8, .2, 1)",
            },
            pose,
            () => {
                state.rebounding = false;
            }
        );
    };

    document.addEventListener("pointerover", (event) => {
        if (event.pointerType && event.pointerType !== "mouse") return;
        const control = event.target.closest?.(motionSelector);
        if (!control) return;
        if (event.relatedTarget && control.contains(event.relatedTarget)) return;
        const state = getMotionState(control);
        state.hovered = true;
        if (state.pressed) return;
        state.rebounding = false;
        animateHover(control);
    }, true);

    document.addEventListener("pointerout", (event) => {
        if (event.pointerType && event.pointerType !== "mouse") return;
        const control = event.target.closest?.(motionSelector);
        if (!control) return;
        if (event.relatedTarget && control.contains(event.relatedTarget)) return;
        const state = getMotionState(control);
        state.hovered = false;
        if (state.pressed) return;
        state.rebounding = false;
        animateRest(control);
    }, true);

    document.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || !event.isPrimary) return;
        const control = event.target.closest?.(motionSelector);
        if (!control || !canAnimateControl(control)) return;
        const state = getMotionState(control);
        state.hovered = event.pointerType === "mouse"
            ? Boolean(state.hovered || control.matches(":hover"))
            : false;
        state.pressed = true;
        state.rebounding = false;
        pressedControl = control;
        pressedPointerId = event.pointerId;
        const pose = targetPose(control, state);
        animatePose(
            control,
            state,
            [
                { offset: 0 },
                {
                    offset: 1,
                    translate: `0 ${pose.y}px`,
                    scale: String(pose.scale),
                },
            ],
            {
                duration: 70,
                easing: "cubic-bezier(.25, .62, .3, 1)",
            },
            pose
        );
    }, true);

    document.addEventListener("pointerup", (event) => {
        if (
            pressedPointerId !== null
            && event.pointerId !== pressedPointerId
        ) {
            return;
        }
        const control = pressedControl;
        pressedControl = null;
        pressedPointerId = null;
        if (control) releaseControl(control);
    }, true);

    document.addEventListener("pointercancel", (event) => {
        if (
            pressedPointerId !== null
            && event.pointerId !== pressedPointerId
        ) {
            return;
        }
        const control = pressedControl;
        pressedControl = null;
        pressedPointerId = null;
        if (!control) return;
        const state = getMotionState(control);
        state.pressed = false;
        state.hovered = false;
        animateRest(control);
    }, true);

    reducedMotion.addEventListener?.("change", () => {
        document.querySelectorAll(motionSelector).forEach(clearMotion);
    });

    // 键盘触发的 click 没有 pointerdown/up，补一次完整但更短的按压。
    document.addEventListener("click", (event) => {
        if (event.detail !== 0) return;
        const control = event.target.closest?.(motionSelector);
        if (!control || !canAnimateControl(control)) return;
        const state = getMotionState(control);
        state.pressed = true;
        state.hovered = false;
        const pose = targetPose(control, state);
        animatePose(
            control,
            state,
            [
                { offset: 0 },
                {
                    offset: 1,
                    translate: `0 ${pose.y}px`,
                    scale: String(pose.scale),
                },
            ],
            {
                duration: 60,
                easing: "cubic-bezier(.25, .62, .3, 1)",
            },
            pose,
            () => {
                window.setTimeout(() => releaseControl(control), 45);
            }
        );
    }, true);

    document.addEventListener("keydown", (event) => {
        const target = event.target;
        if (!(target instanceof HTMLTextAreaElement)) return;
        if (!target.closest(".composer-input")) return;
        if (event.key !== "Enter") return;

        // 输入法组合状态中的回车只用于确认候选词。
        if (event.isComposing || event.keyCode === 229) {
            event.stopImmediatePropagation();
            return;
        }

        // Shift+Enter 明确插入换行。不能只依赖浏览器默认行为，因为不同输入法/
        // WebView 下 Gradio 仍可能把组合后的回车识别为提交。
        if (event.shiftKey) {
            event.preventDefault();
            event.stopImmediatePropagation();
            const start = target.selectionStart ?? target.value.length;
            const end = target.selectionEnd ?? start;
            target.setRangeText("\n", start, end, "end");
            const inputEvent = typeof InputEvent === "function"
                ? new InputEvent("input", {
                    bubbles: true,
                    inputType: "insertLineBreak",
                    data: "\n",
                })
                : new Event("input", { bubbles: true });
            target.dispatchEvent(inputEvent);
            return;
        }

        event.preventDefault();
        event.stopImmediatePropagation();
        if (event.repeat || !target.value.trim()) return;

        const sendButton = document.querySelector(".composer .send-btn");
        if (sendButton && !sendButton.disabled) sendButton.click();
    }, true);

    // 发送后只跟随新出现的用户气泡；AI 流式更新不再反复抢占滚动位置。
    const chatScrollKey = "__briefMeChatAutoScrollInstalled";
    if (!window[chatScrollKey]) {
        window[chatScrollKey] = true;
        let pendingUserBaseline = null;
        let pendingUntil = 0;

        const userMessages = () =>
            Array.from(document.querySelectorAll(".chatbot .message.user"));

        const findChatScroller = () => {
            const chatbot = document.querySelector(".chatbot");
            if (!chatbot) return null;
            const bubbleWrap = chatbot.querySelector(".bubble-wrap");
            if (bubbleWrap) return bubbleWrap;
            return [chatbot, ...chatbot.querySelectorAll("*")].find((element) => {
                const style = getComputedStyle(element);
                return /(auto|scroll)/.test(style.overflowY)
                    && element.scrollHeight > element.clientHeight;
            }) || chatbot;
        };

        const scrollToLatestUserMessage = () => {
            const messages = userMessages();
            const scroller = findChatScroller();
            if (!messages.length || !scroller) return false;

            const latest = messages[messages.length - 1];
            const scrollerRect = scroller.getBoundingClientRect();
            const messageRect = latest.getBoundingClientRect();
            const targetTop = Math.max(
                0,
                scroller.scrollTop + messageRect.bottom - scrollerRect.bottom + 18
            );
            const reduceMotion = window.matchMedia(
                "(prefers-reduced-motion: reduce)"
            ).matches;

            if (typeof scroller.scrollTo === "function") {
                scroller.scrollTo({
                    top: targetTop,
                    behavior: reduceMotion ? "auto" : "smooth",
                });
            } else {
                scroller.scrollTop = targetTop;
            }
            return true;
        };

        const flushPendingChatScroll = () => {
            if (pendingUserBaseline === null) return;
            if (userMessages().length > pendingUserBaseline) {
                requestAnimationFrame(() => {
                    requestAnimationFrame(() => {
                        if (scrollToLatestUserMessage()) {
                            pendingUserBaseline = null;
                        }
                    });
                });
            } else if (Date.now() >= pendingUntil) {
                pendingUserBaseline = null;
            }
        };

        const beginPendingChatScroll = () => {
            pendingUserBaseline = userMessages().length;
            pendingUntil = Date.now() + 1800;
            [0, 80, 220, 500, 1000].forEach((delay) => {
                window.setTimeout(flushPendingChatScroll, delay);
            });
        };

        document.addEventListener("click", (event) => {
            const sendButton = event.target.closest?.(".composer .send-btn");
            if (sendButton && !sendButton.disabled) {
                beginPendingChatScroll();
            }
        }, true);

        const chatObserver = new MutationObserver(flushPendingChatScroll);
        chatObserver.observe(document.body, { childList: true, subtree: true });
    }
})();
"""


# =====================================================================
# UI 组装
# =====================================================================
def build_ui() -> gr.Blocks:
    q = _quick_prompts()
    preferred_download_dir = _load_download_dir_preference()

    def _update_businesses(project):
        first_business = next(iter(q[project]))
        first_command = next(iter(q[project][first_business]))
        return (
            gr.update(choices=list(q[project].keys()), value=first_business),
            gr.update(choices=list(q[project][first_business].keys()), value=first_command),
            q[project][first_business][first_command],
        )

    def _update_commands(project, business):
        first_command = next(iter(q[project][business]))
        return (
            gr.update(choices=list(q[project][business].keys()), value=first_command),
            q[project][business][first_command],
        )

    def _select_command(project, business, command):
        return q[project][business][command]

    with gr.Blocks(
        title="BriefMe · 多场景数据统计助手",
        fill_height=True,
    ) as demo:

        # ------- 顶部应用栏 -------
        with gr.Row(elem_classes="app-bar"):
            with gr.Column(scale=5, min_width=260, elem_classes="brand-col"):
                gr.HTML(BRAND_HTML, elem_classes="bare")
            with gr.Column(scale=5, min_width=380, elem_classes="status-col"):
                with gr.Row(elem_classes="status-row"):
                    status_display = gr.HTML(
                        _status_html(), elem_classes="bare status-html"
                    )
                    refresh_btn = gr.Button(
                        "刷新状态", size="sm", elem_classes="ghost-btn refresh-btn"
                    )

        # ------- 左右分栏 -------
        with gr.Row(elem_classes="workspace"):
            # --- 左侧：指令台（标题 / 选择 / 预览 / 填入按钮同处一张卡片）---
            with gr.Column(scale=1, min_width=320, elem_classes="side-col"):
                with gr.Group(elem_classes="card"):
                    gr.HTML(
                        '<div class="card-head">'
                        '<div class="card-title">快捷指令</div>'
                        "</div>",
                        elem_classes="bare",
                    )
                    with gr.Column(elem_classes="card-body"):
                        with gr.Column(elem_classes="card-section"):
                            gr.HTML(
                                '<div class="section-label">项目</div>',
                                elem_classes="bare",
                            )
                            project_select = gr.Radio(
                                choices=list(q.keys()),
                                value="永锋钢铁",
                                show_label=False,
                                container=False,
                                elem_classes="project-seg",
                            )
                        with gr.Column(elem_classes="card-section"):
                            gr.HTML(
                                '<div class="section-label">业务</div>',
                                elem_classes="bare",
                            )
                            business_select = gr.Dropdown(
                                choices=list(q["永锋钢铁"].keys()),
                                value="打包带钢卷",
                                show_label=False,
                                container=False,
                                filterable=False,
                                allow_custom_value=False,
                                elem_classes="task-dropdown business-dropdown",
                            )
                        with gr.Column(elem_classes="card-section"):
                            gr.HTML(
                                '<div class="section-label">指令</div>',
                                elem_classes="bare",
                            )
                            command_select = gr.Dropdown(
                                choices=list(
                                    q["永锋钢铁"]["打包带钢卷"].keys()
                                ),
                                value="昨日打包带情况",
                                show_label=False,
                                container=False,
                                filterable=False,
                                allow_custom_value=False,
                                elem_classes="task-dropdown command-dropdown",
                            )
                        with gr.Column(elem_classes="card-section"):
                            gr.HTML(
                                '<div class="section-label">待发送内容</div>',
                                elem_classes="bare",
                            )
                            command_preview = gr.Textbox(
                                value=q["永锋钢铁"]["打包带钢卷"]["昨日打包带情况"],
                                show_label=False,
                                interactive=False,
                                lines=3,
                                container=False,
                                elem_classes="preview-box",
                            )
                            fill_btn = gr.Button(
                                "填入输入框",
                                variant="primary",
                                size="sm",
                                elem_classes="fill-btn",
                            )

                # 从属卡片 1：文件下载路径（常展开）
                with gr.Group(elem_classes="card card-flat setting-card"):
                    gr.HTML(
                        '<div class="card-head">'
                        '<div class="card-title">文件下载路径</div>'
                        "</div>",
                        elem_classes="bare",
                    )
                    with gr.Column(elem_classes="card-body"):
                        download_dir = gr.Textbox(
                            show_label=False,
                            container=True,
                            value=preferred_download_dir,
                            placeholder=DEFAULT_DOWNLOAD_DIR,
                            lines=1,
                            elem_classes="path-input",
                        )
                        with gr.Row(elem_classes="path-actions"):
                            open_folder_btn = gr.Button(
                                "打开文件夹",
                                variant="secondary",
                                size="sm",
                                elem_classes="path-action-btn",
                            )
                            choose_path_btn = gr.Button(
                                "选择路径",
                                variant="primary",
                                size="sm",
                                elem_classes="path-action-btn",
                            )

                # 从属卡片 2：文件上传（常展开，重点强调投放区）
                with gr.Group(elem_classes="card card-flat setting-card upload-card"):
                    gr.HTML(
                        '<div class="card-head">'
                        '<div class="card-title">文件上传</div>'
                        '<div class="card-note">永锋人工检判表</div>'
                        "</div>",
                        elem_classes="bare",
                    )
                    with gr.Column(elem_classes="card-body"):
                        manual_file = gr.File(
                            label="永锋人工检判表",
                            file_types=[".xlsx"],
                            type="filepath",
                            show_label=False,
                            elem_classes="upload-zone",
                        )

            # --- 右侧：对话 + 产物停靠 ---
            with gr.Column(scale=4, min_width=480, elem_classes="main-col"):
                with gr.Group(elem_classes="card chat-card"):
                    with gr.Row(elem_classes="card-head"):
                        gr.HTML(
                            '<div class="card-title">对话</div>',
                            elem_classes="bare head-left",
                        )
                        clear_btn = gr.Button(
                            "清空对话",
                            size="sm",
                            variant="secondary",
                            elem_classes="ghost-btn head-btn",
                        )
                    chatbot = gr.Chatbot(
                        height=520,
                        show_label=False,
                        render_markdown=True,
                        elem_classes="chatbot",
                        avatar_images=(None, None),
                        container=False,
                        layout="bubble",
                        placeholder=(
                            '<div class="chat-empty">'
                            '<div class="chat-empty-title">你好，我是 BriefMe</div>'
                            '<div class="chat-empty-sub">'
                            "中冶赛迪多场景数据统计助手。左侧挑一条快捷指令，"
                            "或在下方直接输入问题。"
                            "</div>"
                            '<div class="chat-empty-eg">'
                            "试试：发 2026-04-15 的废钢检判情况"
                            "</div>"
                            "</div>"
                        ),
                    )
                    conversation_stop_divider = gr.HTML(
                        '<div class="chat-stop-divider">'
                        "<span>对话已终止</span>"
                        "</div>",
                        elem_classes="chat-stop-divider-wrap",
                    )
                    with gr.Row(elem_classes="composer"):
                        msg = gr.Textbox(
                            placeholder="输入消息，如：发 2026-04-15 的废钢检判情况 ...",
                            show_label=False,
                            scale=9,
                            container=False,
                            autofocus=True,
                            lines=3,
                            elem_classes="composer-input",
                        )
                        with gr.Column(
                            scale=1,
                            min_width=104,
                            elem_classes="send-actions",
                        ):
                            stop_btn = gr.Button(
                                "终止对话",
                                size="sm",
                                variant="secondary",
                                elem_classes="stop-btn",
                            )
                            submit_btn = gr.Button(
                                "发送",
                                variant="primary",
                                elem_classes="send-btn",
                            )

                initial_runs = load_artifact_history()
                initial_run = initial_runs[0] if initial_runs else _legacy_run_from_scan()
                initial_session = {
                    "vpn_state": "unknown",
                    "messages": [],
                    "current_run": initial_run,
                    "recent_runs": initial_runs,
                }
                (
                    artifact_summary_init,
                    product_history_init,
                    image_summary_init,
                    gallery_init,
                    package_history_init,
                    log_summary_init,
                    log_history_init,
                ) = _output_view(initial_session)
                with gr.Group(elem_classes="card dock-card"):
                    gr.HTML(
                        '<div class="card-head">'
                        '<div class="card-title">输出中心</div>'
                        "</div>",
                        elem_classes="bare",
                    )
                    with gr.Tabs(elem_classes="dock"):
                        with gr.Tab("产物"):
                            with gr.Column(elem_classes="output-content-panel"):
                                artifact_summary = gr.HTML(
                                    value=artifact_summary_init,
                                    elem_classes="output-summary-html",
                                )
                                artifact_file_panel = gr.HTML(
                                    value=product_history_init,
                                    elem_classes="output-artifact-files",
                                    container=False,
                                )
                                open_output_dir_btn = gr.Button(
                                    "打开所在文件夹",
                                    size="sm",
                                    variant="secondary",
                                    elem_classes="output-action-btn",
                                )
                        with gr.Tab("图片"):
                            with gr.Column(
                                elem_classes="output-content-panel output-image-panel"
                            ):
                                image_summary = gr.HTML(
                                    value=image_summary_init,
                                    elem_classes="output-summary-html",
                                )
                                with gr.Row(
                                    elem_classes="output-image-content-row"
                                ):
                                    with gr.Column(
                                        scale=2,
                                        min_width=0,
                                        elem_classes="output-gallery-column",
                                    ):
                                        gr.HTML(
                                            '<div class="artifact-panel-head">'
                                            "<strong>最近 20 张错判渲染图</strong>"
                                            "</div>",
                                            elem_classes="artifact-panel-head-html",
                                        )
                                        gallery = gr.Gallery(
                                            value=gallery_init,
                                            columns=4,
                                            height=210,
                                            show_label=False,
                                            allow_preview=True,
                                            elem_classes="output-gallery",
                                        )
                                    with gr.Column(
                                        scale=1,
                                        min_width=260,
                                        elem_classes="output-zip-column",
                                    ):
                                        image_zip_panel = gr.HTML(
                                            value=package_history_init,
                                            elem_classes="output-zip-files",
                                            container=False,
                                        )
                                open_image_dir_btn = gr.Button(
                                    "打开图片目录",
                                    size="sm",
                                    variant="secondary",
                                    elem_classes="output-action-btn",
                                )
                        with gr.Tab("日志"):
                            with gr.Column(
                                elem_classes="output-content-panel output-log-panel"
                            ):
                                log_summary = gr.HTML(
                                    value=log_summary_init,
                                    elem_classes="output-summary-html",
                                )
                                log_history_display = gr.HTML(
                                    value=log_history_init,
                                    elem_classes="output-log-history",
                                )
                                log_refresh_btn = gr.Button(
                                    "刷新日志",
                                    size="sm",
                                    elem_classes="ghost-btn refresh-btn log-refresh-btn",
                                )
                                log_timer = gr.Timer(2.0, active=True)
        session = gr.State(initial_session)

# --- 绑定：输入/按钮 ---
        def _sync_append(message, history):
            history = _normalize_chat_history(history)
            message = (message or "").strip()
            if not message:
                return "", history
            return "", history + [{"role": "user", "content": message}]

        def pack_multilabel_handler(message, history, session, save_dir, *, yongfeng: bool = False):
            history = _normalize_chat_history(history)
            dest = (save_dir or "").strip() or _parse_save_path(message)
            dates = (parse_yongfeng_dates if yongfeng else parse_shenglong_dates)(message)
            pack_fn = pack_yongfeng_multilabel if yongfeng else pack_shenglong_multilabel
            site = "永锋" if yongfeng else "盛隆"
            xlsx, pptx, imgs = _scan_latest_artifacts()
            if not dest:
                history.append({
                    "role": "assistant",
                    "content": (
                        "请先在左侧填写「图像保存路径」（须与下载时同一目录），"
                        f"再确认打包{site}废钢多标签分类数据集。"
                    ),
                })
                yield history, session, xlsx, pptx, imgs, None
                return
            try:
                result = pack_fn(dest, dates=dates or None)
            except Exception as exc:
                history.append({"role": "assistant", "content": f"❌ 打包失败: {exc}"})
                yield history, session, xlsx, pptx, imgs, None
                return
            lines = [f"✅ 已按人工筛完后的原图打包「废钢多标签分类数据集」（{site}）"]
            for day in result.get("days") or []:
                if day.get("skipped"):
                    lines.append(f"- {day.get('date')}: 没有可打包的图，已跳过")
                else:
                    lines.append(
                        f"- {day.get('date')}: {day.get('images')} 张 → {day.get('zip')}"
                    )
            if not result.get("zip_files"):
                lines.append("没有生成任何压缩包。请确认目录里还有车次图片。")
            history.append({"role": "assistant", "content": "\n".join(lines)})
            yield history, session, xlsx, pptx, imgs, None

        def download_image_handler(message, history, session, save_dir, *, yongfeng: bool):
            history = _normalize_chat_history(history)
            dest = (save_dir or "").strip() or _parse_save_path(message)
            dates = (parse_yongfeng_dates if yongfeng else parse_shenglong_dates)(message)
            iter_fn = iter_yongfeng_images if yongfeng else iter_shenglong_images
            site = "永锋" if yongfeng else "盛隆"
            source = "srape-steel" if yongfeng else "3000"
            example = "永锋图像" if yongfeng else "盛隆图像"
            xlsx, pptx, imgs = _scan_latest_artifacts()
            if not dest:
                history.append({
                    "role": "assistant",
                    "content": (
                        "请先在左侧填写「图像保存路径」，例如 "
                        f"`/Users/你的用户名/Desktop/{example}`，再发送下载指令。"
                        "图会立刻写到这个目录，中断后重新跑同一日期即可续传。"
                    ),
                })
                yield history, session, xlsx, pptx, imgs, None
                return
            if not dates:
                history.append({
                    "role": "assistant",
                    "content": (
                        f"请写明日期。单日：下载 2026-09-01 的【{site}】检判原图；"
                        f"多日：下载 2026-08-31、2026-09-01 的【{site}】检判原图；"
                        f"连续区间：下载 2026-08-31 到 2026-09-01 的【{site}】检判原图。"
                    ),
                })
                yield history, session, xlsx, pptx, imgs, None
                return

            date_label = "、".join(dates)
            note = ""
            if not yongfeng and "MINIO图像下载" in message:
                note = "已改为走 3000 业务系统，不再扫描 MinIO 桶。\n"
            history.append({
                "role": "assistant",
                "content": (
                    f"{note}📥 开始从 {source} 下载 {date_label} 的【{site}】智能判级原图\n"
                    f"保存目录：{dest}\n"
                    "每下一车就会写到磁盘，可在该目录看进度。"
                ),
            })
            yield history, session, xlsx, pptx, imgs, None

            lines = [history[-1]["content"]]
            try:
                for event in iter_fn(output_dir=dest, dates=dates):
                    if event.get("type") == "done":
                        result = event.get("result") or {}
                        zips = result.get("zip_files") or []
                        zip_text = "\n".join(f"- {p}" for p in zips) if zips else "- 无"
                        scp_text = result.get("scp_report") or ""
                        if yongfeng:
                            pack_note = (
                                "已自动打好「废钢分类数据集」，以及按主料型的实例分割包和边缘分割包。\n"
                                "永锋不再另打「废钢多标签分类数据集」。"
                            )
                        else:
                            pack_note = (
                                "「废钢多标签分类数据集」还没打。"
                                "请按日期、按车次打开文件夹删掉不合格图，"
                                "再点指令「确认打包多标签」或在对话里说确认打包。"
                            )
                        pack_label = (
                            "废钢分类数据集和按料型的实例/边缘分割包"
                            if yongfeng
                            else "实例/边缘分割包"
                        )
                        lines.append(
                            f"✅ 完成：新下载 {result.get('success', 0)} 张，"
                            f"跳过 {result.get('skipped_existing', 0)} 张，"
                            f"失败 {result.get('failed', 0)} 张\n"
                            f"进度日志：{result.get('output_dir')}/download_progress.log\n"
                            f"{pack_label}：\n{zip_text}\n"
                            f"{scp_text}\n"
                            f"{pack_note}"
                        )
                    else:
                        msg_line = event.get("message") or ""
                        if msg_line:
                            lines.append(msg_line)
                    history[-1]["content"] = "\n".join(lines[-50:])
                    xlsx, pptx, imgs = _scan_latest_artifacts()
                    yield history, session, xlsx, pptx, imgs, None
            except Exception as exc:
                lines.append(f"❌ 下载中断：{exc}\n已写入的文件仍在 {dest}，重跑同一日期会跳过已有文件。")
                history[-1]["content"] = "\n".join(lines[-50:])
                xlsx, pptx, imgs = _scan_latest_artifacts()
                yield history, session, xlsx, pptx, imgs, None

        def upload_yongfeng_handler(message, history, session, save_dir):
            """永锋 datasets/ zip 上传到金睛平台（单独指令，逐日流式汇报）。"""
            history = _normalize_chat_history(history)
            dest = (save_dir or "").strip() or _parse_save_path(message)
            dates = parse_yongfeng_dates(message)
            xlsx, pptx, imgs = _scan_latest_artifacts()
            if not dest:
                history.append({
                    "role": "assistant",
                    "content": (
                        "请先在左侧填写「图像保存路径」（与下载时同一个目录），"
                        "再发送上传指令。程序会上传该目录下 <日期>/datasets/ 里的 zip。"
                    ),
                })
                yield history, session, xlsx, pptx, imgs, None
                return
            if not dates:
                history.append({
                    "role": "assistant",
                    "content": (
                        "请写明要上传的日期，例如：\n"
                        "上传 2026-09-01 的【永锋】数据集；\n"
                        "上传 2026-08-31、2026-09-01 的【永锋】数据集。"
                    ),
                })
                yield history, session, xlsx, pptx, imgs, None
                return

            date_label = "、".join(dates)
            history.append({
                "role": "assistant",
                "content": (
                    f"📤 开始上传 {date_label} 的【永锋】数据集 zip 到金睛平台\n"
                    f"（pre-athena-mv.cisdigital.cn，无标注模式导入）\n"
                    f"目录：{dest}\n"
                    "每个 zip 上传后会自动建导入任务并等待结果。"
                ),
            })
            yield history, session, xlsx, pptx, imgs, None

            # 编排仍一次传入全部日期（跨日入口锁 + 统一重试）；
            # 聊天版式与旧版一致：开场白 + 逐日【日期】块 + 全部日期处理完成。
            lines = [history[-1]["content"]]
            try:
                for event in iter_upload_zips_for_dates(dest, dates=dates):
                    if event.get("type") == "day":
                        day_report = event.get("day_report") or {}
                        day = day_report.get("day") or ""
                        lines.append(f"\n【{day}】")
                        lines.extend(day_report.get("lines") or [])
                        history[-1]["content"] = "\n".join(lines[-80:])
                        xlsx, pptx, imgs = _scan_latest_artifacts()
                        yield history, session, xlsx, pptx, imgs, None
                    elif event.get("type") == "done":
                        result = event.get("result") or {}
                        # 用最终态（含统一重试结果）重写逐日块，避免留下首轮 ⏳
                        lines = [lines[0]]
                        for day in result.get("days") or []:
                            lines.append(f"\n【{day.get('day')}】")
                            lines.extend(day.get("lines") or [])
                        lines.append(
                            f"\n全部日期处理完成：成功 {int(result.get('uploaded') or 0)} 个，"
                            f"跳过 {int(result.get('skipped') or 0)} 个，"
                            f"失败 {int(result.get('failed') or 0)} 个。"
                        )
                        history[-1]["content"] = "\n".join(lines[-80:])
                        xlsx, pptx, imgs = _scan_latest_artifacts()
                        yield history, session, xlsx, pptx, imgs, None
            except Exception as exc:
                lines.append(f"❌ 上传中断：{exc}")
                history[-1]["content"] = "\n".join(lines[-80:])
                xlsx, pptx, imgs = _scan_latest_artifacts()
                yield history, session, xlsx, pptx, imgs, None

        # ========== 其他功能处理器（普通）==========
        def align_manual_handler(message, history, session, manual_upload):
            history = _normalize_chat_history(history)
            path = resolve_manual_path(message, manual_upload)
            example_manual_path = Path.home() / "Downloads" / "废钢人工判定.xlsx"
            xlsx, pptx, imgs = _scan_latest_artifacts()
            if not path or not Path(path).is_file():
                history.append({
                    "role": "assistant",
                    "content": (
                        "请先在左侧「永锋人工检判表」选择 xlsx，"
                        "或在消息里写上文件绝对路径。\n"
                        "例如：对齐【永锋】废钢视觉质检实绩和人工检判结果 "
                        f"`{example_manual_path}`"
                    ),
                })
                yield history, session, xlsx, pptx, imgs, None
                return
            history.append({
                "role": "assistant",
                "content": (
                    "开始对齐永锋废钢视觉质检实绩和人工检判表。\n"
                    f"人工表：{path}\n"
                    "只取同一天、已经质检完成的车次。对不上的记为未检判。"
                ),
            })
            yield history, session, xlsx, pptx, imgs, None
            lines = [history[-1]["content"]]
            try:
                for event in iter_align_manual(path):
                    if event.get("type") == "status":
                        lines.append(str(event.get("message") or ""))
                    elif event.get("type") == "done":
                        lines.append(format_align_summary(event.get("result") or {}))
                    history[-1]["content"] = "\n".join(lines[-60:])
                    xlsx, pptx, imgs = _scan_latest_artifacts()
                    yield history, session, xlsx, pptx, imgs, None
            except Exception as exc:
                lines.append(f"对齐失败：{exc}\n请确认永锋 VPN 已连接，并且人工表能打开。")
                history[-1]["content"] = "\n".join(lines[-60:])
                xlsx, pptx, imgs = _scan_latest_artifacts()
                yield history, session, xlsx, pptx, imgs, None

        def normal_handler(user_message, history, session, save_dir):
            history = _normalize_chat_history(history)
            xlsx, pptx, imgs = _scan_latest_artifacts()
            if not user_message:
                yield history, session, xlsx, pptx, imgs, None
                return

            agent._default_shenglong_output_dir = (save_dir or "").strip()
            agent._default_yongfeng_output_dir = (save_dir or "").strip()

            working_session = copy.deepcopy(
                session or {"vpn_state": "unknown", "messages": []}
            )
            status_queue: queue.Queue = queue.Queue()
            result: Dict[str, object] = {}
            started_at = time.monotonic()

            def emit_status(message):
                if message:
                    status_queue.put(str(message))

            def run_agent():
                try:
                    result["value"] = agent.chat(
                        user_message,
                        working_session,
                        on_status=emit_status,
                    )
                except Exception as exc:
                    result["error"] = exc
                finally:
                    status_queue.put(None)

            steps: List[str] = []
            history.append(
                {
                    "role": "assistant",
                    "content": _render_live_status(steps),
                }
            )
            yield history, copy.deepcopy(working_session), xlsx, pptx, imgs, None

            worker = threading.Thread(target=run_agent, daemon=True)
            worker.start()

            while True:
                try:
                    message = status_queue.get(timeout=1.0)
                except queue.Empty:
                    history[-1]["content"] = _render_live_status(
                        steps, time.monotonic() - started_at
                    )
                    yield history, copy.deepcopy(working_session), xlsx, pptx, imgs, None
                    continue

                if message is None:
                    break
                if not steps or steps[-1] != message:
                    steps.append(message)
                history[-1]["content"] = _render_live_status(
                    steps, time.monotonic() - started_at
                )
                yield history, copy.deepcopy(working_session), xlsx, pptx, imgs, None

            worker.join(timeout=0.2)
            history.pop()

            if "error" in result:
                reply = f"处理失败：{result['error']}"
                final_session = working_session
            else:
                reply, final_session = result.get("value") or (
                    "处理完成，但没有返回内容。",
                    working_session,
                )

            history.append({"role": "assistant", "content": str(reply)})
            xlsx, pptx, imgs = _scan_latest_artifacts()
            yield history, final_session, xlsx, pptx, imgs, None

        # ========== 路由处理器 ==========
        def _dispatch_route(_pending_message, history, session, save_dir, manual_upload):
            _save_download_dir_preference(save_dir)
            user_message = ""
            if history and len(history) > 0:
                last_item = history[-1]
                if isinstance(last_item, dict):
                    content = last_item.get("content", "")
                    if isinstance(content, list):
                        text_parts = [str(item.get("text", "")) for item in content if isinstance(item, dict) and "text" in item]
                        user_message = "".join(text_parts).strip()
                    else:
                        user_message = str(content).strip()

            agent._default_shenglong_output_dir = (save_dir or "").strip()
            agent._default_yongfeng_output_dir = (save_dir or "").strip()
            agent._default_yongfeng_manual_file = uploaded_to_path(manual_upload)
            if is_yongfeng_manual_align(user_message):
                for result in align_manual_handler(user_message, history, session, manual_upload):
                    yield result
            elif is_dual_plant_image_or_pack_request(user_message):
                xlsx, pptx, imgs = _scan_latest_artifacts()
                history = _normalize_chat_history(history)
                history.append({
                    "role": "assistant",
                    "content": (
                        "这句话里同时出现了永锋和盛隆。"
                        "请分开发送：下载 【永锋】检判原图，或下载 【盛隆】检判原图。"
                    ),
                })
                yield history, session, xlsx, pptx, imgs, None
            elif is_ambiguous_multilabel_pack(user_message):
                xlsx, pptx, imgs = _scan_latest_artifacts()
                history = _normalize_chat_history(history)
                history.append({
                    "role": "assistant",
                    "content": (
                        "确认打包多标签只用于盛隆。"
                        "请说：确认打包【盛隆】废钢多标签分类数据集。"
                        "永锋下载结束时已经自动打好废钢分类数据集，以及按料型的实例/边缘分割包。"
                    ),
                })
                yield history, session, xlsx, pptx, imgs, None
            elif is_yongfeng_multilabel_pack(user_message):
                xlsx, pptx, imgs = _scan_latest_artifacts()
                history = _normalize_chat_history(history)
                history.append({
                    "role": "assistant",
                    "content": (
                        "永锋不再在人工筛图后另打「废钢多标签分类数据集」。"
                        "下载结束时会自动生成「废钢分类数据集」，"
                        "以及每种主料型的实例分割包和边缘分割包。"
                    ),
                })
                yield history, session, xlsx, pptx, imgs, None
            elif is_shenglong_multilabel_pack(user_message):
                for result in pack_multilabel_handler(
                    user_message, history, session, save_dir, yongfeng=False
                ):
                    yield result
            elif is_yongfeng_dataset_upload(user_message):
                for result in upload_yongfeng_handler(
                    user_message, history, session, save_dir
                ):
                    yield result
            elif is_yongfeng_image_download(user_message):
                for result in download_image_handler(
                    user_message, history, session, save_dir, yongfeng=True
                ):
                    yield result
            elif is_shenglong_image_download(user_message):
                for result in download_image_handler(
                    user_message, history, session, save_dir, yongfeng=False
                ):
                    yield result
            else:
                for result in normal_handler(
                    user_message, history, session, save_dir
                ):
                    yield result

        def route_handler(
            pending_message,
            history,
            session,
            save_dir,
            manual_upload,
            active_project,
            active_business,
            active_command,
        ):
            _save_download_dir_preference(save_dir)
            if (save_dir or "").strip():
                allowed_dir = str(Path(save_dir).expanduser().resolve())
                if allowed_dir not in demo.allowed_paths:
                    demo.allowed_paths.append(allowed_dir)
            user_message = ""
            if history:
                last_item = history[-1]
                if isinstance(last_item, dict):
                    content = last_item.get("content", "")
                    if isinstance(content, list):
                        user_message = "".join(
                            str(item.get("text", ""))
                            for item in content
                            if isinstance(item, dict) and "text" in item
                        ).strip()
                    else:
                        user_message = str(content).strip()

            current_session = copy.deepcopy(
                session or {"vpn_state": "unknown", "messages": []}
            )
            run = new_run(
                str(active_project or ""),
                str(active_business or ""),
                str(active_command or user_message or ""),
                save_dir,
            )
            append_step(run, "已接收任务，开始处理")
            sync_current_run(current_session, run)
            # 先持久化任务本身，即使随后取消或进程退出，也不会从历史中消失。
            commit_run(current_session, run)
            last_history = history
            last_session = current_session
            status = "success"

            try:
                for result in _dispatch_route(
                    pending_message,
                    history,
                    current_session,
                    save_dir,
                    manual_upload,
                ):
                    out_history, out_session = result[0], result[1]
                    if isinstance(out_session, dict):
                        current_session = out_session
                    run_data = current_session.get("current_run")
                    if isinstance(run_data, dict):
                        run = run_from_dict(run_data)
                    else:
                        sync_current_run(current_session, run)
                    for directory in run.output_dirs:
                        log_candidate = Path(directory) / "download_progress.log"
                        if log_candidate.is_file():
                            run.log_path = str(log_candidate)
                            break
                    sync_current_run(current_session, run)
                    last_history = out_history
                    last_session = current_session
                    yield (
                        out_history,
                        current_session,
                        *_output_view(current_session),
                    )
            except GeneratorExit:
                run = run_from_dict(
                    last_session.get("current_run") or run.to_dict()
                )
                for directory in run.output_dirs:
                    scan_new_artifacts(run, directory)
                finalize_run(run, "cancelled")
                commit_run(last_session, run)
                raise
            except Exception as exc:
                status = "failed"
                run = run_from_dict(
                    last_session.get("current_run") or run.to_dict()
                )
                for directory in run.output_dirs:
                    scan_new_artifacts(run, directory)
                finalize_run(run, status)
                commit_run(last_session, run)
                error_history = _normalize_chat_history(last_history)
                error_history.append(
                    {"role": "assistant", "content": f"处理失败：{exc}"}
                )
                yield (
                    error_history,
                    last_session,
                    *_output_view(last_session),
                )
            else:
                run = run_from_dict(
                    last_session.get("current_run") or run.to_dict()
                )
                for directory in run.output_dirs:
                    scan_new_artifacts(run, directory)
                finalize_run(run, status)
                commit_run(last_session, run)
                yield (
                    last_history,
                    last_session,
                    *_output_view(last_session),
                )

        # ------- 事件绑定 -------
        def _fill_message(text):
            return text

        def _wire_submit(trigger):
            return trigger(
                _sync_append,
                inputs=[msg, chatbot],
                outputs=[msg, chatbot],
                queue=False,
            ).then(
                route_handler,
                inputs=[
                    msg,
                    chatbot,
                    session,
                    download_dir,
                    manual_file,
                    project_select,
                    business_select,
                    command_select,
                ],
                outputs=[
                    chatbot,
                    session,
                    artifact_summary,
                    artifact_file_panel,
                    image_summary,
                    gallery,
                    image_zip_panel,
                    log_summary,
                    log_history_display,
                ],
            )

        submit_events = [
            _wire_submit(submit_btn.click),
            _wire_submit(msg.submit),
        ]
        stop_btn.click(
            None,
            None,
            None,
            cancels=submit_events,
            queue=False,
            api_visibility="private",
        )

        demo.load(
            first_run_path_notice,
            inputs=[chatbot],
            outputs=[chatbot],
        )

        clear_btn.click(
            clear_chat,
            inputs=[session],
            outputs=[msg, chatbot, session],
        )

        refresh_btn.click(_status_html, outputs=status_display)
        open_folder_btn.click(
            _open_download_dir,
            inputs=[download_dir],
            outputs=None,
            queue=False,
        )
        choose_path_btn.click(
            _choose_download_dir,
            inputs=[download_dir],
            outputs=[download_dir],
            queue=False,
            show_progress="hidden",
        )
        download_dir.change(
            _save_download_dir_preference,
            inputs=[download_dir],
            outputs=None,
            queue=False,
            show_progress="hidden",
        )
        download_dir.blur(
            _save_download_dir_preference,
            inputs=[download_dir],
            outputs=None,
            queue=False,
            show_progress="hidden",
        )
        open_output_dir_btn.click(
            _open_run_folder,
            inputs=[session],
            outputs=None,
            queue=False,
        )
        open_image_dir_btn.click(
            _open_run_folder,
            inputs=[session],
            outputs=None,
            queue=False,
        )
        log_refresh_btn.click(
            lambda current: (
                _log_summary_html(_active_run(current)),
                _download_history_html(current),
            ),
            inputs=[session],
            outputs=[log_summary, log_history_display],
            queue=False,
            js=REFRESH_LOG_JS,
        )
        log_timer.tick(
            lambda current: (
                _log_summary_html(_active_run(current)),
                _download_history_html(current),
            ),
            inputs=[session],
            outputs=[log_summary, log_history_display],
            queue=False,
        )

        project_select.change(
            _update_businesses,
            inputs=project_select,
            outputs=[business_select, command_select, command_preview],
            queue=False,
        )
        business_select.change(
            _update_commands,
            inputs=[project_select, business_select],
            outputs=[command_select, command_preview],
            queue=False,
        )
        command_select.change(
            _select_command,
            inputs=[project_select, business_select, command_select],
            outputs=command_preview,
            queue=False,
        )
        fill_btn.click(_fill_message, inputs=command_preview, outputs=msg, queue=False)

    def _open_file_endpoint(token: str):
        path = _resolve_file_selection_target(token)
        if path is None:
            return JSONResponse(
                {"ok": False, "error": "文件不存在或记录已失效"},
                status_code=404,
            )
        try:
            _reveal_file_in_folder(path)
        except OSError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=500,
            )
        return JSONResponse({"ok": True})

    if not any(
        getattr(route, "path", None) == "/briefme/open-file"
        for route in demo.app.routes
    ):
        demo.app.add_api_route(
            "/briefme/open-file",
            _open_file_endpoint,
            methods=["GET"],
            name="briefme_open_file",
        )
    _remember_download_dir(DOWNLOADS_ROOT)
    for directory in sorted(_DOWNLOAD_ALLOWED_DIRS):
        if directory not in demo.allowed_paths:
            demo.allowed_paths.append(directory)
    return demo


# 只监听本机，并禁止 Gradio 公网分享。分享会下载并运行 frpc。
GRADIO_SERVER_NAME = "127.0.0.1"
GRADIO_SHARE = False


if __name__ == "__main__":
    demo = build_ui()
    demo.launch(
        server_name=GRADIO_SERVER_NAME,
        server_port=int(os.environ.get("BRIEFME_PORT", "7860")),
        share=GRADIO_SHARE,
        theme=THEME,
        css=CUSTOM_CSS,
        js=CHAT_KEYBOARD_JS,
        allowed_paths=list(demo.allowed_paths),
        _app=demo.app,
    )
