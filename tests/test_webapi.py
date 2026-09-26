"""导入页后端：文件名净化、上传、预览、确认、归档、报告。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from funhub import webapi
from funhub.webapi import ImportInputError

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "legacy_export_sample.json"


def test_sanitize_filename_keeps_safe_names():
    assert webapi.sanitize_filename("export.json") == "export.json"
    assert webapi.sanitize_filename("my-export_2026.json") == "my-export_2026.json"


def test_sanitize_filename_strips_directories():
    # 目录部分被剥掉，越界写法无法落到 import/ 之外
    assert webapi.sanitize_filename("../../etc/passwd") == "passwd.json"
    assert webapi.sanitize_filename("sub/dir/export.json") == "export.json"


def test_sanitize_filename_forces_json_suffix():
    assert webapi.sanitize_filename("export.txt") == "export.txt.json"


def test_sanitize_filename_replaces_unsafe_names():
    stamp = datetime(2026, 1, 2, 3, 4, 5)
    assert webapi.sanitize_filename("龟龟的导出.json", now=stamp) == "upload_20260102-030405.json"
    assert webapi.sanitize_filename("a b.json", now=stamp) == "upload_20260102-030405.json"
    assert webapi.sanitize_filename("", now=stamp) == "upload_20260102-030405.json"
    assert webapi.sanitize_filename("x" * 200 + ".json", now=stamp) == "upload_20260102-030405.json"


def test_resolve_import_file_rejects_traversal(data_dir: Path):
    for name in ("../outside.json", "sub/x.json", "/etc/passwd", "", "x.txt"):
        with pytest.raises(ImportInputError):
            webapi.resolve_import_file(data_dir, name)


def test_stage_upload_writes_into_import_dir(data_dir: Path):
    path = webapi.stage_upload(data_dir, "export.json", b'{"users": []}', max_bytes=1024)
    assert path.parent == webapi.import_dir(data_dir)
    assert path.name == "export.json"
    assert path.read_bytes() == b'{"users": []}'


def test_stage_upload_avoids_collisions(data_dir: Path):
    first = webapi.stage_upload(data_dir, "export.json", b"{}", max_bytes=1024)
    second = webapi.stage_upload(data_dir, "export.json", b"{}", max_bytes=1024)
    assert first.name == "export.json"
    assert second.name == "export-2.json"


def test_stage_upload_rejects_empty_and_oversized(data_dir: Path):
    with pytest.raises(ImportInputError):
        webapi.stage_upload(data_dir, "export.json", b"", max_bytes=1024)
    with pytest.raises(ImportInputError):
        webapi.stage_upload(data_dir, "export.json", b"x" * 2048, max_bytes=1024)


async def test_preview_reports_without_writing(database, settings, data_dir: Path):
    webapi.stage_upload(data_dir, "export.json", SAMPLE.read_bytes(), max_bytes=10**7)
    report = await webapi.preview_import(database, settings, data_dir, "export.json")

    assert report.dry_run is True
    assert report.checkins_inserted == 5
    assert report.coins_granted == 2645
    row = await database.fetchone("SELECT COUNT(*) AS total FROM players")
    assert int(row["total"]) == 0
    # 预览不会把文件搬走
    assert (webapi.import_dir(data_dir) / "export.json").is_file()


async def test_apply_import_archives_file_and_records_report(database, settings, data_dir: Path):
    webapi.stage_upload(data_dir, "export.json", SAMPLE.read_bytes(), max_bytes=10**7)
    report = await webapi.apply_import(database, settings, data_dir, "export.json")

    assert report.dry_run is False
    assert report.coins_granted == 2645
    assert not (webapi.import_dir(data_dir) / "export.json").exists()
    assert (webapi.done_dir(data_dir) / "export.json").is_file()

    listing = await webapi.list_pending(database, data_dir)
    assert listing["files"] == []
    assert listing["last_report"]["coins_granted"] == 2645
    assert listing["last_report"]["files"] == ["export.json"]


async def test_apply_is_idempotent_for_repeated_uploads(database, settings, data_dir: Path):
    webapi.stage_upload(data_dir, "export.json", SAMPLE.read_bytes(), max_bytes=10**7)
    first = await webapi.apply_import(database, settings, data_dir, "export.json")

    webapi.stage_upload(data_dir, "export.json", SAMPLE.read_bytes(), max_bytes=10**7)
    second = await webapi.apply_import(database, settings, data_dir, "export.json")

    assert first.coins_granted == 2645
    assert second.coins_granted == 0
    assert second.checkins_inserted == 0


async def test_apply_import_without_backfill(database, settings, data_dir: Path):
    webapi.stage_upload(data_dir, "export.json", SAMPLE.read_bytes(), max_bytes=10**7)
    report = await webapi.apply_import(
        database,
        settings,
        data_dir,
        "export.json",
        backfill_coins=False,
    )
    assert report.coins_granted == 0
    assert report.checkins_inserted == 5


async def test_list_pending_lists_files(database, data_dir: Path):
    webapi.stage_upload(data_dir, "b.json", b"{}", max_bytes=1024)
    webapi.stage_upload(data_dir, "a.json", b"{}", max_bytes=1024)
    listing = await webapi.list_pending(database, data_dir)
    assert [item["name"] for item in listing["files"]] == ["a.json", "b.json"]
    assert listing["files"][0]["size"] == 2
    assert listing["last_report"] is None


async def test_invalid_json_is_rejected(database, settings, data_dir: Path):
    webapi.stage_upload(data_dir, "broken.json", b"{not json", max_bytes=1024)
    with pytest.raises(ImportInputError):
        await webapi.preview_import(database, settings, data_dir, "broken.json")


async def test_missing_users_list_is_rejected(database, settings, data_dir: Path):
    webapi.stage_upload(data_dir, "wrong.json", json.dumps({"foo": 1}).encode(), max_bytes=1024)
    with pytest.raises(ImportInputError):
        await webapi.apply_import(database, settings, data_dir, "wrong.json")


async def test_missing_file_is_rejected(database, settings, data_dir: Path):
    with pytest.raises(ImportInputError):
        await webapi.apply_import(database, settings, data_dir, "nope.json")


async def test_describe_marks_preview(database, settings, data_dir: Path):
    webapi.stage_upload(data_dir, "export.json", SAMPLE.read_bytes(), max_bytes=10**7)
    preview = await webapi.preview_import(database, settings, data_dir, "export.json")
    assert webapi.describe(preview).startswith("预览：")
    report = await webapi.apply_import(database, settings, data_dir, "export.json")
    assert webapi.describe(report).startswith("导入完成：")
    assert "补发 2645 币" in webapi.describe(report)
