"""
前端 3D 的**数值探针**：真的建场景、真的喂一帧输入、真的比较前后位置。

══════════════════════════════════════════════════════════════════
为什么这个项目的后端测试里会出现"跑 node"
══════════════════════════════════════════════════════════════════
2026-09-24 需求方反馈「wasd 出现混乱，按住 a 甚至可能是往右走」。

根因在 `frontend/src/three/rig.ts`：世界位移 → 图纸位移的负号
**只写在行走分支里**，自由视角分支直接 `py += dz`。于是自由视角下
WASD 全部沿图纸 y 轴反了。

这个 bug 逃过了当时全部的检查，理由值得记下来：

  · **静态检查看不见** —— `test_frontend_contract.py` 那一类比对的是
    "两边的枚举/字段名是否一致"。而这里两边都对，错的是**符号**。
  · **类型全对** —— 全是 `number`，`vue-tsc` 一声不响。
  · **单元测试看不见** —— 后端没有这层几何。
  · **人工看也不一定看得见** —— 自由视角是穿墙的，没有"撞墙"这种
    外部反馈；而且它在**一半的情况下**看起来是对的（行走模式）。

只有"跑起来、动起来、比起始位置"能抓住它。所以这里做数值验证。

══════════════════════════════════════════════════════════════════
⚠️ 判据必须独立于被测代码
══════════════════════════════════════════════════════════════════
探针用 `THREE.Object3D.getWorldDirection()` 和 `camera.quaternion`
（由 three 自己从相机矩阵算）作为"前方/右方"的基准，**不是**用
`rig.ts` 里那两行 `-Math.sin(yaw)` 公式 —— 用后者等于自己验自己，
符号写反时两边一起反，点积照样是 +1。

同一条纪律在别处也用过：`test_frontend_contract.py` 直接解析前端源码
而不是复用后端枚举；`SceneViewer` 的小地图用后端的投影而不是 Three 的。

══════════════════════════════════════════════════════════════════
跳过而不是失败
══════════════════════════════════════════════════════════════════
探针要 node + `frontend/node_modules`。`deploy/Dockerfile` 不 COPY
`frontend/`，CI 里也可能没装 node —— 那种环境下这条不该把测试套件弄红
（与 `test_frontend_contract.py` 的处理一致：前端源码不在就 skip）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FRONTEND = REPO / "frontend"
PROBE = FRONTEND / "probe" / "rig_wasd.ts"
PROBE_START = FRONTEND / "probe" / "rig_start_pose.ts"
RUNNER = FRONTEND / "probe" / "run.mjs"

#: 探针要跑几十个用例、每个都新建一次 three 场景，esbuild 还要打一次 bundle。
#: 实测约 6 秒；给到 120 秒是留冷启动的余量（Windows 上首次 node 启动偏慢）。
_TIMEOUT_S = 120


def _find_node() -> str | None:
    """
    找 node。

    ⚠️ 本机 node 装在 `D:\\VibeCoding\\NodeJS` 而**不在 PATH 上** ——
    所以先试环境变量、再试 PATH，最后才试几个常见安装位置。
    直接写死一个绝对路径是不行的：换台机器就静默跳过，
    而"静默跳过"正是这个项目最不想要的那种失败。
    """
    for env in ("QY_NODE", "NODE"):
        p = os.environ.get(env)
        if p and Path(p).exists():
            return str(p)
    found = shutil.which("node")
    if found:
        return found
    for cand in (
        Path("C:/Program Files/nodejs/node.exe"),
        Path("D:/VibeCoding/NodeJS/node.exe"),
        Path("/usr/bin/node"),
        Path("/usr/local/bin/node"),
    ):
        if cand.exists():
            return str(cand)
    return None


NODE = _find_node()

pytestmark = pytest.mark.skipif(
    NODE is None or not PROBE.exists() or not (FRONTEND / "node_modules").exists(),
    reason="没有 node 或没有 frontend/node_modules —— 跳过前端 3D 数值探针",
)


def _run_probe(probe: Path = PROBE) -> tuple[int, dict | None, str]:
    """
    跑探针。返回 `(退出码, 解析出来的报告, stderr)`。

    ⚠️ 探针把 JSON 报告打到 **stdout**、把人看的汇总打到 stderr。
    分开是有意的：报告要给下面的断言读，汇总要给在终端里手跑的人看，
    混在一条流里两边都别扭。
    """
    proc = subprocess.run(
        [NODE, str(RUNNER), str(probe)],
        cwd=str(FRONTEND),
        capture_output=True,
        timeout=_TIMEOUT_S,
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    report: dict | None = None
    start = out.find("{")
    if start >= 0:
        try:
            report = json.loads(out[start:out.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError):
            report = None
    return proc.returncode, report, f"{out}\n{err}".strip()


def test_wasd方向与相机自身的前右方向一致():
    """
    按 W 朝画面正前走、按 A 朝画面左侧走 —— **每个朝向都要成立**。

    ⚠️ 朝向是这条测试的关键。这个 bug 在 yaw=0 时恰好看不出来
    （那时前后正好落在图纸 y 轴上，多出来/少掉的负号落在另一个分量）。
    只测一个朝向的探针会漏掉它 —— 所以探针跑了 8 个朝向 × 4 个方向
    × 2 个模式 = 64 个用例，加上升降与"行走模式不该升降"，共 72 个。
    """
    code, report, raw = _run_probe()
    assert report is not None, f"探针没有输出可解析的 JSON 报告：\n{raw}"

    assert report["total"] >= 64, (
        f"探针只跑了 {report['total']} 个用例 —— 方向用例应当覆盖"
        f"8 个朝向 × 4 个方向 × 2 个模式。用例被删掉的话这条测试就形同虚设"
    )
    assert code == 0 and report["failed"] == 0, (
        "3D 漫游的方向判据挂了：按住某个键时，人的位移方向与相机自身"
        "朝向不一致（点积为负 = 走了反方向）。\n"
        "明细：\n"
        + "\n".join(
            f"  [{r['mode']}] 朝向 {r['heading_deg']}° 按 {r['key']}："
            f"点积 {r['dot']}（{r.get('note') or ''}）"
            for r in report["failures"][:12]
        )
        + f"\n\n探针原始输出：\n{raw[-2000:]}"
    )


def test_自由视角与行走模式的方向判据必须同时成立():
    """
    两个模式**分开断言**，因为它们曾经分叉过。

    实测的形态就是：行走分支写对了、自由视角分支漏了负号。
    合并成一条总数断言的话，一个模式全错、另一个全对时，
    报错只说"有 24 个失败"，看不出是哪个模式 —— 而"只有一半模式错"
    正是这个 bug 最难查的地方。
    """
    code, report, raw = _run_probe()
    assert report is not None, f"探针没有输出报告：\n{raw}"
    for mode in ("walk", "fly"):
        rows = [r for r in report["rows"] if r["mode"] == mode]
        assert rows, f"{mode} 模式下一条用例都没跑"
        bad = [r for r in rows if not r["ok"]]
        assert not bad, (
            f"{mode} 模式有 {len(bad)}/{len(rows)} 个方向用例失败：\n"
            + "\n".join(
                f"  [{mode}] 朝向 {r['heading_deg']}° 按 {r['key']}："
                f"点积 {r['dot']}（{r.get('note') or ''}）"
                for r in bad[:8]
            )
        )
    assert code == 0


def test_按键表是唯一来源_界面不另存一份():
    """
    按键定义只允许在 `three/keys.ts` 里有一份。

    ⚠️ 这是 2026-09-24 那次排查的副产物：查"wasd 混乱"时发现
    "哪个键干什么"在代码里有**三份** —— `KEYMAP`、`onKeyDown` 里的 if、
    以及界面提示文案。三份里改一份，另外两份都不报错。
    （真正的根因不在按键表，但这份重复是真的，而且它会掩盖下一次
    真正的按键 bug：界面教用户按一个已经改掉的键。）

    做法是让提示文案由按键表推出来（`helpLine`），并在下面钉死
    "渲染层不再自己写一份 KEYMAP / 不再手写提示文案"。
    """
    import re

    keys_ts = FRONTEND / "src" / "three" / "keys.ts"
    viewer = FRONTEND / "src" / "components" / "SceneViewer.vue"
    if not keys_ts.exists() or not viewer.exists():
        pytest.skip("前端源码不在")

    ksrc = keys_ts.read_text(encoding="utf-8")
    # W A S D 四个键必须在同一张表里定义
    for code in ("KeyW", "KeyA", "KeyS", "KeyD"):
        assert code in ksrc, f"keys.ts 里没有 {code} —— 按键表被拆散了"
    assert "export const KEYMAP" in ksrc, "keys.ts 不再导出 KEYMAP"
    assert "export function helpLine" in ksrc, "keys.ts 不再提供 helpLine"

    vsrc = viewer.read_text(encoding="utf-8")
    code_only = re.sub(r"/\*.*?\*/", "", vsrc, flags=re.S)
    code_only = re.sub(r"//[^\n]*", "", code_only)

    assert "helpLine(" in code_only, (
        "SceneViewer 没有用 helpLine 生成操作提示 —— 手写的提示文案会与"
        "按键表漂移，界面将教用户按一个不存在的键"
    )
    assert not re.search(r"const\s+KEYMAP\s*[:=]", code_only), (
        "SceneViewer 里又出现了第二份 KEYMAP —— 按键定义必须唯一，"
        "两份里改一份不会报错"
    )
    # 手写"W A S D…"这类提示的痕迹：出现了就说明文案是抄的不是推的
    stale = re.findall(r"['\"][^'\"]*\bW\s+A\s+S\s+D\b[^'\"]*['\"]", code_only)
    assert not stale, (
        f"SceneViewer 里手写了操作文案 {stale} —— 应当由 keys.ts 的"
        f"helpLine() 推出来"
    )
