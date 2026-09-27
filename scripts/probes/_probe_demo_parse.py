"""
一次性探针：把 `演示素材/户型图/` 下三张演示图各真跑一遍解析，
把解析出来的房间/门窗/面积落盘，供写「屋主户型详情」时对齐用。

    python scripts/probes/_probe_demo_parse.py            # 三张全跑
    python scripts/probes/_probe_demo_parse.py 01 03      # 只跑指定编号

⚠️ 这不是验收脚本，是**一次性测量工具**（起名 `_probe_` 与本项目其它探针一致）。
   解析要调视觉模型，两张约 1–2 分钟。
"""

from __future__ import annotations

import base64
import json
import pathlib
import sys
import time
import urllib.request

def _project_root() -> pathlib.Path:
    """
    往上找到项目根（**不要写死层数**）。

    ⚠️ 这里原先写的是 `Path(__file__).resolve().parent.parent` —— 在 `scripts/` 下
    正好是项目根。2026-09-28 把这批一次性探针收进 `scripts/probes/` 之后，
    它就变成了 `scripts/`：于是 `sys.path` 插错、`import backend` 直接失败，
    而 `ROOT / "演示素材"` 也会指到一个不存在的地方。
    改成"往上找带 backend/app 的那一级"，以后无论挪到哪一层都对。
    """
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "backend" / "app").is_dir():
            return parent
    return here.parent


ROOT = _project_root()
BASE = "http://127.0.0.1/api/v1"
PLANS = ROOT / "演示素材" / "户型图"
OUT = ROOT / "logs" / "demo-parse"

for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass


def call(path: str, body: dict | None = None, token: str | None = None,
         timeout: float = 120.0) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def login() -> str:
    j = call("/auth/login", {"username": "vip", "password": "vip123"})
    return j["data"]["access_token"]


def parse_one(png: pathlib.Path, token: str) -> dict:
    b64 = base64.b64encode(png.read_bytes()).decode("ascii")
    j = call("/layout/parse", {
        "image": f"data:image/png;base64,{b64}",
        "image_media_type": "image/png",
        "detail_level": "full",
    }, token)
    task_id = j["data"]["task_id"]
    print(f"  task_id = {task_id}")
    t0 = time.time()
    while True:
        time.sleep(3)
        st = call(f"/task/{task_id}/status", None, token)["data"]
        el = time.time() - t0
        print(f"    [{el:5.1f}s] status={st.get('status')} phase={st.get('phase')} "
              f"progress={st.get('progress')}")
        if st.get("status") in ("completed", "failed", "cancelled"):
            return {"task_id": task_id, "elapsed": round(el, 1), "status": st}
        if el > 300:
            raise TimeoutError("轮询超过 300 秒")


def summarize(layout: dict) -> str:
    if not layout:
        return "（没有 layout）"
    lines = [
        f"layout_id   = {layout.get('layout_id')}",
        f"total_area  = {layout.get('total_area')}",
        f"confidence  = {layout.get('confidence')}  mode={layout.get('mode')}",
        f"north_arrow = {layout.get('has_north_arrow')}  "
        f"entrance    = {layout.get('entrance_orientation')}",
        f"dimensions  = {layout.get('dimensions')}",
        "房间：",
    ]
    for r in layout.get("rooms") or []:
        lines.append(f"  - {r.get('name')}（{r.get('type')}）{r.get('area')}㎡ "
                     f"朝向={r.get('orientation')}")
    total = sum((r.get("area") or 0) for r in layout.get("rooms") or [])
    lines.append(f"  房间面积合计 = {total:.1f}㎡")
    lines.append(f"门（{len(layout.get('doors') or [])}）：")
    for d in layout.get("doors") or []:
        lines.append(f"  - {d}")
    lines.append(f"窗（{len(layout.get('windows') or [])}）：")
    for w in layout.get("windows") or []:
        lines.append(f"  - {w}")
    lines.append("墙体：")
    for w in layout.get("walls") or []:
        lines.append(f"  - type={w.get('type')} {w.get('note')}")
    lines.append(f"warnings = {layout.get('warnings')}")
    lines.append("uncertain_points：")
    for u in layout.get("uncertain_points") or []:
        lines.append(f"  - {u}")
    return "\n".join(lines)


def main() -> int:
    want = sys.argv[1:]
    OUT.mkdir(parents=True, exist_ok=True)
    token = login()
    print("登录成功")
    for png in sorted(PLANS.glob("*.png")):
        num = png.stem[:2]
        if want and num not in want:
            continue
        print(f"\n═══ {png.name} ═══")
        try:
            rec = parse_one(png, token)
        except Exception as e:  # noqa: BLE001
            print(f"  解析失败：{type(e).__name__}: {e}")
            continue
        st = rec["status"]
        if st.get("status") != "completed":
            print(f"  任务未完成：{json.dumps(st, ensure_ascii=False)[:800]}")
            continue
        result = st.get("result") or st.get("data") or st
        (OUT / f"{num}-raw.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        layout = result.get("layout") or {}
        diag = result.get("diagnosis") or layout.get("diagnosis") or {}
        report = summarize(layout)
        (OUT / f"{num}-layout.txt").write_text(report, encoding="utf-8")
        print(report)
        if diag:
            print(f"\n诊断 overall={diag.get('overall_score')} "
                  f"confidence={diag.get('confidence')} "
                  f"gaps={len(diag.get('data_gaps') or [])}")
            for k, v in (diag.get("dimensions") or {}).items():
                print(f"  - {k}: score={v.get('score')} "
                      f"insufficient={v.get('insufficient_data')}")
    print(f"\n落盘目录：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
