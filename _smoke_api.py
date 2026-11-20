# -*- coding: utf-8 -*-
"""0-1 API smoke against running server on 11100.

流程：开窗 → 同槽冲突 → live 同事务改挂成功 → 租约同源 → 重叠改挂整笔回滚 → 关窗释放。
建议配合一次性临时库起服：CURTAINCLOCK_DB=$(mktemp) uvicorn app.main:app ...
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:11100/api"


def call(method: str, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"detail": raw}
        return e.code, payload


def main() -> None:
    st, dash = call("GET", "/dashboard")
    assert st == 200, dash
    day = dash["fixture_day"]
    print("dashboard", dash)

    # 开窗占用
    st, open1 = call("POST", "/leases/open", {"session_code": "W-DIM-01"})
    assert st == 200, open1
    print("open1", open1)

    # 同算力槽同时段 → 冲突落桶
    st, created = call(
        "POST",
        "/sessions",
        {
            "code": "W-DUP-01",
            "title": "冲突试窗",
            "subject": "ETL",
            "room_code": "S-ETL-01",
            "teacher_code": "E-PIPE-02",
            "day": day,
            "start_min": 570,
            "end_min": 640,
        },
    )
    assert st == 200, created
    st, conflict = call("POST", "/leases/open", {"session_code": "W-DUP-01"})
    assert st == 409, conflict
    print("conflict", conflict)

    # live 同事务改挂成功：定义与 open 租约同步移动
    st, reb = call(
        "POST",
        "/sessions/W-DIM-01/rebind",
        {
            "room_code": "S-BI-01",
            "teacher_code": "E-BI-01",
            "start_min": 900,
            "end_min": 960,
        },
    )
    assert st == 200 and reb["mode"] == "live_rebind", reb
    print("rebind_live", reb)

    # 租约同源：open 租约行已随定义移动，旧资源无幽灵占用
    st, leases = call("GET", "/leases")
    mine = [l for l in leases if l["lease_no"] == open1["lease_no"]][0]
    assert mine["status"] == "open" and mine["room_code"] == "S-BI-01", mine
    assert mine["teacher_code"] == "E-BI-01" and mine["start_min"] == 900, mine
    print("lease_moved", mine["lease_no"], mine["room_code"], mine["start_min"])

    # 重叠改挂：先开邻窗，再把 live 窗改挂到邻窗资源同时段 → 409 整笔回滚
    st, open2 = call("POST", "/leases/open", {"session_code": "W-FACT-01"})
    assert st == 200, open2
    st, reb2 = call(
        "POST",
        "/sessions/W-DIM-01/rebind",
        {
            "room_code": "S-ETL-02",
            "teacher_code": "E-PIPE-02",
            "start_min": 540,
            "end_min": 630,
        },
    )
    assert st == 409, reb2
    print("rebind_conflict", reb2)

    # 回滚核查：定义仍停在改挂后的 S-BI-01 900-960，未被撕裂
    st, sessions = call("GET", "/sessions")
    w = [s for s in sessions if s["code"] == "W-DIM-01"][0]
    assert w["room_code"] == "S-BI-01" and w["start_min"] == 900, w

    # 关窗释放
    st, closed = call("POST", "/leases/close", {"lease_no": open1["lease_no"]})
    assert st == 200, closed
    st, closed2 = call("POST", "/leases/close", {"lease_no": open2["lease_no"]})
    assert st == 200, closed2
    print("close", closed, closed2)

    st, dash2 = call("GET", "/dashboard")
    assert st == 200 and dash2["open_leases"] == 0, dash2
    assert dash2["conflict_count"] >= 2, dash2
    print("dashboard_after", dash2)
    print("SMOKE_OK")


if __name__ == "__main__":
    main()
