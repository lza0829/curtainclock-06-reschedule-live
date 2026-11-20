# Curtainclock（幕窗钟）

骨架：**批窗定义 → 算力槽/执行器资源依赖就绪 → 开窗租约占用 → 关窗释放与冲突水位互证**

- 前端：http://127.0.0.1:6100 （React + Vite）
- API：http://127.0.0.1:11100

## 本地冒烟（推荐）

```powershell
cd D:\Asolo11\claude-workspace\curtainclock\source
# 本机若无 3.11：py -V:Astral/CPython3.11.15 -m venv .venv
py -V:Astral/CPython3.11.15 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
$env:PYTHONPATH = "$PWD\backend"
Start-Process .\.venv\Scripts\uvicorn.exe -ArgumentList 'app.main:app','--host','127.0.0.1','--port','11100' -WorkingDirectory "$PWD\backend"
cd frontend
# 前端需 Node ≥18；不足时用 docker compose 起 web
npm install
npm run dev
```

浏览器打开 http://127.0.0.1:6100 ：开一节课 → 同算力槽冲突 → 关窗释放。

## 改挂架构（全局唯一：同事务改挂臂）

占用中（live）批窗允许改时段 / 改挂算力槽 / 执行器，口径为**定义与租约同事务改挂**：

- 改挂入口唯一：`POST /api/sessions/{code}/rebind`（draft 仅改定义；live 同事务改挂；done 封存禁改）。
- live 改挂时，批窗定义行与 open 租约行在**同一事务**内同步移动时段与资源码，占用索引（leases 行）随资源码更新，不留旧资源幽灵占用。
- 与他窗 open 租约重叠或未就绪：整笔回滚、不改库；冲突尝试另开事务落冲突桶并抬水位（与开窗共用 `record_conflicts`，同源）。
- 就绪/重叠预检与开窗同源（`readiness`，排除本窗自身租约）；`BEGIN IMMEDIATE` 事务使并发改挂与邻窗开窗串行，不得双占。
- 批窗页、租约页、总览、冲突桶均直读同一库表，数字同源可核。

核对：`python -I _verify_rebind.py`（服务层+并发，临时库）；`python _smoke_api.py`（HTTP 全链路，需先起服，可用 `CURTAINCLOCK_DB=$(mktemp)` 指定一次性临时库）。

## Docker

```powershell
docker compose up -d --build
```
