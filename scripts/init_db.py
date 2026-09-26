"""
建表 / 迁移入口。

    python scripts/init_db.py            # 应用全部未执行的迁移
    python scripts/init_db.py --dry-run  # 只列出待执行的，不动库
    python scripts/init_db.py --status   # 列出已应用与待执行的

⚠️ **本脚本必须先设置 WindowsSelectorEventLoopPolicy 再连库。**
Windows 默认的 ProactorEventLoop 跑不了 psycopg 的异步模式，报的是
`Psycopg cannot use the 'ProactorEventLoop' to run in async mode` ——
与 `scripts/run_server.py` 里那个坑是同一个（容器里是 Linux，不受影响）。
设置必须发生在这个进程里、且在建立连接之前：它是**进程级**的，
没法由被导入的模块替调用方修好。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

if sys.platform == "win32":  # noqa: E402 —— 必须在导入 psycopg 相关模块前执行
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.core.config import settings  # noqa: E402
from app.db import migrations, pool  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    print(f"目标库：{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}")
    if not settings.ENABLE_DB:
        print("⚠️ ENABLE_DB=false —— 业务落库是关掉的。仍可建表，但应用不会写入。")

    if args.status:
        done = await migrations.applied()
        if done is None:
            print("✗ 数据库连不上，无法读取迁移状态")
            return 2
        files = migrations.discover()
        print(f"\n迁移文件 {len(files)} 个：")
        for version, name, _ in files:
            mark = "✓ 已应用" if version in done else "· 待执行"
            print(f"  {mark}  {version}_{name}")
        await pool.close_pool()
        return 0

    report = await migrations.migrate(dry_run=args.dry_run)
    await pool.close_pool()

    if not report.get("ok"):
        print(f"✗ 失败：{report.get('reason')}")
        return 2

    print(f"已应用：{report['applied'] or '（无，已是最新）'}")
    if report.get("pending"):
        print(f"待执行：{report['pending']}")
    print(f"累计迁移文件：{report['total']} 个")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="建表 / 迁移业务表")
    ap.add_argument("--dry-run", action="store_true", help="只列出待执行的迁移")
    ap.add_argument("--status", action="store_true", help="列出已应用与待执行的迁移")
    return asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
