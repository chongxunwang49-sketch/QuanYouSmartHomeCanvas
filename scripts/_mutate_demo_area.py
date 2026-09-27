"""
变异测试：把 02 那份详情的建筑面积改掉，`tests/test_demo_assets.py` 该不该红。

    python scripts/_mutate_demo_area.py

改完自动还原（try/finally）。**跑不红就是测试没用** ——
这正是本项目对"守卫型测试"的一贯要求（见 tests/test_walkable.py 里的同类做法）。
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGETS = [
    ROOT / "演示素材" / "演示资料" / "02-两室一厅-78平.md",
    ROOT / "seed_data" / "demo_house_details" / "02-两室一厅-78平.md",
]

for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    originals = {p: p.read_bytes() for p in TARGETS}
    try:
        # 把 92.4 改成 60.0 —— 距离 46.8 / 98.1 都比原来近，
        # 01 和 03 两张图的"最近邻"会一起翻到 02 上。
        for p in TARGETS:
            text = p.read_text(encoding="utf-8")
            new = re.sub(r"建筑面积：92\.4", "建筑面积：60.0", text, count=1)
            assert new != text, f"{p.name} 里没找到要改的那个数"
            p.write_text(new, encoding="utf-8")
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_demo_assets.py", "-q",
             "--no-header", "-p", "no:cacheprovider"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        tail = (out.stdout or "").strip().splitlines()
        print("\n".join(tail[-12:]))
        failed = "failed" in (out.stdout or "")
        print(f"\n改掉 02 的面积之后：{'红了 ✓（测试有效）' if failed else '还是绿的 ✗（测试没起作用）'}")
        return 0 if failed else 1
    finally:
        for p, data in originals.items():
            p.write_bytes(data)
        print("已还原两份文件")


if __name__ == "__main__":
    raise SystemExit(main())
