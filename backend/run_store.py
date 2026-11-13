"""B-B03 独立任务: run 记录保护.

run 存在状态字段 (pending / completed)。核心规则:
    - 查询: 任意状态的 run 均可读
    - 更新: 仅 pending 允许; completed 的 run 拒绝任何修改, 数据库不改动
    - 新增: 不受限制 (默认 pending)

防竞态要点: 更新/完成语句自带 status='pending' 条件, 状态校验与写库在
同一原子 UPDATE 内完成——即使"先查"与"写"之间 run 被 complete, rowcount=0
同样拒绝, 不存在"查了 pending 却覆盖 completed"的窗口。
"""

import json
import sqlite3
from datetime import datetime, timedelta

from db import close_conn, get_conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("payload"):
        try:
            d["payload"] = json.loads(d["payload"])
        except json.JSONDecodeError:
            pass  # 保留原文
    return d


def create_run(run_id: str, payload: dict = None) -> tuple:
    """新增 run (status=pending)。run_id 已存在则拒绝, 不覆盖。"""
    try:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO runs (run_id, status, payload, created_at) VALUES (?,?,?,?)",
                (run_id, "pending",
                 json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                 _now()),
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.IntegrityError:
        return False, "run %s already exists" % run_id
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, "created"


def get_run(run_id: str):
    """查询单个 run (任意状态均可读)。不存在返回 None。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    finally:
        close_conn(conn)
    return _row_to_dict(row) if row is not None else None


def list_runs() -> list:
    """全部 run 列表 (任意状态均可读), 按 created_at 倒序 (新在前)。

    同秒创建的 run 用 rowid 倒序兜底, 保证结果稳定。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
    finally:
        close_conn(conn)
    return [_row_to_dict(r) for r in rows]


def update_run(run_id: str, payload: dict) -> tuple:
    """更新 run 内容: 仅 pending 允许; completed 拒绝且不改动数据库。"""
    try:
        conn = get_conn()
        try:
            row = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return False, "run %s not found" % run_id
            if row["status"] == "completed":
                return False, "run %s is completed, not modifiable" % run_id
            cur = conn.execute(
                "UPDATE runs SET payload=?, updated_at=? WHERE run_id=? AND status='pending'",
                (json.dumps(payload, ensure_ascii=False), _now(), run_id),
            )
            if cur.rowcount == 0:  # 查与写之间被 complete 的竞态兜底
                return False, "run %s is completed, not modifiable" % run_id
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, "updated"


def complete_run(run_id: str) -> tuple:
    """pending → completed 状态转换; 已 completed 或不存在均拒绝。"""
    try:
        conn = get_conn()
        try:
            cur = conn.execute(
                "UPDATE runs SET status='completed', updated_at=? "
                "WHERE run_id=? AND status='pending'",
                (_now(), run_id),
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    if cur.rowcount == 0:
        if get_run(run_id) is None:
            return False, "run %s not found" % run_id
        return False, "run %s is completed, not modifiable" % run_id
    return True, "completed"


# ============================================================
# B-B03 阶段03板块3: 通用依赖校验框架 + T1 (仅 T1, 禁 T2~T5)
# 框架: seed 元数据 → 读证据快照 → 逐依赖判断 → 写 dependency_checks
#       → 汇总写 task_results。依赖判断函数只实现 T1-D1。
# ============================================================

T1_TASK_ID = "T1"
T1_TASK_NAME = "关键服务点最低需求保障"
T1_TASK_DESC = "全部关键服务点当期最低需求均可被当前库存保障"
T1_DEPS = (
    {
        "dep_id": "T1-D1",
        "dep_name": "关键节点库存≥当期最低需求",
        "dep_desc": "每个服务点每种物资的当期最低需求 ≤ 当前全网该物资库存观测总量",
    },
)


def seed_t1_meta() -> tuple:
    """插入 T1 任务与依赖定义。幂等: 重复执行不重复插入、不覆盖已有行。"""
    try:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO task_definitions "
                "(task_id, task_name, task_desc, created_at) VALUES (?,?,?,?)",
                (T1_TASK_ID, T1_TASK_NAME, T1_TASK_DESC, _now()),
            )
            for dep in T1_DEPS:
                conn.execute(
                    "INSERT OR IGNORE INTO dependency_definitions "
                    "(dep_id, task_id, dep_name, dep_desc, created_at) VALUES (?,?,?,?,?)",
                    (dep["dep_id"], T1_TASK_ID, dep["dep_name"], dep["dep_desc"], _now()),
                )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, "seeded"


def _latest_demands(conn) -> list:
    """每个 (service_id, material_id) 的最新一条需求观测 (按最大 demand_id)。"""
    rows = conn.execute(
        "SELECT service_id, material_id, required_quantity, observed_at "
        "FROM demand_records d "
        "WHERE d.demand_id = ("
        "  SELECT MAX(d2.demand_id) FROM demand_records d2 "
        "  WHERE d2.service_id = d.service_id AND d2.material_id = d.material_id)"
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_stock_totals(conn) -> dict:
    """每个 (reserve_id, material_id) 最新库存观测, 按物资全网求和。"""
    rows = conn.execute(
        "SELECT material_id, quantity FROM stock_observations s "
        "WHERE s.stock_id = ("
        "  SELECT MAX(s2.stock_id) FROM stock_observations s2 "
        "  WHERE s2.reserve_id = s.reserve_id AND s2.material_id = s.material_id)"
    ).fetchall()
    totals = {}
    for r in rows:
        totals[r["material_id"]] = totals.get(r["material_id"], 0) + r["quantity"]
    return totals


def _check_dep_t1d1(conn) -> tuple:
    """T1-D1 判断: 每个服务点每种物资的当期需求 ≤ 全网该物资库存。

    返回 (status, msg), status ∈ satisfied / unsatisfied / unknown。
    无任何需求观测判 unknown; 某物资无库存观测按库存 0 计 (与阶段02
    缺口语义一致: 缺口 = 需求 − 全网该物资库存)。
    """
    demands = _latest_demands(conn)
    if not demands:
        return "unknown", "无服务点需求观测记录, 无法判断"
    stocks = _latest_stock_totals(conn)
    shortfalls = []
    ok_count = 0
    for d in demands:
        total = stocks.get(d["material_id"], 0)
        if total >= d["required_quantity"]:
            ok_count += 1
        else:
            shortfalls.append(
                "%s %s 缺口%d (需求%d, 库存%d)"
                % (d["service_id"], d["material_id"],
                   d["required_quantity"] - total, d["required_quantity"], total)
            )
    if shortfalls:
        return "unsatisfied", "; ".join(shortfalls)
    return "satisfied", "全部%d项需求均满足 (关键节点库存≥当期最低需求)" % ok_count


# 依赖校验分发表: dep_id → 判断函数。T2~T5 的依赖不在此实现。
_T1_DEP_CHECKERS = {
    "T1-D1": _check_dep_t1d1,
}


def run_t1_check(run_id: str) -> tuple:
    """执行 T1 全部依赖校验, 结果落库 dependency_checks + task_results。

    - 仅 status=pending 的 run 允许执行; completed / 不存在直接拒绝。
    - 落库用 INSERT...SELECT FROM runs WHERE status='pending' 原子控制:
      预检与写库之间 run 被 complete 时 rowcount=0, 整体 rollback, 不写任何数据。
    - 同一 run 重复执行时覆盖旧结果 (每 run 每依赖/每任务只保留最新一次)。

    返回 (ok, msg, data); data = {task_id, task_status, deps:[{dep_id,status,check_msg}]}
    """
    seed_t1_meta()  # 幂等保底: 元数据缺失时补种
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT status FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False, "run %s not found" % run_id, None
            if row["status"] != "pending":
                return False, "run %s is completed, not modifiable" % run_id, None

            deps = conn.execute(
                "SELECT dep_id FROM dependency_definitions WHERE task_id=? ORDER BY dep_id",
                (T1_TASK_ID,),
            ).fetchall()
            if not deps:
                return False, "task %s metadata missing" % T1_TASK_ID, None

            dep_results = []
            for d in deps:
                dep_id = d["dep_id"]
                checker = _T1_DEP_CHECKERS.get(dep_id)
                if checker is None:
                    dep_results.append({
                        "dep_id": dep_id, "status": "unknown",
                        "check_msg": "依赖 %s 的判断逻辑未实现" % dep_id,
                    })
                else:
                    status, check_msg = checker(conn)
                    dep_results.append(
                        {"dep_id": dep_id, "status": status, "check_msg": check_msg}
                    )

            if any(r["status"] == "unsatisfied" for r in dep_results):
                task_status = "blocked"
            elif all(r["status"] == "satisfied" for r in dep_results):
                task_status = "established"
            else:
                task_status = "unknown"
            task_msg = "; ".join(
                "%s:%s" % (r["dep_id"], r["status"]) for r in dep_results
            )

            # ---- 原子落库: 写库瞬间要求 run 仍为 pending, 否则整体回滚 ----
            new_check_ids = []  # (dep_id, 新行 check_id), 用于覆盖旧结果
            for r in dep_results:
                cur = conn.execute(
                    "INSERT INTO dependency_checks "
                    "(run_id, dep_id, status, check_msg, created_at) "
                    "SELECT ?,?,?,?,? FROM runs WHERE run_id=? AND status='pending'",
                    (run_id, r["dep_id"], r["status"], r["check_msg"], _now(), run_id),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return False, "run %s is completed, not modifiable" % run_id, None
                new_check_ids.append((r["dep_id"], cur.lastrowid))
            cur_results = conn.execute(
                "INSERT INTO task_results (run_id, task_id, status, result_msg, created_at) "
                "SELECT ?,?,?,?,? FROM runs WHERE run_id=? AND status='pending'",
                (run_id, T1_TASK_ID, task_status, task_msg, _now(), run_id),
            )
            if cur_results.rowcount == 0:
                conn.rollback()
                return False, "run %s is completed, not modifiable" % run_id, None
            # 覆盖旧结果: 同一 run 的同一依赖/任务只保留本次新写入的行
            for dep_id, check_id in new_check_ids:
                conn.execute(
                    "DELETE FROM dependency_checks "
                    "WHERE run_id=? AND dep_id=? AND check_id<?",
                    (run_id, dep_id, check_id),
                )
            conn.execute(
                "DELETE FROM task_results WHERE run_id=? AND task_id=? AND result_id<?",
                (run_id, T1_TASK_ID, cur_results.lastrowid),
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc, None
    return True, "checked", {
        "run_id": run_id,
        "task_id": T1_TASK_ID,
        "task_status": task_status,
        "deps": dep_results,
    }


# ============================================================
# B-B03 阶段03板块4: T2~T5 依赖判断 + 全套校验执行器
# (板块3的 T1 代码一律不动; 本块复用 _check_dep_t1d1/_latest_demands
#  与板块3的原子落库模式。禁止 what-if / plan 推演逻辑。)
# ============================================================

T2T5_TASKS = (
    {
        "task_id": "T2",
        "task_name": "运输链路可达保障",
        "task_desc": "关键物资存在可用运输通道",
        "deps": (
            {
                "dep_id": "T2-D1",
                "dep_name": "物资存在可用运输通道",
                "dep_desc": "至少存在一个最新状态为 AVAILABLE/IN_TRANSIT 的运输资源",
            },
        ),
    },
    {
        "task_id": "T3",
        "task_name": "调度响应时效保障",
        "task_desc": "调度计划可以在需求时限前抵达",
        "deps": (
            {
                "dep_id": "T3-D1",
                "dep_name": "调度可在需求时限前送达",
                "dep_desc": "每项最新需求提出后存在运输/签收活动响应 "
                            "(库中无调度计划证据, 以现有观测做响应性代理判断)",
            },
        ),
    },
    {
        "task_id": "T4",
        "task_name": "入库签收合规保障",
        "task_desc": "在途物资签收记录可闭环核验",
        "deps": (
            {
                "dep_id": "T4-D1",
                "dep_name": "在途物资签收记录可闭环核验",
                "dep_desc": "每条签收记录可对应到同储备点同物资的库存观测; "
                            "有在途运输时必须有签收记录",
            },
        ),
    },
    {
        "task_id": "T5",
        "task_name": "证据一致性校验",
        "task_desc": "库存、运输、签收、需求观测之间数据不自相矛盾",
        "deps": (
            {
                "dep_id": "T5-D1",
                "dep_name": "多源观测数据无冲突",
                "dep_desc": "无负值矛盾、无非法状态值、观测时间不晚于落库时间(容差内)",
            },
        ),
    },
)


def seed_t2t5_meta() -> tuple:
    """插入 T2~T5 任务与依赖定义。幂等: 重复执行不重复插入、不覆盖已有行。"""
    try:
        conn = get_conn()
        try:
            for task in T2T5_TASKS:
                conn.execute(
                    "INSERT OR IGNORE INTO task_definitions "
                    "(task_id, task_name, task_desc, created_at) VALUES (?,?,?,?)",
                    (task["task_id"], task["task_name"], task["task_desc"], _now()),
                )
                for dep in task["deps"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO dependency_definitions "
                        "(dep_id, task_id, dep_name, dep_desc, created_at) VALUES (?,?,?,?,?)",
                        (dep["dep_id"], task["task_id"], dep["dep_name"],
                         dep["dep_desc"], _now()),
                    )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, "seeded"


def _parse_ts(value):
    """解析观测时间字符串 (兼容 ISO 'T' 与空格分隔), 失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _check_dep_t2d1(conn) -> tuple:
    """T2-D1: 至少存在一个最新状态 AVAILABLE/IN_TRANSIT 的运输资源。

    全部 FAULT → unsatisfied; 无运输观测 → unknown。
    """
    rows = conn.execute(
        "SELECT transport_id, availability FROM transport_observations t "
        "WHERE t.observation_id = ("
        "  SELECT MAX(t2.observation_id) FROM transport_observations t2 "
        "  WHERE t2.transport_id = t.transport_id)"
    ).fetchall()
    if not rows:
        return "unknown", "无运输资源观测记录, 无法判断"
    usable = [r["transport_id"] for r in rows
              if r["availability"] in ("AVAILABLE", "IN_TRANSIT")]
    faulty = [r["transport_id"] for r in rows if r["availability"] == "FAULT"]
    others = [r["transport_id"] for r in rows
              if r["availability"] not in ("AVAILABLE", "IN_TRANSIT", "FAULT")]
    if usable:
        msg = "存在可用运输通道: %s" % ", ".join(usable)
        if faulty:
            msg += "; 故障: %s" % ", ".join(faulty)
        return "satisfied", msg
    if faulty:
        return "unsatisfied", "全部运输资源均 FAULT, 无可用运输通道: %s" % ", ".join(faulty)
    return "unknown", "运输资源状态值无法识别: %s" % ", ".join(others)


def _check_dep_t3d1(conn) -> tuple:
    """T3-D1: 每项最新需求提出后, 存在运输观测/签收活动响应。

    库中无调度计划(dispatch)证据表且约束禁止新增表, 以"需求观测时间之后
    存在运输或签收活动"作为调度响应时效的代理判断, 不做 what-if 推演;
    需求时间不可解析判 unknown。
    """
    demands = _latest_demands(conn)  # 复用板块3函数
    if not demands:
        return "unknown", "无服务点需求观测记录, 无法判断"
    activity_ts = []
    for row in conn.execute("SELECT observed_at FROM transport_observations"):
        t = _parse_ts(row["observed_at"])
        if t is not None:
            activity_ts.append(t)
    for row in conn.execute("SELECT received_at FROM receipt_records"):
        t = _parse_ts(row["received_at"])
        if t is not None:
            activity_ts.append(t)
    unverified, lagging = [], []
    for d in demands:
        t = _parse_ts(d["observed_at"])
        if t is None:
            unverified.append("%s %s" % (d["service_id"], d["material_id"]))
            continue
        if not any(a >= t for a in activity_ts):
            lagging.append(
                "%s %s (需求 %s)" % (d["service_id"], d["material_id"], d["observed_at"])
            )
    if lagging:
        return "unsatisfied", ("以下需求提出后无任何运输/签收活动响应: "
                               + "; ".join(lagging))
    if unverified:
        return "unknown", "以下需求时间不可解析, 无法核验时效: " + "; ".join(unverified)
    return "satisfied", "全部%d项需求提出后均有运输/签收活动响应" % len(demands)


def _check_dep_t4d1(conn) -> tuple:
    """T4-D1: 每条签收记录可对应到同储备点同物资的库存观测 (闭环核验);
    存在在途运输时必须有签收记录支撑。"""
    receipts = conn.execute(
        "SELECT receipt_id, reserve_id, material_id FROM receipt_records"
    ).fetchall()
    in_transit = conn.execute(
        "SELECT COUNT(*) c FROM transport_observations t "
        "WHERE t.observation_id = ("
        "  SELECT MAX(t2.observation_id) FROM transport_observations t2 "
        "  WHERE t2.transport_id = t.transport_id) AND t.availability='IN_TRANSIT'"
    ).fetchone()["c"]
    if not receipts:
        if in_transit:
            return "unsatisfied", ("存在%d个在途运输资源但无任何签收记录, "
                                   "无法闭环核验" % in_transit)
        return "unknown", "无签收记录且无在途运输, 依赖不适用"
    broken = []
    for r in receipts:
        hit = conn.execute(
            "SELECT 1 FROM stock_observations "
            "WHERE reserve_id=? AND material_id=? LIMIT 1",
            (r["reserve_id"], r["material_id"]),
        ).fetchone()
        if hit is None:
            broken.append("%s (%s/%s)" % (r["receipt_id"], r["reserve_id"], r["material_id"]))
    if broken:
        return "unsatisfied", "签收记录无法与库存观测闭环核验: " + "; ".join(broken)
    return "satisfied", "全部%d条签收记录均可与库存观测闭环核验" % len(receipts)


_TIME_TOLERANCE = timedelta(seconds=60)  # 观测时间晚于落库时间的允许容差


def _check_dep_t5d1(conn) -> tuple:
    """T5-D1: 多源观测一致性 — 负值矛盾 / 运输状态非法值 / 时间线倒挂。"""
    conflicts = []
    total = 0
    for table, col, label in (
        ("stock_observations", "quantity", "库存"),
        ("demand_records", "required_quantity", "需求"),
        ("transport_observations", "capacity", "运输容量"),
    ):
        bad = conn.execute(
            "SELECT COUNT(*) c FROM %s WHERE %s IS NOT NULL AND %s < 0"
            % (table, col, col)
        ).fetchone()["c"]
        total += conn.execute(
            "SELECT COUNT(*) c FROM %s" % table
        ).fetchone()["c"]
        if bad:
            conflicts.append("%s观测存在%d条负值记录" % (label, bad))
    bad_states = conn.execute(
        "SELECT DISTINCT availability FROM transport_observations "
        "WHERE availability NOT IN ('AVAILABLE','IN_TRANSIT','FAULT')"
    ).fetchall()
    if bad_states:
        conflicts.append("运输状态非法值: %s"
                         % ", ".join(r["availability"] for r in bad_states))
    inverted = 0
    for table, obs_col in (
        ("stock_observations", "observed_at"),
        ("demand_records", "observed_at"),
        ("transport_observations", "observed_at"),
    ):
        for row in conn.execute(
            "SELECT %s AS o, created_at AS c FROM %s" % (obs_col, table)
        ):
            o, c = _parse_ts(row["o"]), _parse_ts(row["c"])
            if o is not None and c is not None and o > c + _TIME_TOLERANCE:
                inverted += 1
    for row in conn.execute(
        "SELECT received_at AS o, created_at AS c FROM receipt_records"
    ):
        o, c = _parse_ts(row["o"]), _parse_ts(row["c"])
        if o is not None and c is not None and o > c + _TIME_TOLERANCE:
            inverted += 1
    if inverted:
        conflicts.append("观测/签收时间晚于落库时间%d条 (超过容差)" % inverted)
    if total == 0 and not conflicts:
        return "unknown", "无任何观测证据, 无法校验一致性"
    if conflicts:
        return "unsatisfied", "; ".join(conflicts)
    return "satisfied", "库存/需求/运输/签收观测之间无矛盾"


_ALL_TASK_IDS = ("T1", "T2", "T3", "T4", "T5")

# 全任务依赖分发表: task_id -> {dep_id: 判断函数}。T1 复用板块3函数, 不改其代码。
_ALL_TASK_CHECKERS = {
    "T1": {"T1-D1": _check_dep_t1d1},
    "T2": {"T2-D1": _check_dep_t2d1},
    "T3": {"T3-D1": _check_dep_t3d1},
    "T4": {"T4-D1": _check_dep_t4d1},
    "T5": {"T5-D1": _check_dep_t5d1},
}


def run_all_tasks_check(run_id: str) -> tuple:
    """串行执行 T1~T5 全套依赖校验, 结果一次性落库 dependency_checks + task_results。

    - 仅 status=pending 的 run 允许执行; completed / 不存在直接拒绝。
    - 全部 checks/results 在同一事务内用 INSERT...SELECT FROM runs
      WHERE status='pending' 原子写入, 任一写入 rowcount=0 整体回滚, 不写任何数据。
    - 同 run 重复执行覆盖旧结果 (每 run 每依赖/每任务只保留最新一次)。

    返回 (ok, msg, data);
    data = {run_id, tasks:[{task_id, task_status, task_msg, deps:[...]}]}
    """
    seed_t1_meta()   # 幂等保底 (板块3函数, 不改动)
    seed_t2t5_meta()
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT status FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False, "run %s not found" % run_id, None
            if row["status"] != "pending":
                return False, "run %s is completed, not modifiable" % run_id, None

            tasks_out = []
            for task_id in _ALL_TASK_IDS:
                dep_rows = conn.execute(
                    "SELECT dep_id FROM dependency_definitions "
                    "WHERE task_id=? ORDER BY dep_id",
                    (task_id,),
                ).fetchall()
                dep_results = []
                for d in dep_rows:
                    dep_id = d["dep_id"]
                    checker = _ALL_TASK_CHECKERS.get(task_id, {}).get(dep_id)
                    if checker is None:
                        dep_results.append({
                            "dep_id": dep_id, "status": "unknown",
                            "check_msg": "依赖 %s 的判断逻辑未实现" % dep_id,
                        })
                    else:
                        status, check_msg = checker(conn)
                        dep_results.append(
                            {"dep_id": dep_id, "status": status, "check_msg": check_msg}
                        )
                if not dep_results:
                    dep_results.append({
                        "dep_id": "-", "status": "unknown",
                        "check_msg": "任务 %s 无依赖定义" % task_id,
                    })
                if any(r["status"] == "unsatisfied" for r in dep_results):
                    task_status = "blocked"
                elif all(r["status"] == "satisfied" for r in dep_results):
                    task_status = "established"
                else:
                    task_status = "unknown"
                task_msg = "; ".join(
                    "%s:%s" % (r["dep_id"], r["status"]) for r in dep_results
                )
                tasks_out.append({
                    "task_id": task_id, "task_status": task_status,
                    "task_msg": task_msg, "deps": dep_results,
                })

            # ---- 原子落库 (同一事务): 全部 dependency_checks + task_results ----
            new_check_ids, new_result_ids = [], []
            for t in tasks_out:
                for r in t["deps"]:
                    if r["dep_id"] == "-":  # 无依赖定义的占位行不落库
                        continue
                    cur = conn.execute(
                        "INSERT INTO dependency_checks "
                        "(run_id, dep_id, status, check_msg, created_at) "
                        "SELECT ?,?,?,?,? FROM runs WHERE run_id=? AND status='pending'",
                        (run_id, r["dep_id"], r["status"], r["check_msg"], _now(), run_id),
                    )
                    if cur.rowcount == 0:
                        conn.rollback()
                        return False, "run %s is completed, not modifiable" % run_id, None
                    new_check_ids.append((r["dep_id"], cur.lastrowid))
                cur = conn.execute(
                    "INSERT INTO task_results "
                    "(run_id, task_id, status, result_msg, created_at) "
                    "SELECT ?,?,?,?,? FROM runs WHERE run_id=? AND status='pending'",
                    (run_id, t["task_id"], t["task_status"], t["task_msg"], _now(), run_id),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return False, "run %s is completed, not modifiable" % run_id, None
                new_result_ids.append((t["task_id"], cur.lastrowid))
            # 覆盖旧结果: 同一 run 的同一依赖/任务只保留本次新写入的行
            for dep_id, check_id in new_check_ids:
                conn.execute(
                    "DELETE FROM dependency_checks "
                    "WHERE run_id=? AND dep_id=? AND check_id<?",
                    (run_id, dep_id, check_id),
                )
            for task_id, result_id in new_result_ids:
                conn.execute(
                    "DELETE FROM task_results WHERE run_id=? AND task_id=? AND result_id<?",
                    (run_id, task_id, result_id),
                )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc, None
    return True, "checked", {
        "run_id": run_id,
        "tasks": [
            {k: t[k] for k in ("task_id", "task_status", "task_msg", "deps")}
            for t in tasks_out
        ],
    }
