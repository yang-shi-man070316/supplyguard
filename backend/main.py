"""应急物资保障 SupplyGuard - 后端骨架（Day 1）

教学案例来源：S03-B 案例 B-B03《应急物资保障任务认知》（V0.8R1，改造参赛）

已实现的：
- 校园极端天气演练场景：3 个关键服务点 + 2 个储备点 + 3 类教学物资
- 缺口计算：需求 − 已签收 − 在途 = 缺口
- 签收语义：**签收取证失败 ≠ 未送达**，状态判为「待验证」而非「未送达」
- run_id：每次分析生成独立 UUID，旧 run 不覆盖

留给学生用 AI 完成的：
- TODO[阶段1] 缺口优先级排序：按服务点等级 + 缺口比例，而非原始数量
- TODO[阶段2] 签收证据链：签收记录带拍照/签收人/时间，能回到原投送单
- TODO[阶段3] LLM 解释层（只解释结构化结果，不判定保障状态）
- TODO[阶段4] 物资保障知识 RAG 问答
"""

import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from mqtt_client import CURRENT_STATE, start_mqtt, stop_mqtt

DB = Path(__file__).parent / "supplyguard.db"
FRONTEND = Path(__file__).parent.parent / "frontend"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    start_mqtt()  # 连接失败只打警告, 不阻塞启动
    yield
    stop_mqtt()


app = FastAPI(title="应急物资保障 SupplyGuard", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ---- 教学规模固定（不放大，避免变成物流调度系统）----
MATERIALS = ["饮用水", "应急照明", "急救包"]

SITES = [
    # (id, 名称, 类型, 人数/规模)
    ("S1", "教学楼 A", "service", 320),
    ("S2", "图书馆", "service", 180),
    ("S3", "体育馆", "service", 260),
    ("D1", "物资仓库 1", "depot", 0),
    ("D2", "物资仓库 2", "depot", 0),
]

# 初始需求（人 × 系数，示意用）
BASE_DEMAND = {
    ("S1", "饮用水"): 320,
    ("S1", "应急照明"): 40,
    ("S1", "急救包"): 6,
    ("S2", "饮用水"): 180,
    ("S2", "应急照明"): 24,
    ("S2", "急救包"): 4,
    ("S3", "饮用水"): 260,
    ("S3", "应急照明"): 32,
    ("S3", "急救包"): 5,
}

# 已签收（演练前已到位的部分）
DELIVERED = {
    ("S1", "饮用水"): 200,
    ("S1", "应急照明"): 40,
    ("S2", "饮用水"): 180,
    ("S2", "急救包"): 4,
    ("S3", "应急照明"): 32,
}

# 在途（已派出、未签收）
IN_TRANSIT = [
    # (投送单, 起点, 终点, 物资, 数量, 状态)
    ("T-001", "D1", "S1", "饮用水", 120, "dispatched"),
    ("T-002", "D1", "S3", "饮用水", 260, "dispatched"),
    ("T-003", "D2", "S1", "急救包", 6, "sign_failed"),  # 签收取证失败：送达与否未知
]


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sites (
            id TEXT PRIMARY KEY, name TEXT, kind TEXT, scale INTEGER
        );
        CREATE TABLE IF NOT EXISTS demand (
            site_id TEXT, material TEXT, qty INTEGER,
            PRIMARY KEY (site_id, material)
        );
        CREATE TABLE IF NOT EXISTS stock (
            site_id TEXT, material TEXT, qty INTEGER,
            PRIMARY KEY (site_id, material)
        );
        CREATE TABLE IF NOT EXISTS shipments (
            id TEXT PRIMARY KEY, from_depot TEXT, to_site TEXT,
            material TEXT, qty INTEGER, status TEXT, note TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, created_at TEXT, summary TEXT
        );
        """
    )
    conn.execute("DELETE FROM sites")
    conn.execute("DELETE FROM demand")
    conn.execute("DELETE FROM stock")
    conn.execute("DELETE FROM shipments")
    conn.executemany(
        "INSERT INTO sites VALUES (?,?,?,?)",
        [list(s) for s in SITES],
    )
    conn.executemany(
        "INSERT INTO demand VALUES (?,?,?)",
        [[k[0], k[1], v] for k, v in BASE_DEMAND.items()],
    )
    conn.executemany(
        "INSERT INTO stock VALUES (?,?,?)",
        [[k[0], k[1], v] for k, v in DELIVERED.items()],
    )
    conn.executemany(
        "INSERT INTO shipments VALUES (?,?,?,?,?,?,?)",
        [list(t) + [""] for t in IN_TRANSIT],
    )
    conn.commit()
    conn.close()


def compute_state():
    """结构化计算：每个服务点 × 每种物资的状态。不调用 LLM。"""
    conn = db()
    rows = conn.execute(
        """
        SELECT s.id AS site, s.name AS site_name, d.material, d.qty AS demand,
               COALESCE(k.qty, 0) AS delivered
        FROM sites s
        JOIN demand d ON d.site_id = s.id
        LEFT JOIN stock k ON k.site_id = d.site_id AND k.material = d.material
        WHERE s.kind = 'service'
        ORDER BY s.id, d.material
        """
    ).fetchall()

    transit = conn.execute(
        "SELECT to_site, material, qty, status FROM shipments"
    ).fetchall()
    conn.close()

    # 在途按状态拆分：已发运计入可用，签收取证失败计入"待验证"
    in_transit_ok = {}
    pending = {}
    for t in transit:
        key = (t["to_site"], t["material"])
        if t["status"] == "dispatched":
            in_transit_ok[key] = in_transit_ok.get(key, 0) + t["qty"]
        elif t["status"] == "sign_failed":
            pending[key] = pending.get(key, 0) + t["qty"]

    items = []
    for r in rows:
        key = (r["site"], r["material"])
        demand = r["demand"]
        delivered = r["delivered"]
        transit_qty = in_transit_ok.get(key, 0)
        pending_qty = pending.get(key, 0)

        available = delivered + transit_qty
        gap_raw = max(demand - available, 0)

        # 核心语义：签收取证失败 → 待验证，不判未送达
        # 待验证时不输出确定缺口，只给潜在区间 [最乐观, 最悲观]
        gap_lo, gap_hi = gap_raw, gap_raw
        if pending_qty > 0 and gap_raw > 0:
            status = "待验证"
            gap_lo = max(demand - available - pending_qty, 0)
            gap_hi = gap_raw
            gap_out = 0  # 未定，不判确定缺口
            note = "有 %d 件签收取证失败，送达未定；潜在缺口 %d–%d" % (
                pending_qty, gap_lo, gap_hi)
        elif gap_raw == 0:
            status = "满足"
            gap_out = 0
            note = "已到位或已发运"
        elif available == 0:
            status = "缺口"
            gap_out = gap_raw
            note = "无到位、无在途"
        else:
            status = "部分满足"
            gap_out = gap_raw
            note = "尚缺 %d" % gap_raw

        items.append(
            {
                "site": r["site"],
                "site_name": r["site_name"],
                "material": r["material"],
                "demand": demand,
                "delivered": delivered,
                "in_transit": transit_qty,
                "sign_pending": pending_qty,
                "gap": gap_out,
                "gap_range": [gap_lo, gap_hi],
                "status": status,
                "note": note,
            }
        )

    summary = {
        "total": len(items),
        "满足": sum(1 for i in items if i["status"] == "满足"),
        "缺口": sum(1 for i in items if i["status"] == "缺口"),
        "部分满足": sum(1 for i in items if i["status"] == "部分满足"),
        "待验证": sum(1 for i in items if i["status"] == "待验证"),
    }
    return items, summary


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/api/state")
def state():
    items, summary = compute_state()
    return {"items": items, "summary": summary}


@app.get("/api/current-state")
def current_state():
    """B-B03 阶段01: 返回 MQTT 收到的最新内存状态 (不落库、不保存历史)."""
    return CURRENT_STATE


@app.post("/api/analyze")
def analyze():
    """跑一次保障分析，生成独立 run_id。先计算，再解释。"""
    items, summary = compute_state()
    run_id = str(uuid.uuid4())
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = db()
    conn.execute(
        "INSERT INTO runs VALUES (?,?,?)",
        (run_id, ts, "满足%d/缺口%d/待验证%d" % (summary["满足"], summary["缺口"], summary["待验证"])),
    )
    conn.commit()
    conn.close()

    # TODO[阶段3]：把 items 交给 LLM，只让它解释，不让它改 status
    explanation = (
        "本次共 %d 项物资需求：满足 %d 项、缺口 %d 项、部分满足 %d 项、待验证 %d 项。"
        % (
            summary["total"],
            summary["满足"],
            summary["缺口"],
            summary["部分满足"],
            summary["待验证"],
        )
    )
    pending = [i for i in items if i["status"] == "待验证"]
    if pending:
        explanation += (
            "其中 %d 项存在签收取证失败，按规定判为待验证——"
            "签收失败不等于未送达，需补充取证后再下结论。" % len(pending)
        )
    explanation += "（此段为规则模板输出，阶段3 接入 LLM 后由其解释结构化结果。）"

    return {
        "run_id": run_id,
        "created_at": ts,
        "items": items,
        "summary": summary,
        "explanation": explanation,
    }


@app.post("/api/shipment/{shipment_id}/sign")
def sign(shipment_id: str, ok: bool = True):
    """模拟签收。ok=false 表示签收取证失败。"""
    conn = db()
    row = conn.execute("SELECT * FROM shipments WHERE id=?", (shipment_id,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "msg": "投送单不存在"}

    if ok:
        conn.execute(
            "UPDATE shipments SET status='signed' WHERE id=?", (shipment_id,)
        )
        conn.execute(
            "INSERT INTO stock VALUES (?,?,?) ON CONFLICT(site_id, material) "
            "DO UPDATE SET qty = qty + ?",
            (row["to_site"], row["material"], row["qty"], row["qty"]),
        )
        msg = "签收成功，计入到位"
    else:
        conn.execute(
            "UPDATE shipments SET status='sign_failed' WHERE id=?", (shipment_id,)
        )
        msg = "签收取证失败：状态转为待验证，不判为未送达"

    conn.commit()
    conn.close()
    return {"ok": True, "msg": msg}


@app.post("/api/reset")
def reset():
    init_db()
    return {"ok": True, "msg": "演练数据已重置"}


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
