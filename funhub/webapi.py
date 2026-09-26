"""导入页与 CLI 共用的后端逻辑。

这里只做「文件怎么落盘、哪个文件、报告怎么写」这类编排，具体的入库政策在
``legacy.py``。本模块不 import astrbot，Page 的 HTTP handler 由 ``main.py`` 薄薄
地包一层 ``astrbot.api.web`` 即可。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .legacy import ImportReport, LegacyImportError, import_export, load_export_file
from .messages import import_summary

IMPORT_DIR_NAME = "import"
DONE_DIR_NAME = "done"
LAST_REPORT_KEY = "last_import"
SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
UPLOAD_PREFIX = "upload_"


class ImportInputError(ValueError):
    """输入不可用，可直接展示给使用者。"""


@dataclass(frozen=True, slots=True)
class PendingFile:
    name: str
    size: int
    modified_at: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "size": self.size, "modified_at": self.modified_at}


def import_dir(data_dir: str | Path) -> Path:
    path = Path(data_dir) / IMPORT_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def done_dir(data_dir: str | Path) -> Path:
    path = import_dir(data_dir) / DONE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_filename(name: str, *, now: datetime | None = None) -> str:
    """把上传的文件名收敛成一个安全的、必定以 ``.json`` 结尾的名字。"""
    candidate = Path(str(name or "").strip()).name
    if not SAFE_NAME_PATTERN.match(candidate):
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        return f"{UPLOAD_PREFIX}{stamp}.json"
    if not candidate.lower().endswith(".json"):
        candidate = f"{candidate}.json"
    return candidate


def _unique_path(directory: Path, filename: str) -> Path:
    target = directory / filename
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    index = 2
    while True:
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def resolve_import_file(data_dir: str | Path, filename: str) -> Path:
    """把文件名解析成 ``import/`` 下的真实路径，拒绝任何越界写法。"""
    if not filename or not SAFE_NAME_PATTERN.match(filename):
        raise ImportInputError("文件名不合法，只允许字母、数字、点、下划线和短横线")
    root = import_dir(data_dir).resolve()
    target = (root / filename).resolve()
    if target.parent != root or target.suffix.lower() != ".json":
        raise ImportInputError("文件名不合法或不是 .json 文件")
    if not target.is_file():
        raise ImportInputError(f"文件不存在：{filename}")
    return target


def stage_upload(
    data_dir: str | Path,
    filename: str,
    raw: bytes,
    *,
    max_bytes: int,
    now: datetime | None = None,
) -> Path:
    """把上传内容落到 ``import/`` 并返回路径。"""
    if not raw:
        raise ImportInputError("上传的文件是空的")
    if len(raw) > max_bytes:
        raise ImportInputError(f"文件超过上限 {max_bytes // (1024 * 1024)} MB")
    target = _unique_path(import_dir(data_dir), sanitize_filename(filename, now=now))
    target.write_bytes(raw)
    return target


async def list_pending(database: Database, data_dir: str | Path) -> dict[str, Any]:
    """待导入文件列表 + 上一次的导入报告。"""
    directory = import_dir(data_dir)
    files: list[dict[str, Any]] = []
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if not entry.is_file() or entry.suffix.lower() != ".json":
            continue
        stat = entry.stat()
        files.append(
            PendingFile(
                name=entry.name,
                size=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime).isoformat(
                    timespec="seconds",
                ),
            ).as_dict(),
        )

    last_report: dict[str, Any] | None = None
    raw = await database.get_meta(LAST_REPORT_KEY)
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            last_report = parsed
    return {"files": files, "last_report": last_report}


async def preview_import(
    database: Database,
    settings: Settings,
    data_dir: str | Path,
    filename: str,
    *,
    backfill_coins: bool | None = None,
) -> ImportReport:
    """只读预览：报告里就是"确认后会补发多少"，不写库。"""
    path = resolve_import_file(data_dir, filename)
    payload = _load_payload(path)
    return await _run_import(
        database,
        settings,
        payload,
        filename=path.name,
        dry_run=True,
        backfill_coins=backfill_coins,
    )


async def apply_import(
    database: Database,
    settings: Settings,
    data_dir: str | Path,
    filename: str,
    *,
    backfill_coins: bool | None = None,
) -> ImportReport:
    """真正导入，成功后把文件归档到 ``import/done/`` 并记录报告。"""
    path = resolve_import_file(data_dir, filename)
    payload = _load_payload(path)
    report = await _run_import(
        database,
        settings,
        payload,
        filename=path.name,
        dry_run=False,
        backfill_coins=backfill_coins,
    )

    archived = _unique_path(done_dir(data_dir), path.name)
    try:
        shutil.move(str(path), str(archived))
    except OSError:
        # 归档失败不该让已经落库的导入看起来像失败了。
        pass

    await database.set_meta(
        LAST_REPORT_KEY,
        json.dumps(report.as_dict(), ensure_ascii=False),
    )
    return report


def _load_payload(path: Path) -> Any:
    try:
        return load_export_file(path)
    except LegacyImportError as exc:
        raise ImportInputError(str(exc)) from exc


async def _run_import(
    database: Database,
    settings: Settings,
    payload: Any,
    *,
    filename: str,
    dry_run: bool,
    backfill_coins: bool | None,
) -> ImportReport:
    """结构校验失败也必须是"输入错误"，不能变成 500。"""
    try:
        return await import_export(
            database,
            payload,
            settings,
            filename=filename,
            dry_run=dry_run,
            backfill_coins=backfill_coins,
        )
    except LegacyImportError as exc:
        raise ImportInputError(str(exc)) from exc


def describe(report: ImportReport, *, preview: bool | None = None) -> str:
    """报告的一行文本形式（CLI 与日志用）。"""
    return import_summary(report, preview=preview)
