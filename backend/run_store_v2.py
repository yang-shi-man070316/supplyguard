"""阶段03板块2: run 生命周期数据层 (新 runs 表, 板块1文档字段).

职责:
    - create_run: 自动生成 BB03-RUN-xxx, 抓取当前时刻快照并冻结入库
    - list_runs / get_run: run 查询
不做任何 T1~T5 验算逻辑 (板块2 禁止)。

快照冻结规则:
    - input_snapshot_json 在 create 时一次性写入「当前时刻」数据:
      服务点 / 物资 / 需求(每个服务点×物资最新一条) / 接收(全部签收记录)
      / 库存(每个储备点×物资最新一条) / 运输(每个运输资源最新一条)
      (板块4 起扩展库存/运输与各条目 created_at, 供 T2~T5 只读校验)
    - assumptions_json 初始为空 JSON 对象 {}
    - run_status=pending; created_at 由表默认填充
    - completed 之后快照禁止修改、禁止覆盖: 由板块1触发器
      trg_runs_completed_snapshot_immutable 强制, 本层不提供任何更新接口。

阶段07板块1 what-if 扩展 (只增不改, reality 行为完全不变):
    - run_type 支持 reality (默认, 原行为) 与 what-if;
    - what-if run: parent_run_id 必填且必须指向 reality run; 创建时深拷贝
      parent 的 input_snapshot_json 作为自身快照, 再用 apply_assumptions
      把假设修改叠加到「副本」上; assumptions_json 存原始假设;
    - 假设修改绝不写回 parent (本层对 parent 只 SELECT, 不 UPDATE/INSERT);
      子 run 完成后其自身快照同样被触发器冻结 (只增不改);
    - runs / task_results / dependency_checks 三表隔离规则不变:
      所有判定结果均以 run_id 区分, what-if 不触碰 parent 的任何判定行。
"""

import copy
import json
import sqlite3
from datetime import datetime

from constants import RUN_TYPE, RUN_TYPES, RUN_TYPE_WHATIF
from db import close_conn, get_conn, next_run_id

_RUN_ID_RETRY = 3  # run_id 撞主键 (并发创建) 时的重试次数

# what-if 假设 key 语法 (阶段07板块1):
#   transport.<transport_id>.<field>    覆盖 transports 条目字段
#   stock.<reserve_id>.<material_id>    覆盖 stocks 条目 quantity
#   remaining_minutes                   覆盖顶层 remaining_time (分钟)
_WHATIF_TRANSPORT_FIELDS = ("availability", "capacity")
_WHATIF_TRANSPORT_STATES = ("AVAILABLE", "IN_TRANSIT", "FAULT")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row) -> dict:
    """run 行转 dict; input_snapshot_json / assumptions_json 解析为对象。"""
    d = dict(row)
    for key in ("input_snapshot_json", "assumptions_json"):
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except json.JSONDecodeError:
                pass  # 防御脏数据: 保留原文
    return d


def _capture_snapshot(conn) -> dict:
    """抓取当前时刻快照 (与创建 run 同连接同事务, 保证口径一致)。

    内容 (板块4 起扩展为全量观测, 供 T1~T5 只读快照校验):
        services    当前出现过的服务点列表 (需求/接收记录 distinct)
        materials   当前出现过的物资列表 (需求/接收/库存 distinct)
        demands    每个 (服务点, 物资) 的最新一条需求观测 (含 created_at)
        receipts   全部接收(签收)记录 (追加型数据, 全量; 含 created_at)
        stocks     每个 (储备点, 物资) 的最新一条库存观测 (含 created_at)
        transports 每个运输资源的最新一条观测 (含 created_at)
    各条目均带 created_at (落库时间), 供 T5 时间线倒挂校验。
    """
    services = [r[0] for r in conn.execute(
        "SELECT DISTINCT service_id FROM demand_records "
        "UNION SELECT DISTINCT service_id FROM receipt_records_v2 ORDER BY 1"
    ).fetchall()]
    materials = [r[0] for r in conn.execute(
        "SELECT DISTINCT material_id FROM demand_records "
        "UNION SELECT DISTINCT material_id FROM receipt_records_v2 "
        "UNION SELECT DISTINCT material_id FROM stock_observations ORDER BY 1"
    ).fetchall()]
    demands = [dict(r) for r in conn.execute(
        "SELECT service_id, material_id, required_quantity, observed_at, created_at "
        "FROM demand_records d "
        "WHERE d.demand_id = ("
        "  SELECT MAX(d2.demand_id) FROM demand_records d2 "
        "  WHERE d2.service_id = d.service_id AND d2.material_id = d.material_id)"
        "ORDER BY service_id, material_id"
    ).fetchall()]
    receipts = [dict(r) for r in conn.execute(
        "SELECT receipt_id, dispatch_id, service_id, material_id, "
        "received_quantity, observed_at, created_at "
        "FROM receipt_records_v2 ORDER BY observed_at, id"
    ).fetchall()]
    stocks = [dict(r) for r in conn.execute(
        "SELECT reserve_id, material_id, quantity, observed_at, created_at "
        "FROM stock_observations s "
        "WHERE s.stock_id = ("
        "  SELECT MAX(s2.stock_id) FROM stock_observations s2 "
        "  WHERE s2.reserve_id = s.reserve_id AND s2.material_id = s.material_id)"
        "ORDER BY reserve_id, material_id"
    ).fetchall()]
    transports = [dict(r) for r in conn.execute(
        "SELECT transport_id, availability, capacity, observed_at, created_at "
        "FROM transport_observations t "
        "WHERE t.observation_id = ("
        "  SELECT MAX(t2.observation_id) FROM transport_observations t2 "
        "  WHERE t2.transport_id = t.transport_id)"
        "ORDER BY transport_id"
    ).fetchall()]
    return {
        "snapshot_at": _now(),
        "services": services,
        "materials": materials,
        "demands": demands,
        "receipts": receipts,
        "stocks": stocks,
        "transports": transports,
    }


def _validate_assumptions(assumptions: dict):
    """假设 key 语法与取值校验 (不依赖快照内容, 入参非法抛 ValueError)。"""
    if not isinstance(assumptions, dict) or not assumptions:
        raise ValueError("assumptions must be a non-empty object")
    for key, value in assumptions.items():
        key = str(key).strip()
        parts = key.split(".")
        if parts[0] == "transport" and len(parts) == 3:
            if parts[2] not in _WHATIF_TRANSPORT_FIELDS:
                raise ValueError(
                    "assumption %r: field must be one of %s"
                    % (key, ", ".join(_WHATIF_TRANSPORT_FIELDS)))
            if parts[2] == "availability" and value not in _WHATIF_TRANSPORT_STATES:
                raise ValueError(
                    "assumption %r: illegal availability %r (expected %s)"
                    % (key, value, "/".join(_WHATIF_TRANSPORT_STATES)))
        elif parts[0] == "stock" and len(parts) == 3:
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value < 0):
                raise ValueError(
                    "assumption %r: quantity must be a non-negative integer"
                    % key)
        elif key == "remaining_minutes":
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value < 0):
                raise ValueError(
                    "assumption %r: minutes must be a non-negative integer"
                    % key)
        else:
            raise ValueError(
                "unsupported assumption key %r (expected transport.<id>.<field>"
                " / stock.<reserve>.<material> / remaining_minutes)" % key)


def apply_assumptions(snapshot: dict, assumptions: dict) -> dict:
    """把假设修改叠加到快照副本上 (阶段07板块1, 纯函数, 不修改入参)。

    先做 key 语法/取值校验 (_validate_assumptions), 再逐条应用到副本:
        "transport.<transport_id>.<field>"
            覆盖 transports 中该资源条目的 field; 目标条目不存在
            → ValueError (不臆造运输资源)。
        "stock.<reserve_id>.<material_id>"
            覆盖 stocks 中该 (储备点, 物资) 条目的 quantity;
            目标条目不存在 → ValueError (不臆造库存)。
        "remaining_minutes"
            覆盖顶层 remaining_time (分钟)。

    返回深拷贝后的新快照 (假设只作用于副本, 原快照与 parent 基线不受影响)。
    非法输入抛 ValueError, 不产生任何修改。
    """
    _validate_assumptions(assumptions)
    out = copy.deepcopy(snapshot) if isinstance(snapshot, dict) else {}
    for key, value in assumptions.items():
        key = str(key).strip()
        parts = key.split(".")
        if parts[0] == "transport":
            transport_id, field = parts[1], parts[2]
            target = next((t for t in (out.get("transports") or [])
                           if t.get("transport_id") == transport_id), None)
            if target is None:
                raise ValueError(
                    "assumption %r: transport %s not found in baseline snapshot"
                    % (key, transport_id))
            target[field] = value
        elif parts[0] == "stock":
            reserve_id, material_id = parts[1], parts[2]
            target = next((s for s in (out.get("stocks") or [])
                           if s.get("reserve_id") == reserve_id
                           and s.get("material_id") == material_id), None)
            if target is None:
                raise ValueError(
                    "assumption %r: stock %s/%s not found in baseline snapshot"
                    % (key, reserve_id, material_id))
            target["quantity"] = value
        else:  # remaining_minutes
            out["remaining_time"] = value
    return out


def create_run(scenario_id: str, parent_run_id: str = None,
               run_type: str = RUN_TYPE, assumptions: dict = None) -> tuple:
    """新建 run 并冻结快照 (reality / what-if 两类)。

    reality (原行为, 完全不变):
        - run_id 自动生成 (BB03-RUN-001/002/... 自增, next_run_id);
          并发创建撞主键时重试 (_RUN_ID_RETRY 次)。
        - input_snapshot_json = 当前时刻快照 (见 _capture_snapshot);
          assumptions_json = {} (空 JSON 对象, 非 NULL)。
        - parent_run_id 默认 NULL; 不接受 assumptions。

    what-if (阶段07板块1, 只增不改):
        - parent_run_id 必填且必须指向 reality run (不存在 → 404 语义,
          非 reality → 400 语义);
        - 同一事务内深拷贝 parent 快照 → apply_assumptions 叠加假设到
          副本 → 副本作为自身 input_snapshot_json; assumptions_json 存
          原始假设; 对 parent 行零写入 (只读 SELECT);
        - 假设仅作用于自身 run 上下文, 绝不污染 parent 基线。

    入参非法抛 ValueError (接口层转 400)。
    返回 (ok, http_status, msg, data); 成功时 data 为完整 run 记录
    (JSON 列已解析)。
    """
    if scenario_id is None or not str(scenario_id).strip():
        raise ValueError("scenario_id required")
    if run_type not in RUN_TYPES:
        raise ValueError("run_type must be one of %s" % ", ".join(RUN_TYPES))
    scenario_id = str(scenario_id).strip()

    if run_type == RUN_TYPE:  # reality: 原行为不变
        if assumptions not in (None, {}):
            raise ValueError("reality run does not accept assumptions")
        if parent_run_id is not None and not str(parent_run_id).strip():
            parent_run_id = None  # 空串按未传处理 (默认 null)
        last_err = None
        for _ in range(_RUN_ID_RETRY):
            conn = get_conn()
            try:
                run_id = next_run_id(conn)
                snapshot = _capture_snapshot(conn)
                conn.execute(
                    "INSERT INTO runs "
                    "(run_id, scenario_id, run_type, parent_run_id, run_status, "
                    "input_snapshot_json, assumptions_json) VALUES (?,?,?,?,?,?,?)",
                    (run_id, scenario_id, run_type, parent_run_id, "pending",
                     json.dumps(snapshot, ensure_ascii=False), "{}"),
                )
                row = conn.execute(
                    "SELECT * FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                conn.commit()
                return True, 201, "created", _row_to_dict(row)
            except sqlite3.IntegrityError as exc:
                last_err = exc
                conn.rollback()  # 撞主键: 换下一个 run_id 重试
            finally:
                close_conn(conn)
        return False, 500, (
            "run_id generation failed after %d retries: %s"
            % (_RUN_ID_RETRY, last_err)), None

    # ---- what-if 分支 (阶段07板块1) ----
    if parent_run_id is None or not str(parent_run_id).strip():
        raise ValueError("what-if run requires parent_run_id")
    if assumptions is None:
        raise ValueError("what-if run requires assumptions")
    _validate_assumptions(assumptions)  # key 语法/取值校验 (400 语义)
    parent_run_id = str(parent_run_id).strip()

    for _ in range(_RUN_ID_RETRY):
        conn = get_conn()
        try:
            parent = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (parent_run_id,)
            ).fetchone()
            if parent is None:
                return False, 404, (
                    "parent run %s not found" % parent_run_id), None
            if parent["run_type"] != RUN_TYPE:
                return False, 400, (
                    "parent run %s is %s, what-if requires a reality baseline"
                    % (parent_run_id, parent["run_type"])), None
            try:
                base = json.loads(parent["input_snapshot_json"])
            except (TypeError, ValueError):
                return False, 500, (
                    "parent run %s input_snapshot_json invalid"
                    % parent_run_id), None
            if not isinstance(base, dict):
                return False, 500, (
                    "parent run %s input_snapshot_json invalid"
                    % parent_run_id), None
            # 深拷贝 parent 快照 + 叠加假设 → 只写自身新行, parent 零写入
            snapshot = apply_assumptions(base, assumptions)
            run_id = next_run_id(conn)
            conn.execute(
                "INSERT INTO runs "
                "(run_id, scenario_id, run_type, parent_run_id, run_status, "
                "input_snapshot_json, assumptions_json) VALUES (?,?,?,?,?,?,?)",
                (run_id, scenario_id, RUN_TYPE_WHATIF, parent_run_id, "pending",
                 json.dumps(snapshot, ensure_ascii=False),
                 json.dumps(assumptions, ensure_ascii=False)),
            )
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            conn.commit()
            return True, 201, "created", _row_to_dict(row)
        except sqlite3.IntegrityError as exc:
            last_err = exc
            conn.rollback()  # 撞主键: 换下一个 run_id 重试
        finally:
            close_conn(conn)
    return False, 500, (
        "run_id generation failed after %d retries: %s"
        % (_RUN_ID_RETRY, last_err)), None


def list_runs() -> list:
    """run 列表 (只含轻量字段, 不含快照), 按 created_at 倒序 (新在前)。

    同秒创建用 run_id 倒序兜底, 保证结果稳定。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT run_id, scenario_id, run_type, parent_run_id, run_status, "
            "created_at, completed_at FROM runs "
            "ORDER BY created_at DESC, run_id DESC"
        ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]


def get_run(run_id) -> dict:
    """单 run 完整详情 (含 input_snapshot_json / assumptions_json, 已解析)。

    任意状态均可读; 不存在返回 None。
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        close_conn(conn)
    return _row_to_dict(row) if row is not None else None
