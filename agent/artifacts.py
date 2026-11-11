"""任务产物登记与最近运行历史。

输出中心以这里维护的 RunRecord 为唯一数据源。真实文件仍保存在任务输出目录，
这里只保存元数据和绝对路径。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
FILE_KINDS = {
    ".xlsx": "xlsx",
    ".xls": "xlsx",
    ".pptx": "pptx",
    ".ppt": "pptx",
    ".zip": "zip",
}
_history_override = os.getenv("BRIEFME_ARTIFACT_HISTORY_PATH")
ARTIFACT_HISTORY_PATH = (
    Path(_history_override).expanduser()
    if _history_override
    else Path.home() / ".briefme" / "artifact_history.json"
)
MAX_RUNS = 30
MAX_ARTIFACTS_PER_RUN = 300


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_path(path: str | Path) -> Optional[str]:
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, TypeError, ValueError):
        return None


@dataclass
class ArtifactRecord:
    artifact_id: str
    kind: str
    path: str
    label: str
    size: int
    created_at: str
    source_tool: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RunRecord:
    run_id: str
    project: str
    business: str
    command: str
    status: str
    started_at: str
    finished_at: Optional[str]
    output_dirs: List[str] = field(default_factory=list)
    log_path: Optional[str] = None
    steps: List[Dict[str, str]] = field(default_factory=list)
    artifacts: List[ArtifactRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["artifacts"] = [artifact.to_dict() for artifact in self.artifacts]
        return data


def run_from_dict(data: Dict[str, Any]) -> RunRecord:
    artifacts = [
        ArtifactRecord(**item)
        for item in data.get("artifacts", [])
        if isinstance(item, dict)
    ]
    return RunRecord(
        run_id=str(data.get("run_id") or ""),
        project=str(data.get("project") or "未指定项目"),
        business=str(data.get("business") or "未指定业务"),
        command=str(data.get("command") or ""),
        status=str(data.get("status") or "running"),
        started_at=str(data.get("started_at") or _now_iso()),
        finished_at=data.get("finished_at"),
        output_dirs=list(data.get("output_dirs") or []),
        log_path=data.get("log_path"),
        steps=list(data.get("steps") or []),
        artifacts=artifacts,
    )


def sync_current_run(session: Dict[str, Any], run: RunRecord) -> None:
    session["current_run"] = run.to_dict()


def new_run(
    project: str,
    business: str,
    command: str,
    save_dir: Optional[str] = None,
) -> RunRecord:
    run = RunRecord(
        run_id=f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",
        project=(project or "").strip() or "未指定项目",
        business=(business or "").strip() or "未指定业务",
        command=(command or "").strip(),
        status="running",
        started_at=_now_iso(),
        finished_at=None,
    )
    if save_dir:
        add_output_dir(run, save_dir)
    return run


def append_step(run: RunRecord, message: str) -> None:
    text = (message or "").strip()
    if not text:
        return
    if run.steps and run.steps[-1].get("message") == text:
        return
    run.steps.append({"time": _now_iso(), "message": text})
    run.steps = run.steps[-500:]


def add_output_dir(run: RunRecord, raw_path: str | Path) -> None:
    path = _safe_path(raw_path)
    if not path:
        return
    if path not in run.output_dirs:
        run.output_dirs.append(path)
    log_path = Path(path) / "download_progress.log"
    if log_path.is_file():
        run.log_path = str(log_path)


def _kind_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in FILE_KINDS:
        return FILE_KINDS[suffix]
    if suffix in IMAGE_EXTS:
        return "image"
    return "other"


def register_artifact(
    run: RunRecord,
    raw_path: str | Path,
    *,
    kind: Optional[str] = None,
    source_tool: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[ArtifactRecord]:
    path_value = _safe_path(raw_path)
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_file():
        return None
    if any(item.path == path_value for item in run.artifacts):
        return next(item for item in run.artifacts if item.path == path_value)
    try:
        stat = path.stat()
        created_at = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(
            timespec="seconds"
        )
        size = stat.st_size
    except OSError:
        return None
    record = ArtifactRecord(
        artifact_id=uuid.uuid4().hex,
        kind=(kind or _kind_for_path(path)).lower(),
        path=path_value,
        label=path.name,
        size=size,
        created_at=created_at,
        source_tool=source_tool,
        metadata=dict(metadata or {}),
    )
    run.artifacts.append(record)
    run.artifacts = run.artifacts[:MAX_ARTIFACTS_PER_RUN]
    return record


def _register_path_list(
    run: RunRecord,
    values: Any,
    *,
    source_tool: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    if isinstance(values, (str, Path)):
        register_artifact(
            run, values, source_tool=source_tool, metadata=metadata
        )
        return
    if isinstance(values, dict):
        for key in ("path", "file", "file_path", "saved_path"):
            if values.get(key):
                register_artifact(
                    run, values[key], source_tool=source_tool, metadata=metadata
                )
        return
    if isinstance(values, Iterable):
        for item in values:
            _register_path_list(
                run, item, source_tool=source_tool, metadata=metadata
            )


def _scan_new_images(run: RunRecord, raw_dir: str | Path) -> None:
    directory = Path(raw_dir).expanduser()
    if not directory.is_dir():
        return
    try:
        started = datetime.fromisoformat(run.started_at).timestamp() - 2
    except (ValueError, TypeError):
        started = 0
    for path in directory.rglob("*"):
        if path.suffix.lower() not in IMAGE_EXTS or not path.is_file():
            continue
        if "datasets" in {part.lower() for part in path.parts}:
            continue
        try:
            if path.stat().st_mtime < started:
                continue
        except OSError:
            continue
        register_artifact(run, path, kind="image", source_tool="image_scan")


def scan_new_artifacts(run: RunRecord, raw_dir: str | Path) -> None:
    """登记任务开始后写入目录的文件，不登记日志和隐藏文件。"""
    directory = Path(raw_dir).expanduser()
    if not directory.is_dir():
        return
    try:
        started = datetime.fromisoformat(run.started_at).timestamp() - 2
    except (ValueError, TypeError):
        started = 0
    for path in directory.rglob("*"):
        if not path.is_file() or path.name.startswith((".", "~$")):
            continue
        kind = _kind_for_path(path)
        if kind not in {"xlsx", "pptx", "zip", "image"}:
            continue
        try:
            if path.stat().st_mtime < started:
                continue
        except OSError:
            continue
        register_artifact(run, path, kind=kind, source_tool="output_scan")


def register_artifacts_from_result(
    run: RunRecord,
    tool_name: str,
    result: Any,
    *,
    output_dir: Optional[str] = None,
) -> None:
    """从工具结果中提取明确的产物路径。"""
    if not isinstance(result, dict):
        return
    if output_dir:
        add_output_dir(run, output_dir)
    raw_output_dir = result.get("output_dir")
    if raw_output_dir:
        add_output_dir(run, raw_output_dir)

    metadata = {
        "site": result.get("site"),
        "date": result.get("date"),
        "date_range": result.get("date_range"),
    }
    metadata = {key: value for key, value in metadata.items() if value}
    for key in ("xlsx_path", "weekly_xlsx_path", "master_xlsx_path"):
        if result.get(key):
            register_artifact(
                run,
                result[key],
                kind="xlsx",
                source_tool=tool_name,
                metadata=metadata,
            )
    if result.get("pptx_path"):
        register_artifact(
            run,
            result["pptx_path"],
            kind="pptx",
            source_tool=tool_name,
            metadata=metadata,
        )
    if result.get("zip_files"):
        _register_path_list(
            run,
            result["zip_files"],
            source_tool=tool_name,
            metadata=metadata,
        )
    images = result.get("downloaded_images")
    if images:
        if isinstance(images, list):
            for item in images:
                if isinstance(item, dict) and item.get("dir"):
                    _scan_new_images(run, item["dir"])
                else:
                    _register_path_list(
                        run,
                        item,
                        source_tool=tool_name,
                        metadata=metadata,
                    )
        else:
            _register_path_list(
                run, images, source_tool=tool_name, metadata=metadata
            )


def scan_new_images(run: RunRecord, raw_dir: str | Path) -> None:
    _scan_new_images(run, raw_dir)


def finalize_run(run: RunRecord, status: str = "success") -> None:
    run.status = status
    run.finished_at = _now_iso()
    if run.log_path is None:
        for directory in run.output_dirs:
            candidate = Path(directory) / "download_progress.log"
            if candidate.is_file():
                run.log_path = str(candidate)
                break


def load_artifact_history() -> List[Dict[str, Any]]:
    try:
        payload = json.loads(ARTIFACT_HISTORY_PATH.read_text(encoding="utf-8"))
        runs = payload.get("runs") if isinstance(payload, dict) else payload
        return [run for run in runs if isinstance(run, dict)][:MAX_RUNS]
    except (OSError, ValueError, TypeError):
        return []


def save_artifact_history(runs: Iterable[Dict[str, Any]]) -> None:
    clean_runs = [run for run in runs if isinstance(run, dict)][:MAX_RUNS]
    try:
        ARTIFACT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = ARTIFACT_HISTORY_PATH.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps({"version": 1, "runs": clean_runs}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(ARTIFACT_HISTORY_PATH)
    except (OSError, ValueError, TypeError):
        return


def commit_run(session: Dict[str, Any], run: RunRecord) -> None:
    data = run.to_dict()
    session["current_run"] = data
    recent = [
        item for item in session.get("recent_runs", []) if item.get("run_id") != data["run_id"]
    ]
    session["recent_runs"] = [data, *recent][:10]
    persisted = [
        item
        for item in load_artifact_history()
        if item.get("run_id") != data["run_id"]
    ]
    save_artifact_history([data, *persisted])


def artifact_files(run: Dict[str, Any], *, limit: Optional[int] = None) -> List[str]:
    paths = [
        item["path"]
        for item in run.get("artifacts", [])
        if item.get("kind") != "image" and Path(item.get("path", "")).is_file()
    ]
    return paths[:limit] if limit else paths


def artifact_files_by_kind(
    run: Dict[str, Any],
    kinds: Iterable[str],
    *,
    limit: Optional[int] = None,
) -> List[str]:
    """按产物类型筛选仍存在的文件，保持任务登记顺序。"""
    wanted = {str(kind).lower() for kind in kinds}
    paths = [
        item["path"]
        for item in run.get("artifacts", [])
        if str(item.get("kind") or "").lower() in wanted
        and Path(item.get("path", "")).is_file()
    ]
    return paths[:limit] if limit else paths


def image_items(run: Dict[str, Any], *, limit: int = 20) -> List[tuple[str, str]]:
    items: List[tuple[str, str]] = []
    for artifact in run.get("artifacts", []):
        if artifact.get("kind") != "image":
            continue
        path = artifact.get("path", "")
        if not Path(path).is_file():
            continue
        caption = artifact.get("metadata", {}).get("date") or artifact.get("label", "")
        items.append((path, caption))
        if len(items) >= limit:
            break
    return items


def output_dirs(run: Dict[str, Any]) -> List[str]:
    directories = [
        path
        for path in run.get("output_dirs", [])
        if path and Path(path).is_dir()
    ]
    if not directories:
        for artifact in run.get("artifacts", []):
            parent = Path(artifact.get("path", "")).parent
            if parent.is_dir():
                value = str(parent.resolve())
                if value not in directories:
                    directories.append(value)
    return directories
