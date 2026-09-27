"""
把一个大推送拆成多个小推送 —— 绕开代理对单个大 POST 的限制。

    python scripts/probes/_seed_github.py            # 干跑：只算要传多少、怎么分批
    python scripts/probes/_seed_github.py --push     # 真推

═══════════════════════════════════════════════════════════════════
为什么需要它
═══════════════════════════════════════════════════════════════════
本机到 GitHub 要走代理，而代理**扛不住单个大 POST**：
实测 26 MB / 16,792 个对象一次推，每次都是
`RPC failed; HTTP 408` / `TLS connect error` / `remote end hung up`；
而一次只推一个提交（几 MB）就过。问题是这条链上有**一个提交独自带来
16,792 个对象**，它一失败，排在它后面的 24 个提交全过不去。

═══════════════════════════════════════════════════════════════════
办法：先把**二进制块（blob）**分批预置到远端
═══════════════════════════════════════════════════════════════════
git 推一个引用时，只发"从新提交可达、而远端还不认识"的对象 —— **不管这些对象
是从哪个引用可达的**。而 blob 是**与路径无关**的（同一份内容在任何树里都是同一个
对象）。所以：

  ① 把"缺的对象"里的 blob 挑出来，按每批 N 个分批；
  ② 建一条**临时分支**（`refs/heads/qy-seed`），一批一个合成提交，
     把这批 blob 挂在合成路径下（`__seed__/000123` 这种，只为占位，不代表任何东西）；
     每批推一次 —— 每次只有几 MB ✓
  ③ 等 blob 都上了远端，再推真正的 `main`：此时它只需要传**树与提交**
     （blob 远端已经有了）→ 包很小 ✓
  ④ 删掉临时分支。

⚠️ **不改写历史**：合成提交只挂在临时分支上，`main` 一个字节都没动过。
⚠️ 临时分支删掉后，那批占位 blob 会变成不可达对象，GitHub 侧迟早会回收 ——
   在不影响任何真实提交的前提下把空间还回去。

它是**一次性工具**，但留着：换个网络环境再遇到"大推送过不去"还能用。
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys


def _project_root() -> pathlib.Path:
    """往上找到带 `backend/app` 的那一级（别写死层数，见 _probe_wall_snap.py 的教训）。"""
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "backend" / "app").is_dir():
            return parent
    return here.parent


ROOT = _project_root()
for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass

#: 每批多少个 blob。16,792 个对象里 blob 占绝大多数、平均约 1.5 KB，
#: 所以 2000 个一批约 3 MB —— 与"一次推一个提交就过"的量级相当。
BATCH = 2000

REMOTE = "origin"
TARGET = "main"
SEED_REF = "refs/heads/qy-seed"


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT))
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr.strip()[:300]}")
    return r.stdout


def missing_objects() -> list[str]:
    """远端没有、而本地 HEAD 需要传的对象（`<sha> [路径]`）。"""
    return [ln for ln in git("rev-list", "--objects",
                             f"{REMOTE}/{TARGET}..HEAD").splitlines() if ln.strip()]


def blobs_only(shas: list[str]) -> list[str]:
    """只留 blob（树与提交留着给最后那次推，它们体量小）。"""
    r = subprocess.run(["git", "cat-file", "--batch-check"],
                       input="\n".join(shas), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT))
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "blob":
            out.append(parts[0])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="把大推送拆成小推送（预置 blob）")
    ap.add_argument("--push", action="store_true", help="真的推；不给就只干跑")
    ap.add_argument("--batch", type=int, default=BATCH)
    args = ap.parse_args()

    head = git("rev-parse", "--short", "HEAD").strip()
    base = git("rev-parse", f"{REMOTE}/{TARGET}").strip()
    objs = missing_objects()
    shas = [ln.split()[0] for ln in objs]
    blobs = blobs_only(shas)
    print(f"本地 HEAD      {head}")
    print(f"远端 {TARGET}   {base[:8]}")
    print(f"缺的对象       {len(objs)} 个（其中 blob {len(blobs)} 个）")
    print(f"分批           {len(blobs) // args.batch + 1} 批 × {args.batch} 个 blob")

    if not args.push:
        print("\n（干跑。加 --push 才会真的推）")
        return 0

    # 索引先设成远端的树 —— 合成提交挂在它上面
    git("read-tree", f"{base}^{{tree}}")
    prev = base
    for i in range(0, len(blobs), args.batch):
        batch = blobs[i:i + args.batch]
        for j, sha in enumerate(batch):
            git("update-index", "--add", "--cacheinfo",
                f"100644,{sha},__seed__/{i + j:06d}")
        tree = git("write-tree").strip()
        prev = git("commit-tree", tree, "-p", prev,
                   "-m", f"seed batch {i // args.batch + 1}").strip()
        n = i // args.batch + 1
        total = len(blobs) // args.batch + 1
        # ⚠️ 加 `+`：临时分支是**每次重跑都重建**的（都从 base 起算），
        #    所以新链不一定是远端那条的后代 —— 不加 `+` 会被判成 non-fast-forward
        #    直接拒掉，而它是个用完就删的占位分支，强推没有任何副作用。
        r = subprocess.run(["git", "push", REMOTE, f"+{prev}:{SEED_REF}"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=str(ROOT))
        ok = "✓" if r.returncode == 0 else "✗"
        print(f"  第 {n}/{total} 批（{len(batch)} 个 blob）：{ok}"
              + ("" if r.returncode == 0 else f"  {r.stderr.strip().splitlines()[-1][:80]}"))
        if r.returncode != 0:
            print("  这批没过去 —— 再跑一次本脚本会接着从缺的继续")
            return 1

    print("\nblob 都预置好了，现在推真正的 main（应该只剩树与提交）：")
    r = subprocess.run(["git", "push", REMOTE, f"HEAD:{TARGET}"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT))
    print("  " + (r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "ok"))
    if r.returncode != 0:
        return 1

    print(f"\n推成功。清理临时分支 {SEED_REF}：")
    git("push", REMOTE, "--delete", SEED_REF.split("/", 2)[-1], check=False)
    print("  已删除（那批占位 blob 变成不可达对象，GitHub 侧迟早回收）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
