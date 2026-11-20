import { Fragment, useEffect, useState } from "react";
import { api } from "../api";

function fmtMin(m) {
  const h = String(Math.floor(m / 60)).padStart(2, "0");
  const mm = String(m % 60).padStart(2, "0");
  return `${h}:${mm}`;
}

function toMin(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
}

const statusLabel = { draft: "待开窗", live: "上课中", done: "已结束" };

export default function Sessions() {
  const [rows, setRows] = useState([]);
  const [rooms, setRooms] = useState([]);
  const [teachers, setTeachers] = useState([]);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  async function load() {
    setErr("");
    try {
      const [s, r, t] = await Promise.all([
        api.sessions(),
        api.rooms(),
        api.teachers(),
      ]);
      setRows(s);
      setRooms(r);
      setTeachers(t);
    } catch (e) {
      setErr(e.message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function openLease(code) {
    setMsg("");
    setErr("");
    try {
      const r = await api.openLease(code);
      setMsg(`已开窗，租约 ${r.lease_no}；在途租约 ${r.open_leases}`);
      await load();
    } catch (e) {
      setErr(e.message);
      await load();
    }
  }

  function startEdit(r) {
    setEditing(r.code);
    setForm({
      day: r.day,
      start: fmtMin(r.start_min),
      end: fmtMin(r.end_min),
      room_code: r.room_code,
      teacher_code: r.teacher_code,
    });
    setMsg("");
    setErr("");
  }

  async function saveEdit(r) {
    setMsg("");
    setErr("");
    try {
      const res = await api.rebindSession(r.code, {
        day: form.day,
        start_min: toMin(form.start),
        end_min: toMin(form.end),
        room_code: form.room_code,
        teacher_code: form.teacher_code,
      });
      setMsg(
        res.mode === "live_rebind"
          ? `已同事务改挂 ${r.code}：定义与 open 租约同步移动，旧资源无幽灵占用`
          : `草稿 ${r.code} 已改点`
      );
      setEditing(null);
      await load();
    } catch (e) {
      setErr(e.message);
      await load();
    }
  }

  return (
    <section className="panel">
      <h2>批窗</h2>
      {msg && <div className="msg">{msg}</div>}
      {err && <div className="msg err">{err}</div>}
      <table>
        <thead>
          <tr>
            <th>批窗</th>
            <th>时段</th>
            <th>算力槽 / 执行器</th>
            <th>就绪</th>
            <th>状态</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <Fragment key={r.code}>
              <tr>
                <td>
                  <div>{r.title}</div>
                  <small>{r.code}</small>
                </td>
                <td>
                  {r.day}
                  <br />
                  {fmtMin(r.start_min)}–{fmtMin(r.end_min)}
                </td>
                <td>
                  {r.room_code}
                  <br />
                  {r.teacher_code}
                </td>
                <td>
                  {r.readiness?.ready ? (
                    <span className="tag live">就绪</span>
                  ) : (
                    <span className="tag bad">
                      {(r.readiness?.reasons || []).join("；") || "未就绪"}
                    </span>
                  )}
                </td>
                <td>
                  <span className={`tag ${r.status}`}>
                    {statusLabel[r.status] || r.status}
                  </span>
                </td>
                <td>
                  <button
                    disabled={r.status !== "draft"}
                    onClick={() => openLease(r.code)}
                  >
                    开窗占用
                  </button>{" "}
                  <button
                    className="secondary"
                    disabled={r.status === "done"}
                    onClick={() =>
                      editing === r.code ? setEditing(null) : startEdit(r)
                    }
                  >
                    {editing === r.code ? "收起" : "改挂"}
                  </button>
                </td>
              </tr>
              {editing === r.code && form && (
                <tr>
                  <td colSpan={6}>
                    <div>
                      {r.status === "live"
                        ? "占用中改挂：定义与 open 租约同事务移动；与他窗重叠则整笔回滚。"
                        : "草稿改点：仅改定义，开窗时按新定义占用。"}
                    </div>
                    <label>
                      日期{" "}
                      <input
                        type="date"
                        value={form.day}
                        onChange={(e) =>
                          setForm({ ...form, day: e.target.value })
                        }
                      />
                    </label>{" "}
                    <label>
                      开始{" "}
                      <input
                        type="time"
                        value={form.start}
                        onChange={(e) =>
                          setForm({ ...form, start: e.target.value })
                        }
                      />
                    </label>{" "}
                    <label>
                      结束{" "}
                      <input
                        type="time"
                        value={form.end}
                        onChange={(e) =>
                          setForm({ ...form, end: e.target.value })
                        }
                      />
                    </label>{" "}
                    <label>
                      算力槽{" "}
                      <select
                        value={form.room_code}
                        onChange={(e) =>
                          setForm({ ...form, room_code: e.target.value })
                        }
                      >
                        {rooms.map((x) => (
                          <option key={x.code} value={x.code}>
                            {x.code}
                          </option>
                        ))}
                      </select>
                    </label>{" "}
                    <label>
                      执行器{" "}
                      <select
                        value={form.teacher_code}
                        onChange={(e) =>
                          setForm({ ...form, teacher_code: e.target.value })
                        }
                      >
                        {teachers.map((x) => (
                          <option key={x.code} value={x.code}>
                            {x.code}
                          </option>
                        ))}
                      </select>
                    </label>{" "}
                    <button onClick={() => saveEdit(r)}>保存改挂</button>{" "}
                    <button
                      className="secondary"
                      onClick={() => setEditing(null)}
                    >
                      取消
                    </button>
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </section>
  );
}
