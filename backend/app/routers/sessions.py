from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import connect, transaction
from app.services import lease as lease_svc
from app.services.ready import readiness

router = APIRouter(tags=["sessions"])


class SessionIn(BaseModel):
    code: str = Field(min_length=2)
    title: str
    subject: str
    room_code: str
    teacher_code: str
    day: str
    start_min: int
    end_min: int


class RebindIn(BaseModel):
    """改挂请求：时段与挂接资源，缺省字段保持原值。"""

    day: str | None = None
    start_min: int | None = None
    end_min: int | None = None
    room_code: str | None = None
    teacher_code: str | None = None


@router.get("/sessions")
def list_sessions():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY day, start_min, code"
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["readiness"] = readiness(conn, r)
            out.append(item)
        return out
    finally:
        conn.close()


@router.post("/sessions")
def create_session(body: SessionIn):
    if body.end_min <= body.start_min:
        raise HTTPException(400, "结束时刻必须晚于开始时刻")
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO sessions(code, title, subject, room_code, teacher_code, day, start_min, end_min, status) "
                "VALUES(?,?,?,?,?,?,?,?, 'draft')",
                (
                    body.code,
                    body.title,
                    body.subject,
                    body.room_code,
                    body.teacher_code,
                    body.day,
                    body.start_min,
                    body.end_min,
                ),
            )
            row = conn.execute(
                "SELECT * FROM sessions WHERE code=?", (body.code,)
            ).fetchone()
            return {**dict(row), "readiness": readiness(conn, row)}
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/sessions/{code}/ready")
def session_ready(code: str):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE code=?", (code,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "批窗不存在")
        return readiness(conn, row)
    finally:
        conn.close()


@router.post("/sessions/{code}/rebind")
def rebind_session(code: str, body: RebindIn):
    """改挂入口：draft 改定义；live 同事务改挂定义与 open 租约。

    与他窗重叠/未就绪 → 改挂整笔回滚，冲突尝试另开事务落冲突桶（与开窗同源）。
    """
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(400, "改挂请求为空：至少给一个字段")
    try:
        with transaction() as conn:
            return lease_svc.rebind_session(conn, code, patch)
    except lease_svc.RebindConflict as e:
        with transaction() as conn:
            lease_svc.record_conflicts(conn, e.attempt, e.reasons)
        raise HTTPException(409, "；".join(e.reasons)) from e
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
