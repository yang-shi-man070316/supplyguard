import asyncio
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 阶段06板块2: 把项目根加入 sys.path, 使 backend 下运行时也能 import
# analysis/ 下的统计与辅助证据模块 (独立运行这些模块时它们各自也有兜底)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from analysis.rag_store import run_rag_query
from analysis.weather_evidence import run_weather_evidence
from constants import RUN_TYPE
from db import close_conn, get_conn, next_run_id
from gap_check import get_blockers
from mqtt_client import ws_connections, bind_loop, current_snapshot
from plan_store import get_plans_with_results
from review_store import create_review, get_pending_reviews
from run_store_v2 import _capture_snapshot, create_run, get_run, list_runs
from task_execute import run_execute_all
from t1_check import get_task_result

app = FastAPI(title="SupplyGuard‑阶段01 实时数据流")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"status":"ok","stage":"01‑memory‑only"}

@app.get("/state")
async def get_state():
    """调试查看接口：返回当前内存中全部库存、需求、运输状态"""
    return current_snapshot()

@app.get("/api/history/demands")
def history_demands(service_id: str = None):
    """阶段02: 服务点需求历史 (只读, 直接透传 demand_records 原始字段)。

    使用示例:
        GET /api/history/demands              -> 全量, 按 observed_at 倒序
        GET /api/history/demands?service_id=P1 -> 仅 P1 的需求历史
    查询参数全部可选, 无匹配返回 []。"""
    conn = get_conn()
    try:
        if service_id:
            rows = conn.execute(
                "SELECT * FROM demand_records WHERE service_id=? "
                "ORDER BY observed_at DESC, demand_id DESC",
                (service_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM demand_records "
                "ORDER BY observed_at DESC, demand_id DESC"
            ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]

@app.get("/api/history/stocks")
def history_stocks(reserve_id: str = None):
    """阶段02: 储备点库存观测历史 (只读, 直接透传 stock_observations 原始字段)。

    使用示例:
        GET /api/history/stocks             -> 全量, 按 observed_at 倒序
        GET /api/history/stocks?reserve_id=R1 -> 仅 R1 的库存观测历史
    查询参数全部可选, 无匹配返回 []。"""
    conn = get_conn()
    try:
        if reserve_id:
            rows = conn.execute(
                "SELECT * FROM stock_observations WHERE reserve_id=? "
                "ORDER BY observed_at DESC, stock_id DESC",
                (reserve_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stock_observations "
                "ORDER BY observed_at DESC, stock_id DESC"
            ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]

@app.get("/api/history/transports")
def history_transports(transport_id: str = None):
    """阶段02: 运输状态观测历史 (只读, 直接透传 transport_observations 原始字段;
    transport_observations 表无 service_id 列, 过滤参数为 transport_id)。

    使用示例:
        GET /api/history/transports               -> 全量, 按 observed_at 倒序
        GET /api/history/transports?transport_id=V1 -> 仅 V1 的运输状态历史
    查询参数全部可选, 无匹配返回 []。"""
    conn = get_conn()
    try:
        if transport_id:
            rows = conn.execute(
                "SELECT * FROM transport_observations WHERE transport_id=? "
                "ORDER BY observed_at DESC, observation_id DESC",
                (transport_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transport_observations "
                "ORDER BY observed_at DESC, observation_id DESC"
            ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]

@app.get("/api/history/dispatches")
def history_dispatches(from_reserve_id: str = None, to_service_id: str = None):
    """阶段02: 调配记录历史 (只读, 直接透传 dispatch_records 原始字段;
    dispatch_records 表的事件时间列为 dispatched_at, 按它倒序)。

    使用示例:
        GET /api/history/dispatches            -> 全量, 按 dispatched_at 倒序
        GET /api/history/dispatches?from_reserve_id=R1 -> 仅 R1 发出的调配
        GET /api/history/dispatches?from_reserve_id=R1&to_service_id=P1 -> 双条件过滤
    查询参数全部可选, 可单独或组合使用, 无匹配返回 []。"""
    conn = get_conn()
    try:
        conds, params = [], []
        if from_reserve_id:
            conds.append("from_reserve_id=?")
            params.append(from_reserve_id)
        if to_service_id:
            conds.append("to_service_id=?")
            params.append(to_service_id)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = conn.execute(
            "SELECT * FROM dispatch_records" + where +
            " ORDER BY dispatched_at DESC, dispatch_id DESC",
            params,
        ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]

@app.get("/api/history/receipts")
def history_receipts(service_id: str = None):
    """阶段02: 接收记录历史 (只读, 直接透传 receipt_records_v2 原始字段;
    旧 receipt_records 表原样保留不动)。

    使用示例:
        GET /api/history/receipts              -> 全量, 按 observed_at 倒序
        GET /api/history/receipts?service_id=P1 -> 仅 P1 的接收记录历史
    查询参数全部可选, 无匹配返回 []。"""
    conn = get_conn()
    try:
        if service_id:
            rows = conn.execute(
                "SELECT * FROM receipt_records_v2 WHERE service_id=? "
                "ORDER BY observed_at DESC, id DESC",
                (service_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM receipt_records_v2 ORDER BY observed_at DESC, id DESC"
            ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]

@app.get("/api/evidences")
def list_evidences(evidence_type: str = None, source_id: str = None):
    """阶段02: 统一证据表查询 (只读, 直接透传 evidence_v2 原始字段;
    旧 evidence 表为历史遗留, 原样保留不动, 本接口不读取)。

    使用示例:
        GET /api/evidences                          -> 全量证据, 按 observed_at 倒序
        GET /api/evidences?evidence_type=dispatch   -> 仅调配类证据
        GET /api/evidences?source_id=RCT-2026-001   -> 仅该来源业务记录的证据
    查询参数全部可选, 可单独或组合使用, 无匹配返回 []。"""
    conn = get_conn()
    try:
        conds, params = [], []
        if evidence_type:
            conds.append("evidence_type=?")
            params.append(evidence_type)
        if source_id:
            conds.append("source_id=?")
            params.append(source_id)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = conn.execute(
            "SELECT * FROM evidence_v2" + where +
            " ORDER BY observed_at DESC, created_at DESC, evidence_id DESC",
            params,
        ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]

# ============================================================
# 阶段03板块2: run 生命周期接口 (reality run + 快照冻结)
# 只做 run 创建/查询与快照抓取冻结, 不做任何 T1~T5 验算逻辑
# (execute 验算接口由板块3 提供, 见下方)。
# ============================================================

class RunCreate(BaseModel):
    """POST /api/runs 入参。"""
    scenario_id: str
    parent_run_id: str = None
    run_type: str = RUN_TYPE


@app.post("/api/runs", status_code=201)
def api_create_run(body: RunCreate):
    """创建 S1 reality run。

    - run_id 自动生成 (BB03-RUN-001/002/... 自增)
    - 抓取当前时刻服务点/物资/需求/接收数据冻结进 input_snapshot_json
    - assumptions_json 初始为空 JSON 对象 {}
    - run_status=pending; parent_run_id 默认 null; created_at 自动写入
    """
    if not body.scenario_id.strip():
        raise HTTPException(status_code=400, detail="scenario_id required")
    if body.run_type != RUN_TYPE:
        raise HTTPException(status_code=400,
                            detail="only run_type=%r supported" % RUN_TYPE)
    ok, msg, run = create_run(body.scenario_id, body.parent_run_id, body.run_type)
    if not ok:
        raise HTTPException(status_code=500, detail=msg)
    return run


@app.get("/api/runs")
def api_list_runs():
    """run 列表: run_id / scenario_id / run_type / parent_run_id /
    run_status / created_at / completed_at (不含快照列)。"""
    return list_runs()


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str):
    """单 run 完整详情 (含 input_snapshot_json / assumptions_json);
    run_id 不存在返回 404。"""
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run %s not found" % run_id)
    return run


# ============================================================
# 阶段03板块3: T1 execute 验算接口 + 任务结果查询
# 只实现 T1 校验闭环, 不处理 T2~T5。
# ============================================================

@app.post("/api/runs/{run_id}/execute")
def api_execute_run(run_id: str):
    """执行 T1~T5 全套验算 (板块4): 读 run 快照 → 串行执行 T1、T2、T3、T4、T5
    → 每个任务独立写 dependency_checks + task_results
    → 全部完成后 run 置 completed (写入 completed_at)。

    - run_id 不存在 → 404; 已完成(completed)的 run → 409 拒绝执行
    - 整套执行同一事务; 完成后 run 快照冻结, 禁止任何修改 (触发器兜底)
    """
    ok, http_status, msg, data = run_execute_all(run_id)
    if not ok:
        raise HTTPException(status_code=http_status, detail=msg)
    return data


@app.get("/api/runs/{run_id}/task-result")
def api_get_task_result(run_id: str, task_id: str = "T1"):
    """查询任务结果: T1 任务结果 + 该 run 下全部依赖检查明细
    (含 evidence / reason)。未执行时 task_result 为 null、明细为空数组。

    - run_id 不存在 → 404; 板块3 只支持 task_id=T1 (其余任务板块4 实现)
    """
    ok, http_status, msg, data = get_task_result(run_id, task_id)
    if not ok:
        raise HTTPException(status_code=http_status, detail=msg)
    return data


# ============================================================
# 阶段04板块4: Blockers 缺口查询 + 候选方案评估查询 (只读接口)
# - blockers 来自板块1 gap_check 落库 (含 gap 数值与缺口描述文本)
# - plans 合并板块2 plan_definitions 基础信息 + 板块3 plan_results
#   判定结果; 未判定方案 status 为 null
# ============================================================

@app.get("/api/runs/{run_id}/blockers")
def api_get_blockers(run_id: str):
    """当前 run 全部 blocker 缺口记录: gap 数值 + 缺口描述文本
    (reason_json.summary)。run 不存在 → 404。"""
    ok, http_status, msg, data = get_blockers(run_id)
    if not ok:
        raise HTTPException(status_code=http_status, detail=msg)
    return data


@app.get("/api/runs/{run_id}/plans")
def api_get_plans(run_id: str):
    """合并返回候选方案基础信息 + 可行性判定结果: 每条方案携带方案标识
    (plan_name)、status (executable / non_executable, 未判定 null)、
    结构化 reason_json。run 不存在 → 404。"""
    ok, http_status, msg, data = get_plans_with_results(run_id)
    if not ok:
        raise HTTPException(status_code=http_status, detail=msg)
    return data


# ============================================================
# 阶段05板块2: 复核业务 REST 接口 (新增, 原有接口全部保留不修改)
# - POST /api/reviews                     创建待复核 review 请求
# - GET  /api/reviews/pending             全部 pending 复核单 (小程序拉取)
# - POST /api/reviews/{review_id}/receipt 签收提交 (核心接口)
# ============================================================

class ReviewCreate(BaseModel):
    """POST /api/reviews 入参。"""
    dispatch_id: str
    service_id: str
    material_id: str
    trigger_run_id: str


class ReviewReceiptSubmit(BaseModel):
    """POST /api/reviews/{review_id}/receipt 入参。"""
    receipt_id: str
    dispatch_id: str
    service_id: str
    material_id: str
    received_quantity: int
    observed_at: str
    source_type: str = "API"
    note: Optional[str] = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _submit_review_receipt(review_id: str, body: ReviewReceiptSubmit) -> tuple:
    """签收提交完整动作编排 (供 POST /api/reviews/{review_id}/receipt)。

    按文档规则顺序:
      ① receipt_id 全局唯一: 已存在 → 409 (禁止重复累加, 幂等)
      ② review 必须处于 pending: 不存在 → 404; 已 completed → 409
      ③ dispatch_id/service_id/material_id 必须与 review 记录一致 → 400
      ④ 签收成功完整动作 (同一事务, 原子):
         - review 标记 completed (回填 completed_at)
         - 签收记录写入 receipt_records_v2
         - note (可选) 写入 evidence_v2 证据层
         - 基于 trigger_run 快照新建一条全新 reality run
           (parent_run_id=trigger_run_id; 原始 trigger_run 只读, 绝不修改;
           新 run 快照复用 run_store_v2._capture_snapshot 口径在同一事务
           抓取, 天然包含刚写入的签收记录)
         - 事务提交后对新 run 执行 run_execute_all (完整 T1~T5 验算)
    返回 (ok, http_status, msg, data)。
    """
    # ---- 400: 入参合法性 ----
    for name, value in (("receipt_id", body.receipt_id),
                        ("dispatch_id", body.dispatch_id),
                        ("service_id", body.service_id),
                        ("material_id", body.material_id),
                        ("observed_at", body.observed_at)):
        if value is None or not str(value).strip():
            return False, 400, "%s required" % name, None
    if (isinstance(body.received_quantity, bool)
            or not isinstance(body.received_quantity, int)
            or body.received_quantity < 0):
        return False, 400, "received_quantity must be a non-negative integer", None
    if body.source_type is None or not str(body.source_type).strip():
        return False, 400, "source_type required", None
    if body.note is not None and (not isinstance(body.note, str)
                                  or len(body.note) > 500):
        return False, 400, "note must be a string with at most 500 chars", None

    receipt_id = str(body.receipt_id).strip()
    dispatch_id = str(body.dispatch_id).strip()
    service_id = str(body.service_id).strip()
    material_id = str(body.material_id).strip()
    observed_at = str(body.observed_at).strip()
    source_type = str(body.source_type).strip()
    note = body.note.strip() if body.note and body.note.strip() else None
    review_id = str(review_id).strip()

    # ---- ① 快速幂等预检 (事务内再查兜底并发) ----
    conn = get_conn()
    try:
        dup = conn.execute(
            "SELECT 1 FROM receipt_records_v2 WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
    finally:
        close_conn(conn)
    if dup is not None:
        return False, 409, (
            "receipt %s already exists (幂等, 禁止重复累加签收数量)"
            % receipt_id), None

    # ---- ②③④ 主事务 (run_id 撞主键时整体重试, 最多 3 次) ----
    last_run_id_err = None
    for _ in range(3):
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM review_requests WHERE review_id=?",
                (review_id,),
            ).fetchone()
            if row is None:
                return False, 404, "review %s not found" % review_id, None
            # ② pending 状态闸门
            if row["status"] != "pending":
                return False, 409, (
                    "review %s is %s, receipt submit rejected"
                    % (review_id, row["status"])), None
            # ③ 三元组一致性
            if (row["dispatch_id"] != dispatch_id
                    or row["service_id"] != service_id
                    or row["material_id"] != material_id):
                return False, 400, (
                    "request (dispatch_id=%s, service_id=%s, material_id=%s) "
                    "does not match review %s"
                    % (dispatch_id, service_id, material_id, review_id)), None
            # ① 并发兜底: 同 receipt_id 已存在 → 409
            if conn.execute(
                "SELECT 1 FROM receipt_records_v2 WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone() is not None:
                return False, 409, (
                    "receipt %s already exists (幂等, 禁止重复累加签收数量)"
                    % receipt_id), None

            # ---- ④ 完整动作 (同一事务) ----
            conn.execute(
                "INSERT INTO receipt_records_v2 "
                "(receipt_id, dispatch_id, service_id, material_id, "
                "received_quantity, observed_at, source_type) "
                "VALUES (?,?,?,?,?,?,?)",
                (receipt_id, dispatch_id, service_id, material_id,
                 body.received_quantity, observed_at, source_type),
            )
            completed_at = _now()
            cur = conn.execute(
                "UPDATE review_requests SET status='completed', completed_at=? "
                "WHERE review_id=? AND status='pending'",
                (completed_at, review_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False, 409, (
                    "review %s is no longer pending, receipt submit rejected"
                    % review_id), None
            evidence_id = None
            if note is not None:
                evidence_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO evidence_v2 "
                    "(evidence_id, evidence_type, source_id, status, "
                    "value_json, observed_at) VALUES (?,?,?,?,?,?)",
                    (evidence_id, "receipt", receipt_id, "ok",
                     json.dumps({"note": note}, ensure_ascii=False),
                     observed_at),
                )
            # 原始 trigger_run 只读: 仅读取 scenario_id, 绝不修改
            trun = conn.execute(
                "SELECT scenario_id FROM runs WHERE run_id=?",
                (row["trigger_run_id"],),
            ).fetchone()
            if trun is None:
                conn.rollback()
                return False, 500, (
                    "review %s references missing run %s"
                    % (review_id, row["trigger_run_id"])), None
            # 新建全新 reality run: 快照同一事务抓取, 含刚写入的签收记录
            new_run_id = next_run_id(conn)
            snapshot = _capture_snapshot(conn)
            conn.execute(
                "INSERT INTO runs "
                "(run_id, scenario_id, run_type, parent_run_id, run_status, "
                "input_snapshot_json, assumptions_json) VALUES (?,?,?,?,?,?,?)",
                (new_run_id, trun["scenario_id"], RUN_TYPE,
                 row["trigger_run_id"], "pending",
                 json.dumps(snapshot, ensure_ascii=False), "{}"),
            )
            conn.commit()
            break
        except sqlite3.IntegrityError as exc:
            last_run_id_err = exc
            conn.rollback()
            # receipt UNIQUE 并发冲突 → 409; 否则 (run_id 撞主键) → 重试
            c2 = get_conn()
            try:
                exists = c2.execute(
                    "SELECT 1 FROM receipt_records_v2 WHERE receipt_id=?",
                    (receipt_id,),
                ).fetchone()
            finally:
                close_conn(c2)
            if exists is not None:
                return False, 409, (
                    "receipt %s already exists (并发幂等, 禁止重复累加签收数量)"
                    % receipt_id), None
        except sqlite3.Error as exc:
            conn.rollback()
            return False, 500, "db error: %s" % exc, None
        finally:
            close_conn(conn)
    else:
        return False, 500, (
            "new run creation failed after 3 retries: %s" % last_run_id_err), None

    # ---- 对新 run 自动执行完整 T1~T5 验算 ----
    ok_e, http_status_e, msg_e, data_e = run_execute_all(new_run_id)
    if not ok_e:
        return False, 500, (
            "receipt submitted and review completed, but T1~T5 execute "
            "failed on new run %s: %s" % (new_run_id, msg_e)), {
            "receipt_id": receipt_id,
            "review_id": review_id,
            "review_status": "completed",
            "note_evidence_id": evidence_id,
            "new_run_id": new_run_id,
        }
    return True, 200, (
        "receipt submitted, review completed, new run %s executed"
        % new_run_id), {
        "receipt_id": receipt_id,
        "review_id": review_id,
        "review_status": "completed",
        "note_evidence_id": evidence_id,
        "new_run_id": new_run_id,
        "execute": data_e,
    }


@app.post("/api/reviews")
def api_create_review(body: ReviewCreate, response: Response):
    """创建待复核 review 请求 (阶段05板块2)。

    - 入参: dispatch_id / service_id / material_id / trigger_run_id
    - 返回: review_id; 同三元组已存在 pending 单时幂等返回原 review_id
    - 必填缺失 → 400; trigger_run_id 不存在 → 404
    """
    ok, http_status, msg, data = create_review(
        body.dispatch_id, body.trigger_run_id,
        body.service_id, body.material_id)
    if not ok:
        raise HTTPException(status_code=http_status, detail=msg)
    response.status_code = 201 if data["created"] else 200
    return {"review_id": data["review_id"], "created": data["created"]}


@app.get("/api/reviews/pending")
def api_pending_reviews():
    """全部 pending 待复核列表 (供小程序拉取, 阶段05板块2)。

    返回字段严格对齐文档: review_id / service_id / material_id /
    dispatch_id / trigger_run_id / created_at。
    """
    ok, http_status, msg, data = get_pending_reviews()
    if not ok:
        raise HTTPException(status_code=http_status, detail=msg)
    fields = ("review_id", "service_id", "material_id",
              "dispatch_id", "trigger_run_id", "created_at")
    reviews = [{k: r[k] for k in fields} for r in data["reviews"]]
    return {"review_count": len(reviews), "reviews": reviews}


@app.post("/api/reviews/{review_id}/receipt")
def api_submit_review_receipt(review_id: str, body: ReviewReceiptSubmit):
    """签收提交 (阶段05板块2 核心接口)。

    - receipt_id 重复 → 409 (幂等, 禁止重复累加签收数量)
    - review 不存在 → 404; 已 completed → 409 (拒绝签收)
    - 三元组与 review 记录不一致 → 400
    - 成功: review → completed + 签收落 receipt_records_v2 + 新建
      reality run (原 trigger_run 只读) + 自动执行完整 T1~T5 验算。
    """
    ok, http_status, msg, data = _submit_review_receipt(review_id, body)
    if not ok:
        raise HTTPException(status_code=http_status, detail=msg)
    return data


# ============================================================
# 阶段06板块2: 外部辅助证据接口 (天气 / RAG 检索, 只新增不改动)
# - GET /api/weather  抓取实时天气, API 失败降级 status='unavailable'
# - GET /api/rag?q=   保障知识库检索, 无匹配返回固定文本 "无可靠依据"
# 两个接口的结果均写入 evidence_v2 辅助证据 (evidence_type 新增
# weather/rag), 不参与也不修改 T1~T4 任何判定逻辑。
# ============================================================

@app.get("/api/weather")
def api_get_weather():
    """天气辅助证据查询 (阶段06板块2)。

    - 调用天气 Web API (wttr.in, 超时 8s)
    - API 不可用/超时/返回结构异常 → HTTP 200, 返回体 status='unavailable'
      并携带 error_message (降级语义: 不抛 5xx, 不影响 T1~T4 判定)
    - 结果写入 evidence_v2 (evidence_type='weather',
      evidence status 与返回体 status 一致)
    """
    ok, http_status, msg, data = run_weather_evidence()
    if not ok:
        raise HTTPException(status_code=http_status, detail=msg)
    return data


@app.get("/api/rag")
def api_rag_query(q: str):
    """RAG 保障知识库检索 (阶段06板块2)。

    - 检索 rag_docs/ 知识文档, 返回 answer 与 citations 来源引用
    - 无匹配 → answer 固定返回 "无可靠依据", citations 为空列表
    - 结果写入 evidence_v2 (evidence_type='rag', status='ok',
      matched 标记在 value_json 中; 无结果 ≠ 证据不可用)
    - 查询参数 q 缺失/空白 → 400
    """
    ok, http_status, msg, data = run_rag_query(q)
    if not ok:
        raise HTTPException(status_code=http_status, detail=msg)
    return data


@app.websocket("/ws/guarantee")
async def ws_guarantee(websocket: WebSocket):
    bind_loop(asyncio.get_running_loop())
    await websocket.accept()
    ws_connections.add(websocket)
    # 新连接首推当前快照，不用等下一条MQTT消息
    snapshot = current_snapshot()
    if snapshot["stock"] or snapshot["demand"] or snapshot["transport"]:
        try:
            await websocket.send_json(snapshot)
        except Exception:
            pass
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_connections.discard(websocket)
