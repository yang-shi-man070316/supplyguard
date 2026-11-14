"""阶段05板块1: 接收复核请求存储层 (review_requests).

业务规则 (严格按阶段05板块1规格):
    1. create_review: 创建一条 pending 状态待复核请求, 绑定
       dispatch_id / 源 trigger_run_id / 服务点 / 物资;
       源 trigger_run_id 必须存在于 runs 表 (不存在 → 404)。
    2. 幂等: 同一 (dispatch_id, service_id, material_id) 不可重复生成
       pending 待复核单 — 表部分唯一索引 uq_review_pending_triple 兜底 +
       应用层先查后插; 重复创建返回已存在的 pending 单, 不新增行。
       复核完成后同一三元组允许再次生成 (再次待复核)。
    3. mark_review_completed: 仅允许 pending → completed 单向流转,
       回填 completed_at 时间戳; 重复标记幂等, 不覆盖已有完成时间。
    4. 建表: db.py _SCHEMA 已含同段 DDL, 后端启动 (import db) 自动建表;
       review_store.init_review_tables() 供自测脚本 / 手工迁移独立执行
       (IF NOT EXISTS, 幂等, 与 db.py / migration_05_01_review_requests.sql
       三段 DDL 完全一致)。
    5. 本板块只新增文件与新表, 不改动旧表、旧业务文件、main.py 接口、
       前端页面; 不提供对外 API (CLI: python review_store.py
       create|pending|get|complete ...)。

与后续板块的关系: 本模块只负责复核单的存储与状态流转, 复核判定的
触发与消费 (如回执到达后生成复核单、复核完成生成新 reality run)
由阶段05后续板块实现。
"""

import json
import sqlite3
from datetime import datetime

from db import close_conn, get_conn

# ---- review_id 生成规则: BB03-REV-001 / BB03-REV-002 / ... 自增 ----
REVIEW_ID_PREFIX = "BB03-REV-"
REVIEW_ID_DIGITS = 3

# ---- status 枚举 (对齐阶段05文档) ----
REVIEW_STATUS = ["pending", "completed"]

# 与 db.py _SCHEMA 中 review_requests 段完全一致的 DDL (自测/手工迁移用)
_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_requests (
    review_id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    dispatch_id TEXT NOT NULL,
    trigger_run_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','completed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_pending_triple
    ON review_requests(dispatch_id, service_id, material_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_review_requests_status
    ON review_requests(status);
CREATE INDEX IF NOT EXISTS idx_review_requests_trigger_run
    ON review_requests(trigger_run_id);
"""

# 幂等查询: 同一三元组的 pending 单
_PENDING_TRIPLE_SQL = (
    "SELECT * FROM review_requests "
    "WHERE dispatch_id=? AND service_id=? AND material_id=? "
    "AND status='pending'"
)


def _now() -> str:
    """复核完成时间戳 (与 t5_check/gap_check 同口径, 秒级 ISO)。"""
    return datetime.now().isoformat(timespec="seconds")


def init_review_tables():
    """独立建表入口 (幂等, 可重复调用)。

    db.py 在后端启动时已自动执行同段 DDL; 本函数供自测脚本与
    手工迁移使用 (等价于执行 migration_05_01_review_requests.sql)。
    """
    conn = get_conn()
    try:
        conn.executescript(_REVIEW_SCHEMA)
        conn.commit()
    finally:
        close_conn(conn)


def _next_review_id(conn) -> str:
    """按 BB03-REV-001/002... 自增规则计算下一个 review_id (只读)。"""
    row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(review_id, ?) AS INTEGER)) "
        "FROM review_requests WHERE review_id LIKE ?",
        (len(REVIEW_ID_PREFIX) + 1, REVIEW_ID_PREFIX + "%"),
    ).fetchone()
    seq = (row[0] or 0) + 1
    return "%s%0*d" % (REVIEW_ID_PREFIX, REVIEW_ID_DIGITS, int(seq))


def _review_to_dict(row) -> dict:
    return dict(row) if row is not None else None


def create_review(dispatch_id: str, trigger_run_id: str,
                  service_id: str, material_id: str) -> tuple:
    """创建一条 pending 待复核请求 (幂等)。

    - 任一必填字段缺失/为空 → (False, 400)
    - trigger_run_id 不存在于 runs → (False, 404)
    - 同 (dispatch_id, service_id, material_id) 已存在 pending 单
      → 不新增, 返回该已存在单 (True, 200, created=False)
    返回 (ok, http_status, msg, data); data =
    {review_id, created, review}。
    """
    for name, value in (("dispatch_id", dispatch_id),
                        ("trigger_run_id", trigger_run_id),
                        ("service_id", service_id),
                        ("material_id", material_id)):
        if value is None or not str(value).strip():
            return False, 400, "missing required field: %s" % name, None
    dispatch_id = str(dispatch_id).strip()
    trigger_run_id = str(trigger_run_id).strip()
    service_id = str(service_id).strip()
    material_id = str(material_id).strip()

    try:
        conn = get_conn()
        try:
            run = conn.execute(
                "SELECT 1 FROM runs WHERE run_id=?", (trigger_run_id,)
            ).fetchone()
            if run is None:
                return False, 404, "run %s not found" % trigger_run_id, None

            dup = conn.execute(
                _PENDING_TRIPLE_SQL, (dispatch_id, service_id, material_id)
            ).fetchone()
            if dup is not None:
                return True, 200, (
                    "同一 (dispatch_id, service_id, material_id) 已存在 "
                    "pending 复核单 %s, 跳过创建 (幂等)" % dup["review_id"]), {
                    "review_id": dup["review_id"],
                    "created": False,
                    "review": _review_to_dict(dup),
                }

            review_id = _next_review_id(conn)
            conn.execute(
                "INSERT INTO review_requests "
                "(review_id, service_id, material_id, dispatch_id, "
                "trigger_run_id) VALUES (?,?,?,?,?)",
                (review_id, service_id, material_id,
                 dispatch_id, trigger_run_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM review_requests WHERE review_id=?", (review_id,)
            ).fetchone()
        finally:
            close_conn(conn)
    except sqlite3.IntegrityError:
        # 并发兜底: 部分唯一索引冲突 → 读回已存在 pending 单返回, 不报错
        conn = get_conn()
        try:
            dup = conn.execute(
                _PENDING_TRIPLE_SQL, (dispatch_id, service_id, material_id)
            ).fetchone()
        finally:
            close_conn(conn)
        if dup is None:
            return False, 500, "db error: review insert conflict", None
        return True, 200, (
            "同一 (dispatch_id, service_id, material_id) 已存在 "
            "pending 复核单 %s, 跳过创建 (唯一索引兜底)" % dup["review_id"]), {
            "review_id": dup["review_id"],
            "created": False,
            "review": _review_to_dict(dup),
        }
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None
    return True, 200, "review %s created" % review_id, {
        "review_id": review_id,
        "created": True,
        "review": _review_to_dict(row),
    }


def get_pending_reviews() -> tuple:
    """查询全部 pending 状态复核单 (只读, 按创建时间升序)。

    返回 (ok, http_status, msg, data); data =
    {review_count, reviews: [...]}。
    """
    try:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM review_requests WHERE status='pending' "
                "ORDER BY created_at, review_id"
            ).fetchall()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None
    return True, 200, "ok", {
        "review_count": len(rows),
        "reviews": [_review_to_dict(r) for r in rows],
    }


def get_review_by_id(review_id: str) -> tuple:
    """按 review_id 单条查询复核单 (只读)。

    返回 (ok, http_status, msg, data); 不存在 → (False, 404)。
    data = {review}。
    """
    if review_id is None or not str(review_id).strip():
        return False, 400, "missing required field: review_id", None
    review_id = str(review_id).strip()
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM review_requests WHERE review_id=?", (review_id,)
            ).fetchone()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None
    if row is None:
        return False, 404, "review %s not found" % review_id, None
    return True, 200, "ok", {"review": _review_to_dict(row)}


def mark_review_completed(review_id: str) -> tuple:
    """将复核单状态置为 completed 并回填 completed_at (幂等)。

    - 仅 pending → completed 单向流转; 已完成单重复标记不覆盖
      原 completed_at (幂等成功)。
    - review_id 不存在 → (False, 404)
    返回 (ok, http_status, msg, data); data =
    {review_id, completed_at, review}。
    """
    if review_id is None or not str(review_id).strip():
        return False, 400, "missing required field: review_id", None
    review_id = str(review_id).strip()
    completed_at = _now()
    try:
        conn = get_conn()
        try:
            cur = conn.execute(
                "UPDATE review_requests SET status='completed', "
                "completed_at=? WHERE review_id=? AND status='pending'",
                (completed_at, review_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM review_requests WHERE review_id=?", (review_id,)
            ).fetchone()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None
    if row is None:
        return False, 404, "review %s not found" % review_id, None
    if cur.rowcount == 0:
        return True, 200, (
            "review %s already completed (幂等, completed_at 保持原值)"
            % review_id), {
            "review_id": review_id,
            "completed_at": row["completed_at"],
            "review": _review_to_dict(row),
        }
    return True, 200, "review %s completed" % review_id, {
        "review_id": review_id,
        "completed_at": row["completed_at"],
        "review": _review_to_dict(row),
    }


def is_review_completed(review_id: str) -> bool:
    """复核单状态判断工具函数。

    status == 'completed' → True; pending → False;
    不存在 (或 db 异常) → False。是否存在请用 get_review_by_id 区分。
    """
    if review_id is None or not str(review_id).strip():
        return False
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT status FROM review_requests WHERE review_id=?",
                (str(review_id).strip(),),
            ).fetchone()
        finally:
            close_conn(conn)
    except sqlite3.Error:
        return False
    return row is not None and row["status"] == "completed"


if __name__ == "__main__":
    import sys

    init_review_tables()

    def _print(result):
        ok, http_status, msg, data = result
        print(json.dumps({"ok": ok, "http_status": http_status,
                          "msg": msg, "data": data},
                         ensure_ascii=False, indent=2))
        return ok

    args = sys.argv[1:]
    usage = (
        "usage:\n"
        "  python review_store.py create <dispatch_id> <trigger_run_id> "
        "<service_id> <material_id>\n"
        "  python review_store.py pending\n"
        "  python review_store.py get <review_id>\n"
        "  python review_store.py complete <review_id>"
    )
    if not args:
        print(usage, file=sys.stderr)
        sys.exit(2)
    cmd = args[0]
    if cmd == "create" and len(args) == 5:
        ok = _print(create_review(args[1], args[2], args[3], args[4]))
    elif cmd == "pending" and len(args) == 1:
        ok = _print(get_pending_reviews())
    elif cmd == "get" and len(args) == 2:
        ok = _print(get_review_by_id(args[1]))
    elif cmd == "complete" and len(args) == 2:
        ok = _print(mark_review_completed(args[1]))
    else:
        print(usage, file=sys.stderr)
        sys.exit(2)
    sys.exit(0 if ok else 1)
