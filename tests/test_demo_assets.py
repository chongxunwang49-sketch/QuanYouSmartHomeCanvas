"""
演示素材：三张户型图 ↔ 三份屋主户型详情，一一对应。

═══════════════════════════════════════════════════════════════════
这个文件防的是什么
═══════════════════════════════════════════════════════════════════
`演示素材/演示资料/` 里放着三份"屋主户型详情"，与 `演示素材/户型图/` 里的
三张图**同名配对**。演示流程是「上传某张图 → 解析 → 点『载入演示样例』 →
保存并重新诊断」，所以有两条会当场翻车、但不会报错的路径：

  ① 样例配错了图 —— 后端是按 `total_area` 找最近的一份（`routes.py`）。
     某一份的面积数字一改，最近邻就可能跳到另一份上，于是给两室一厅
     配上三室两厅的详情。界面上看不出来，诊断结果却全是错的。
  ② 同一份文档有两个位置（人看的 `演示素材/演示资料/`、后端读的
     `seed_data/demo_house_details/`，理由见 `scripts/sync_demo_details.py`），
     改了一份忘了另一份。同样是**不报错，只是内容悄悄不一样**。

下面逐条钉住它们。
"""

from __future__ import annotations

import hashlib
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 人看的、也是作者改的那一份（唯一真源）。
SRC = ROOT / "演示素材" / "演示资料"

#: 后端运行时读的那一份（`routes.py::_demo_house_details` 读的就是它）。
DST = ROOT / "seed_data" / "demo_house_details"

PLANS = ROOT / "演示素材" / "户型图"

#: 三张演示图**实际解析出来的**总面积（㎡）。
#:
#: 这几个数不是抄图里的标题，也不该手算 —— 标题与图上逐间标注的口径不一致
#: （01 图标题写"约45㎡"、逐间标注合计 46.8㎡）。它们是 2026-09-27 把三张图
#: 各真跑一次 `POST /layout/parse` 量出来的，原始记录在
#: `logs/demo-parse/*-layout.txt`。图一改，这里就该跟着重新量。
PLAN_TOTAL_AREA = {"01": 46.8, "02": 92.1, "03": 98.1}

_AREA_RE = re.compile(r"建筑面积[：:]\s*([0-9]+(?:\.[0-9]+)?)")


def _docs() -> list[pathlib.Path]:
    return sorted(SRC.glob("*.md")) if SRC.is_dir() else []


def _digest(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _area_of(p: pathlib.Path) -> float:
    """
    取出这份详情声明的建筑面积。

    ⚠️ 用**和后端同一个正则**（`routes.py::_demo_house_details`）而不是自己写一个：
       两边口径不一致的话，测试量的是"我以为后端会读到的数"，
       而后端实际读到的是别的 —— 那就白测了。
    """
    m = _AREA_RE.search(p.read_text(encoding="utf-8"))
    assert m, f"{p.name} 里找不到「建筑面积：…」，后端的按面积匹配会拿不到值"
    return float(m.group(1))


# ══════════════════════════════════════════════════════════════════
# 一一对应
# ══════════════════════════════════════════════════════════════════


def test_三张图与三份详情数量一致且同名配对():
    """一一对应的判据就是同名：`01-一室一厅-45平.md` ↔ `01-一室一厅-45平.png`。"""
    docs = _docs()
    plans = sorted(p.name for p in PLANS.glob("*.png"))
    assert len(docs) == 3, f"演示详情应当是 3 份，实际 {len(docs)} 份"
    assert len(plans) == 3, f"演示户型图应当是 3 张，实际 {len(plans)} 张"
    assert [d.stem + ".png" for d in docs] == plans, (
        f"详情与户型图没配上：\n  详情 {[d.stem for d in docs]}\n  图   {plans}"
    )


def test_每份详情里都写明了自己对应哪张图():
    """
    同名配对是**文件系统层面**的约定，打开文件本身看不出来。
    每份详情的第一屏必须写死对应关系 —— 演示时把文件单独发给别人也不会配错。
    """
    for d in _docs():
        text = d.read_text(encoding="utf-8")
        expect = f"对应户型图：演示素材/户型图/{d.stem}.png"
        assert expect in text, f"{d.name} 里没有写「{expect}」"


# ══════════════════════════════════════════════════════════════════
# 面积最近邻 —— 演示时"载入演示样例"到底会给哪一份
# ══════════════════════════════════════════════════════════════════


def test_每张图载入样例时都会拿到自己那一份():
    """
    复刻 `routes.py::sample_house_detail` 的选择逻辑：拿 `layout["total_area"]`
    去比每份详情的建筑面积，取最近的一份。

    这条是**演示阻断级**的：配错一份，整场演示的诊断结果都是别人的房子。
    """
    areas = {d.stem[:2]: _area_of(d) for d in _docs()}
    for num, layout_area in PLAN_TOTAL_AREA.items():
        best = min(areas, key=lambda k: abs(areas[k] - layout_area))
        assert best == num, (
            f"图 {num}（解析总面积 {layout_area}㎡）会匹配到 {best} 那份详情：\n"
            + "\n".join(
                f"    {k}: 详情 {v}㎡  差 {abs(v - layout_area):.1f}"
                for k, v in sorted(areas.items())
            )
        )


def test_请三份详情的面积彼此拉开距离():
    """
    最近邻要稳，三份面积就不能挨得太近 —— 否则以后有人微调一个数，
    上面那条会静默地翻到另一份上（改了数值但测试还是绿的）。
    """
    areas = sorted(_area_of(d) for d in _docs())
    for a, b in zip(areas, areas[1:]):
        assert b - a > 5.0, f"两份详情的建筑面积只差 {b - a:.1f}㎡，最近邻太容易翻"


# ══════════════════════════════════════════════════════════════════
# 两个位置必须逐字节相同
# ══════════════════════════════════════════════════════════════════


def test_真源与镜像逐字节一致():
    docs = _docs()
    names = {d.name for d in docs}
    for d in docs:
        mirror = DST / d.name
        assert mirror.is_file(), f"seed_data 里缺 {d.name}（跑 scripts/sync_demo_details.py）"
        assert _digest(mirror) == _digest(d), (
            f"{d.name} 两个位置的内容不一样 —— 跑 scripts/sync_demo_details.py 同步"
        )
    extra = [f.name for f in sorted(DST.glob("*.md")) if f.name not in names]
    assert not extra, f"seed_data 里有多余的镜像：{extra}"


# ══════════════════════════════════════════════════════════════════
# 正文本身
# ══════════════════════════════════════════════════════════════════


def test_正文不带头号加粗等标记():
    """
    详情是通过解析页的 textarea 让用户看和改的，**按纯文本渲染** ——
    写了 `**层高**` 界面上就会原样显示四个星号。

    这与项目里"界面可见的字符串不写 Markdown"是同一条纪律
    （`tests/test_frontend_contract.py` 管前端源码，这里管演示数据）。
    """
    for d in _docs():
        text = d.read_text(encoding="utf-8")
        for marker in ("**", "__", "##", "```"):
            assert marker not in text, f"{d.name} 里出现了 Markdown 标记 {marker}"


@pytest.mark.parametrize("section", ["朝向与采光", "通风", "墙体与结构", "收纳"])
def test_每份详情都覆盖了诊断要用的那几项(section: str):
    """
    诊断缺数据的原因就在这几项上（层高、朝向、采光面、通风路径、收纳、墙体）——
    它们平面图上读不出来，全靠这份文档补。少写一项，那一维就还是"数据不足"。
    """
    for d in _docs():
        assert section in d.read_text(encoding="utf-8"), (
            f"{d.name} 缺少「{section}」这一节"
        )
