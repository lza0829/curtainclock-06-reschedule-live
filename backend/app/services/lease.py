from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.services.ready import readiness, resource_busy


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bump(conn: sqlite3.Connection, key: str, delta: int) -> int:
    row = conn.execute("SELECT value FROM watermarks WHERE key=?", (key,)).fetchone()
    cur = int(row["value"]) if row else 0
    nxt = cur + delta
    conn.execute(
        "INSERT INTO watermarks(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, nxt, _now()),
    )
    return nxt


def _conflict_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE key='conflict_mode'"
    ).fetchone()
    return row["value"] if row else "reject"


def record_conflicts(
    conn: sqlite3.Connection, session: sqlite3.Row | dict, reasons: list[str]
) -> None:
    """冲突落冲突桶并抬水位：开窗与改挂共用的唯一入口（同源）。"""
    for reason in reasons:
        kind = "room" if "算力槽" in reason else ("teacher" if "执行器" in reason else "ready")
        resource = (
            session["room_code"]
            if kind == "room"
            else (session["teacher_code"] if kind == "teacher" else session["code"])
        )
        conn.execute(
            "INSERT INTO conflicts(kind, session_code, resource_code, day, start_min, end_min, reason, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                kind,
                session["code"],
                resource,
                session["day"],
                session["start_min"],
                session["end_min"],
                reason,
                _now(),
            ),
        )
        _bump(conn, "conflict_count", 1)


class RebindConflict(Exception):
    """live 改挂与他窗重叠或未就绪：整笔回滚不改库，由路由层另开事务落冲突桶。"""

    def __init__(self, reasons: list[str], attempt: dict):
        super().__init__("；".join(reasons))
        self.reasons = reasons
        self.attempt = attempt


def open_lease(conn: sqlite3.Connection, session_code: str) -> dict:
    session = conn.execute(
        "SELECT * FROM sessions WHERE code=?", (session_code,)
    ).fetchone()
    if session is None:
        raise ValueError(f"批窗不存在：{session_code}")
    if session["status"] == "live":
        raise ValueError("批窗已在上课，勿重复开窗")
    if session["status"] == "done":
        raise ValueError("批窗已结束")

    ready = readiness(conn, session)
    if not ready["ready"]:
        mode = _conflict_mode(conn)
        record_conflicts(conn, session, ready["reasons"])
        # 底座贯彻 reject（冲突落库后由调用方提交）；候补算力槽留给后续题
        return {
            "ok": False,
            "error": "；".join(ready["reasons"]),
            "conflict_mode": mode,
        }

    if resource_busy(
        conn,
        kind="room",
        resource_code=session["room_code"],
        day=session["day"],
        start_min=session["start_min"],
        end_min=session["end_min"],
    ) or resource_busy(
        conn,
        kind="teacher",
        resource_code=session["teacher_code"],
        day=session["day"],
        start_min=session["start_min"],
        end_min=session["end_min"],
    ):
        raise ValueError("资源刚被占用，开窗失败")

    n = conn.execute("SELECT COUNT(*) c FROM leases").fetchone()["c"] + 1
    lease_no = f"L-{session['day'].replace('-', '')}-{n:04d}"
    conn.execute(
        "INSERT INTO leases(lease_no, session_code, room_code, teacher_code, day, start_min, end_min, status, opened_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            lease_no,
            session_code,
            session["room_code"],
            session["teacher_code"],
            session["day"],
            session["start_min"],
            session["end_min"],
            "open",
            _now(),
        ),
    )
    conn.execute(
        "UPDATE sessions SET status='live' WHERE code=?", (session_code,)
    )
    open_n = _bump(conn, "open_leases", 1)
    return {
        "ok": True,
        "lease_no": lease_no,
        "session_code": session_code,
        "status": "open",
        "open_leases": open_n,
    }


def close_lease(conn: sqlite3.Connection, lease_no: str) -> dict:
    lease = conn.execute(
        "SELECT * FROM leases WHERE lease_no=?", (lease_no,)
    ).fetchone()
    if lease is None:
        raise ValueError(f"租约不存在：{lease_no}")
    if lease["status"] != "open":
        raise ValueError("租约不在占用态")
    conn.execute(
        "UPDATE leases SET status='closed', closed_at=? WHERE lease_no=?",
        (_now(), lease_no),
    )
    conn.execute(
        "UPDATE sessions SET status='done' WHERE code=?",
        (lease["session_code"],),
    )
    open_n = _bump(conn, "open_leases", -1)
    return {
        "lease_no": lease_no,
        "status": "closed",
        "open_leases": open_n,
    }


def rebind_session(
    conn: sqlite3.Connection, session_code: str, patch: dict
) -> dict:
    """改挂唯一入口，全局贯彻「同事务改挂」臂：

    - draft：仅改批窗定义，无租约可动；
    - live：定义行与 open 租约行在同一事务内同步改挂时段与资源码，
      占用索引（leases 行）随资源码移动，不留旧资源幽灵占用；
      与他窗重叠或未就绪则抛 RebindConflict，整笔回滚、不改库；
    - done：定义封存，禁止改挂。

    就绪/重叠预检与开窗同源（readiness，排除本窗自身 open 租约）。
    """
    session = conn.execute(
        "SELECT * FROM sessions WHERE code=?", (session_code,)
    ).fetchone()
    if session is None:
        raise LookupError(f"批窗不存在：{session_code}")
    if session["status"] == "done":
        raise ValueError("批窗已结束，定义封存，禁止改挂")

    new = {
        "room_code": patch.get("room_code", session["room_code"]),
        "teacher_code": patch.get("teacher_code", session["teacher_code"]),
        "day": patch.get("day", session["day"]),
        "start_min": patch.get("start_min", session["start_min"]),
        "end_min": patch.get("end_min", session["end_min"]),
    }
    if new["end_min"] <= new["start_min"]:
        raise ValueError("结束时刻必须晚于开始时刻")

    if session["status"] == "live":
        attempt = {"code": session_code, **new}
        ready = readiness(conn, attempt)
        if not ready["ready"]:
            raise RebindConflict(ready["reasons"], attempt)
        conn.execute(
            "UPDATE sessions SET room_code=?, teacher_code=?, day=?, start_min=?, end_min=? "
            "WHERE code=? AND status='live'",
            (
                new["room_code"],
                new["teacher_code"],
                new["day"],
                new["start_min"],
                new["end_min"],
                session_code,
            ),
        )
        cur = conn.execute(
            "UPDATE leases SET room_code=?, teacher_code=?, day=?, start_min=?, end_min=? "
            "WHERE session_code=? AND status='open'",
            (
                new["room_code"],
                new["teacher_code"],
                new["day"],
                new["start_min"],
                new["end_min"],
                session_code,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError("live 批窗缺唯一 open 租约，定义与占用已撕裂")
        return {"ok": True, "mode": "live_rebind", "session_code": session_code, **new}

    conn.execute(
        "UPDATE sessions SET room_code=?, teacher_code=?, day=?, start_min=?, end_min=? "
        "WHERE code=? AND status='draft'",
        (
            new["room_code"],
            new["teacher_code"],
            new["day"],
            new["start_min"],
            new["end_min"],
            session_code,
        ),
    )
    return {"ok": True, "mode": "draft_edit", "session_code": session_code, **new}
