"""
演示户型详情 —— 从「作者写的那一份」同步到「后端读的那一份」。

    python scripts/sync_demo_details.py           # 同步（写）
    python scripts/sync_demo_details.py --check   # 只检查是否一致（不写）

═══════════════════════════════════════════════════════════════════
为什么同一份文档有两个地方
═══════════════════════════════════════════════════════════════════
需求方 2026-09-27：

> 我需要你生成详细资料，跟三个平面图一一对应，放置在演示素材文件夹下面
> 创建一个演示资料文件夹存放。

所以**人看的那一份**在 `演示素材/演示资料/` —— 与三张户型图并排，一一对应，
谁做演示都找得到。

而**后端读的那一份**必须在 `seed_data/` 里，原因不在这个脚本，在
`deploy/Dockerfile`：镜像里有 `COPY seed_data/ /app/seed_data/`，
而 `.dockerignore` 明确排除了 `演示素材`（那里面还有 44 MB 的全友版权摄影，
没理由进镜像）。运行时读不到 `演示素材`。

于是同一份内容有两个位置。**两个位置的东西必须逐字节相同** ——
"改了一份忘了另一份"正是这个项目反复在防的那类错误（不报错，只是东西悄悄不一样）。
所以：

  · 作者只改 `演示素材/演示资料/`（那是唯一真源，改完跑一次本脚本）；
  · 本脚本是**单向**的：演示素材 → seed_data，绝不反向；
  · `tests/test_demo_assets.py` 每次跑测试都做一次 `--check`，
    两边一旦不同就红。
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 唯一真源：跟三张户型图并排的那一份，人看的。
SRC = ROOT / "演示素材" / "演示资料"

#: 镜像：后端运行时读的那一份（`routes.py::_demo_house_details`）。
DST = ROOT / "seed_data" / "demo_house_details"

for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_for(doc: pathlib.Path) -> pathlib.Path:
    """这份详情对应的户型图（同名 .png）。一一对应靠的就是这个命名约定。"""
    return ROOT / "演示素材" / "户型图" / f"{doc.stem}.png"


def survey() -> tuple[list[pathlib.Path], list[str]]:
    """列出真源里的文档，并检查每一份都能找到同名的户型图。"""
    if not SRC.is_dir():
        return [], [f"真源目录不存在：{SRC}"]
    docs = sorted(SRC.glob("*.md"))
    problems = [
        f"{d.name}：找不到对应的户型图 {plan_for(d).name}"
        for d in docs if not plan_for(d).is_file()
    ]
    return docs, problems


def main() -> int:
    ap = argparse.ArgumentParser(description="同步演示户型详情（演示素材 → seed_data）")
    ap.add_argument("--check", action="store_true",
                    help="只比对，不写盘；不一致时退出码非 0")
    args = ap.parse_args()

    docs, problems = survey()
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    if not docs:
        print(f"  ✗ {SRC} 里没有任何 .md")
        return 1

    changed: list[str] = []
    wrote = 0
    for d in docs:
        target = DST / d.name
        same = target.is_file() and digest(target) == digest(d)
        if same:
            print(f"  = {d.name}（一致）")
            continue
        if args.check:
            print(f"  ✗ {d.name}（两边不一样）")
            changed.append(d.name)
            continue
        DST.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(d, target)
        wrote += 1
        print(f"  → {d.name}（已同步，{len(d.read_bytes())} 字节）")

    # 反向也要查：seed_data 里多出来的文件同样是一种不一致
    extra = [
        f.name for f in sorted(DST.glob("*.md")) if f.name not in {d.name for d in docs}
    ]
    for name in extra:
        print(f"  ✗ {name}：只在 seed_data 里，真源里没有（多余的镜像）")
        changed.append(name)

    if args.check:
        if changed:
            print(f"\n共 {len(changed)} 处不一致 —— 跑 "
                  f"`python scripts/sync_demo_details.py` 同步")
            return 1
        print(f"\n{len(docs)} 份演示详情，两边逐字节一致")
        return 0

    print(f"\n同步完成：{wrote} 份已写入，真源 {SRC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
