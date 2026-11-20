# -*- coding: utf-8 -*-
"""同事务改挂臂核对（服务层，临时库，不依赖运行中的服务器）。

核对口径：
- live 成功改点：定义行与 open 租约行同事务同步移动，旧资源无幽灵占用；
- 重叠回滚：与他窗重叠则整笔回滚，定义/租约均未动，冲突尝试落冲突桶并抬水位；
- 并发改挂与邻窗开窗不得双占（BEGIN IMMEDIATE 串行化，恰一方成功）；
- done 禁止改挂；草稿改点仅动定义；
- 全程不变量：定义与租约不撕裂、open 租约两两不双占、open_leases 水位与实际同源。

运行：python -I _verify_rebind.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

import app.db as db

db.DB_PATH = Path(tempfile.mkdtemp(prefix="curtainclock-verify-")) / "verify.db"

from app.seed import seed_if_empty  # noqa: E402
from app.services import lease as svc  # noqa: E402
from app.services.ready import resource_busy  # noqa: E402

DAY = "2026-11-20"
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def invariant() -> tuple[bool, str]:
    """全局不变量：不撕裂、不双占、水位同源。"""
    conn = db.connect()
    try:
        opens = conn.execute("SELECT * FROM leases WHERE status='open'").fetchall()
        for i, a in enumerate(opens):
            for b in opens[i + 1 :]:
                for col in ("room_code", "teacher_code"):
                    if (
                        a[col] == b[col]
                        and a["day"] == b["day"]
                        and a["start_min"] < b["end_min"]
                        and b["start_min"] < a["end_min"]
                    ):
                        return False, f"双占:{a['lease_no']}与{b['lease_no']}@{col}={a[col]}"
        wm = conn.execute(
            "SELECT value FROM watermarks WHERE key='open_leases'"
        ).fetchone()
        if int(wm["value"]) != len(opens):
            return False, f"水位漂移:open_leases={wm['value']} 实际={len(opens)}"
        for s in conn.execute("SELECT * FROM sessions WHERE status='live'").fetchall():
            mine = [l for l in opens if l["session_code"] == s["code"]]
            if len(mine) != 1:
                return False, f"live {s['code']} 的 open 租约数={len(mine)}"
            l = mine[0]
            for col in ("room_code", "teacher_code", "day", "start_min", "end_min"):
                if l[col] != s[col]:
                    return False, f"撕裂:{s['code']} 定义.{col}={s[col]} 租约.{col}={l[col]}"
        return True, ""
    finally:
        conn.close()


def fetch(session_code: str):
    conn = db.connect()
    try:
        s = conn.execute(
            "SELECT * FROM sessions WHERE code=?", (session_code,)
        ).fetchone()
        l = conn.execute(
            "SELECT * FROM leases WHERE session_code=? AND status='open'",
            (session_code,),
        ).fetchone()
        return (dict(s) if s else None, dict(l) if l else None)
    finally:
        conn.close()


def watermark(key: str) -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT value FROM watermarks WHERE key=?", (key,)
        ).fetchone()
        return int(row["value"]) if row else 0
    finally:
        conn.close()


def conflict_rows(session_code: str) -> int:
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) c FROM conflicts WHERE session_code=?", (session_code,)
        ).fetchone()["c"]
    finally:
        conn.close()


def main() -> None:
    seed_if_empty()
    ok, why = invariant()
    check("初始不变量（不撕裂/不双占/水位同源）", ok, why)

    # 1) 草稿改点：仅定义动，无租约可动
    with db.transaction() as conn:
        r = svc.rebind_session(conn, "W-RPT-01", {"start_min": 960, "end_min": 1020})
    s, l = fetch("W-RPT-01")
    check(
        "草稿改点仅动定义",
        r["mode"] == "draft_edit"
        and s["start_min"] == 960
        and s["end_min"] == 1020
        and l is None,
    )

    # 2) live 成功改点：定义与 open 租约同事务移动
    with db.transaction() as conn:
        opened = svc.open_lease(conn, "W-DIM-01")
    lease_no = opened["lease_no"]
    with db.transaction() as conn:
        r = svc.rebind_session(
            conn,
            "W-DIM-01",
            {
                "room_code": "S-BI-01",
                "teacher_code": "E-BI-01",
                "start_min": 900,
                "end_min": 960,
            },
        )
    s, l = fetch("W-DIM-01")
    check(
        "live 改挂：定义与租约同步移动",
        r["mode"] == "live_rebind"
        and s["room_code"] == "S-BI-01"
        and s["teacher_code"] == "E-BI-01"
        and s["start_min"] == 900
        and l is not None
        and l["lease_no"] == lease_no
        and l["room_code"] == "S-BI-01"
        and l["teacher_code"] == "E-BI-01"
        and l["start_min"] == 900
        and l["end_min"] == 960,
    )
    conn = db.connect()
    try:
        ghost = resource_busy(
            conn,
            kind="room",
            resource_code="S-ETL-01",
            day=DAY,
            start_min=540,
            end_min=630,
        )
        moved = resource_busy(
            conn,
            kind="room",
            resource_code="S-BI-01",
            day=DAY,
            start_min=900,
            end_min=960,
        )
    finally:
        conn.close()
    check("旧资源无幽灵占用", ghost is None)
    check("新资源占用索引随资源码更新", moved is not None and moved["lease_no"] == lease_no)
    ok, why = invariant()
    check("改挂后不变量", ok, why)

    # 3) 重叠回滚：与他窗 open 租约重叠 → 整笔回滚，冲突落桶
    with db.transaction() as conn:
        svc.open_lease(conn, "W-FACT-01")  # S-ETL-02/E-PIPE-02 540-630 live
    before_s, before_l = fetch("W-DIM-01")
    cc0 = watermark("conflict_count")
    try:
        with db.transaction() as conn:
            svc.rebind_session(
                conn,
                "W-DIM-01",
                {
                    "room_code": "S-ETL-02",
                    "teacher_code": "E-PIPE-02",
                    "start_min": 570,
                    "end_min": 640,
                },
            )
        check("重叠改挂被拒", False, "未抛 RebindConflict")
    except svc.RebindConflict as e:
        # 路由层同款：改挂整笔回滚后，另开事务落冲突桶（与开窗同源）
        with db.transaction() as conn:
            svc.record_conflicts(conn, e.attempt, e.reasons)
        check("重叠改挂抛 RebindConflict 并落冲突桶", True)
    after_s, after_l = fetch("W-DIM-01")
    check("重叠回滚：定义未动", before_s == after_s, f"{before_s} -> {after_s}")
    check("重叠回滚：租约未动", before_l == after_l, f"{before_l} -> {after_l}")
    check(
        "冲突落桶且水位同源",
        conflict_rows("W-DIM-01") >= 1 and watermark("conflict_count") > cc0,
    )
    ok, why = invariant()
    check("回滚后不变量", ok, why)

    # 4) 并发：改挂 W-DIM-01 → S-BI-02/E-BI-02 780-870  vs  邻窗 W-RPT-02 开窗（同槽同时段）
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def t_rebind() -> None:
        barrier.wait()
        try:
            with db.transaction() as conn:
                svc.rebind_session(
                    conn,
                    "W-DIM-01",
                    {
                        "room_code": "S-BI-02",
                        "teacher_code": "E-BI-02",
                        "start_min": 780,
                        "end_min": 870,
                    },
                )
            results["rebind"] = "ok"
        except svc.RebindConflict:
            results["rebind"] = "conflict"
        except Exception as exc:  # noqa: BLE001
            results["rebind"] = f"error:{exc}"

    def t_open() -> None:
        barrier.wait()
        try:
            with db.transaction() as conn:
                r = svc.open_lease(conn, "W-RPT-02")
            results["open"] = "ok" if r.get("ok") else "conflict"
        except Exception as exc:  # noqa: BLE001
            results["open"] = f"error:{exc}"

    th1 = threading.Thread(target=t_rebind)
    th2 = threading.Thread(target=t_open)
    th1.start()
    th2.start()
    th1.join()
    th2.join()
    check(
        "并发改挂与邻窗开窗恰一方成功",
        sorted(results.values()) == ["conflict", "ok"],
        str(results),
    )
    ok, why = invariant()
    check("并发后不变量（不双占/不撕裂/水位同源）", ok, why)

    # 5) done 禁止改挂
    _, l = fetch("W-DIM-01")
    with db.transaction() as conn:
        svc.close_lease(conn, l["lease_no"])
    before_s, _ = fetch("W-DIM-01")
    try:
        with db.transaction() as conn:
            svc.rebind_session(conn, "W-DIM-01", {"start_min": 600})
        check("done 禁止改挂", False, "未拒绝")
    except ValueError:
        after_s, _ = fetch("W-DIM-01")
        check("done 禁止改挂且不改库", before_s == after_s)

    # 6) 校验类拒绝
    try:
        with db.transaction() as conn:
            svc.rebind_session(conn, "W-RPT-01", {"start_min": 1020, "end_min": 1000})
        check("结束<=开始被拒", False, "未拒绝")
    except ValueError:
        check("结束<=开始被拒", True)
    try:
        with db.transaction() as conn:
            svc.rebind_session(conn, "W-NOPE-99", {"start_min": 600})
        check("批窗不存在被拒", False, "未拒绝")
    except LookupError:
        check("批窗不存在被拒", True)

    # 7) 回归：开窗 → 同槽冲突 → 关窗释放
    with db.transaction() as conn:
        r = svc.open_lease(conn, "W-RPT-01")  # 草稿已改点 960-1020 @ S-BI-01
    check("按新定义开窗", r.get("ok") is True, str(r))
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions(code, title, subject, room_code, teacher_code, day, start_min, end_min, status) "
            "VALUES('W-DUP-01','冲突试窗','BI','S-BI-01','E-BI-02',?,990,1030,'draft')",
            (DAY,),
        )
    with db.transaction() as conn:
        r = svc.open_lease(conn, "W-DUP-01")
    check("同槽开窗冲突被拒", r.get("ok") is False, str(r))
    _, l = fetch("W-RPT-01")
    with db.transaction() as conn:
        svc.close_lease(conn, l["lease_no"])
    ok, why = invariant()
    check("回归后不变量", ok, why)

    print()
    if FAILS:
        print(f"VERIFY_FAILED: {len(FAILS)} 项 — {FAILS}")
        sys.exit(1)
    print("VERIFY_OK")


if __name__ == "__main__":
    main()
