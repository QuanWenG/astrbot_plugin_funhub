"""命令行导入老插件签到数据（批处理 / 自动化用）。

WebUI 的「数据导入」页是给日常使用准备的；这个脚本用于服务器上没有浏览器、
或者要一次灌很多文件的场景。两者共用 ``funhub`` 里同一套导入代码，报告字段一致。

用法::

    python scripts/import_legacy.py --source levelup_checkin_20260101-120000.json \\
        [--db data/plugin_data/astrbot_plugin_funhub/funhub.db] \\
        [--data-dir <插件数据目录>] [--default-platform aiocqhttp] \\
        [--dry-run] [--no-backfill] [--json]

退出码：0 成功（含被跳过的记录），2 输入/数据库错误，1 未预期异常。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from funhub import webapi  # noqa: E402
from funhub.config import load_settings  # noqa: E402
from funhub.db import Database  # noqa: E402
from funhub.legacy import LegacyImportError, import_export, load_export_file  # noqa: E402
from funhub.messages import import_summary  # noqa: E402

PLUGIN_NAME = "astrbot_plugin_funhub"
DATABASE_FILENAME = "funhub.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把 astrbot_plugin_LevelUpPvp 导出的签到 JSON 导入龟龟乐园。",
    )
    parser.add_argument("--source", required=True, help="老插件导出的 JSON 文件")
    parser.add_argument("--db", default=None, help="目标 funhub.db 路径")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="插件数据目录（其中的 funhub.db 作为目标库）",
    )
    parser.add_argument(
        "--default-platform",
        default=None,
        help="导出数据缺少 platform 字段时使用的平台，默认 aiocqhttp",
    )
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    parser.add_argument(
        "--no-backfill",
        action="store_true",
        help="不按历史记录补发龟龟币（只导入等级与签到记录）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    return parser


def resolve_db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db).expanduser()
    if args.data_dir:
        return Path(args.data_dir).expanduser() / DATABASE_FILENAME
    env_data = os.environ.get("ASTRBOT_DATA_DIR")
    if env_data:
        candidate = (
            Path(env_data).expanduser()
            / "plugin_data"
            / PLUGIN_NAME
            / DATABASE_FILENAME
        )
        if candidate.parent.is_dir():
            return candidate
    print(
        "找不到目标数据库：请用 --db 指定 funhub.db，或用 --data-dir 指定插件数据目录，"
        "或设置 ASTRBOT_DATA_DIR 环境变量。\n"
        f"常见位置：<AstrBot>/data/plugin_data/{PLUGIN_NAME}/{DATABASE_FILENAME}",
        file=sys.stderr,
    )
    raise SystemExit(2)


async def run(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser()
    if not source.is_file():
        print(f"找不到导出文件：{source}", file=sys.stderr)
        return 2

    db_path = resolve_db_path(args)
    overrides: dict[str, object] = {}
    if args.default_platform:
        overrides["default_platform"] = args.default_platform
    settings = load_settings({"import": overrides} if overrides else {})
    for warning in settings.warnings:
        print(f"[配置] {warning}", file=sys.stderr)

    try:
        payload = load_export_file(source)
    except LegacyImportError as exc:
        print(f"读取失败：{exc}", file=sys.stderr)
        return 2

    database = Database(db_path)
    try:
        report = await import_export(
            database,
            payload,
            settings,
            filename=source.name,
            dry_run=args.dry_run,
            backfill_coins=not args.no_backfill,
        )
    finally:
        await database.close()

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(import_summary(report))
        if not report.dry_run:
            print(f"目标数据库：{db_path}")
        for warning in report.warnings:
            print(f"[提示] {warning}", file=sys.stderr)
        for error in report.errors:
            print(f"[异常] {error}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except LegacyImportError as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 2
    except webapi.ImportInputError as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
