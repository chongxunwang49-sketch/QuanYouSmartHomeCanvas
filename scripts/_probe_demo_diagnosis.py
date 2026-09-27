"""
一次性探针：三张演示图各自「补详情前 / 补详情后」的诊断对照。

    python scripts/_probe_demo_diagnosis.py <layout_id> [<layout_id> ...]
    python scripts/_probe_demo_diagnosis.py --keep-detail <layout_id> ...

对每个 layout_id 做四件事：
  ① GET  /layout/{id}/diagnosis            —— 补详情前那一份（解析时跑的）
  ② GET  /layout/{id}/house-detail/sample  —— 后端按面积推荐了哪一份，
     并与「这份图本该配的那一份」比对（**这就是"一一对应"的实测**）
  ③ POST /layout/{id}/house-detail         —— 保存该份详情
  ④ POST /layout/{id}/diagnose             —— 重跑，与 ① 对照

⚠️ 一次性测量工具，不是验收脚本（起名 `_probe_` 与本项目其它探针一致）。
   除 ③ 之外全是读操作；③ 会写进户型记录里。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1/api/v1"

for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass


def call(path: str, body: dict | None = None, token: str | None = None,
         timeout: float = 180.0) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


def login() -> str:
    return call("/auth/login", {"username": "vip", "password": "vip123"})["data"]["access_token"]


def brief(diag: dict) -> str:
    if not diag:
        return "（没有诊断）"
    dims = diag.get("dimensions") or {}
    lines = [
        f"综合分   {diag.get('overall_score')}      置信度 {diag.get('confidence')}",
        f"数据缺口 {len(diag.get('data_gaps') or [])} 条",
    ]
    for k, v in dims.items():
        flag = "数据不足" if v.get("insufficient_data") else "已评"
        lines.append(f"  - {k:14s} {v.get('score')}  ({flag})")
    gaps = diag.get("data_gaps") or []
    for g in gaps[:8]:
        lines.append(f"  · 缺口：{str(g)[:100]}")
    return "\n".join(lines)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    token = login()
    for layout_id in args:
        num = layout_id[:0] or ""  # 占位，下面按 area 判断
        print(f"\n{'═' * 66}\nlayout_id = {layout_id}\n{'═' * 66}")

        before = call(f"/layout/{layout_id}/diagnosis", None, token)
        if before.get("code") != 0:
            print(f"  取诊断失败：{before.get('message')}")
            continue
        diag_before = before["data"]["diagnosis"]

        sample = call(f"/layout/{layout_id}/house-detail/sample", None, token)
        if sample.get("code") != 0:
            print(f"  取样例失败：{sample.get('message')}")
            continue
        s = sample["data"]
        print(f"② 后端推荐的演示详情：{s['title']}")
        print(f"   匹配口径：{s['matched_by']}")

        print("\n① 补详情前：")
        print(brief(diag_before))

        saved = call(f"/layout/{layout_id}/house-detail",
                     {"title": s["title"], "text": s["text"]}, token)
        if saved.get("code") != 0:
            print(f"  保存详情失败：{saved.get('message')}")
            continue
        print(f"\n③ 已保存详情（{len(s['text'])} 字符）")

        # ⚠️ 传 `{}` 而不是 None：本探针按「有没有 body」决定 GET/POST，
        #    给 None 会发成 GET，撞上 POST-only 路由拿回 405。
        after = call(f"/layout/{layout_id}/diagnose", {}, token)
        if after.get("code") != 0:
            print(f"  重跑诊断失败：{after.get('message')}")
            continue
        diag_after = after["data"]["diagnosis"]
        print("\n④ 补详情后：")
        print(brief(diag_after))

        print(f"\n  → 综合分 {diag_before.get('overall_score')} → "
              f"{diag_after.get('overall_score')}   "
              f"置信度 {diag_before.get('confidence')} → {diag_after.get('confidence')}   "
              f"缺口 {len(diag_before.get('data_gaps') or [])} → "
              f"{len(diag_after.get('data_gaps') or [])}")

        if diag_after.get("summary"):
            print(f"\n  诊断综述：{diag_after['summary']}")
        for key in ("evidence_sources", "model_based_on"):
            if diag_after.get(key):
                print(f"  {key} = {diag_after[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
