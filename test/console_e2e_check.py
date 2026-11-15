# -*- coding: utf-8 -*-
"""Console 前端五视图端到端检查（真实浏览器，Playwright headless）。"""
import json
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000/console/"
import os
SHOT_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "docs", "screenshots"))
os.makedirs(SHOT_DIR, exist_ok=True)

results = []
console_errors = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ((" :: " + detail) if detail else ""))


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(str(e)))

    page.goto(BASE, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    check("页面标题", "保障任务指挥台" in page.title() or "保障任务指挥台" in page.content())
    check("连接状态不再显示 connecting", page.locator("#connText").inner_text() != "连接中",
          page.locator("#connText").inner_text())
    opts = page.locator("#runSel option")
    n_runs = opts.count()
    check("run 下拉已填充", n_runs >= 25, f"{n_runs} 个 run")

    # ---- 选择 RUN-025（回填后 T1~T5 全绿）----
    page.select_option("#runSel", value="BB03-RUN-025")
    page.wait_for_selector(".tfnode", timeout=15000)
    page.screenshot(path=os.path.join(SHOT_DIR, "01-overview.png"), full_page=True)
    chips = page.locator(".tfnode .tfstatus").all_inner_texts()
    check("RUN-025 流程条 5 节点", len(chips) >= 5, str(chips[:5]))
    allgreen = all("成立" in c for c in chips[:5])
    check("RUN-025 全绿 (T1 established)", allgreen, str(chips[:5]))

    # ---- 缺口视图 ----
    page.click("a[href='#/gaps']")
    page.wait_for_timeout(1200)
    gaps_text = page.locator("#view").inner_text()
    check("缺口视图渲染", "缺口" in gaps_text or "blocker" in gaps_text)
    page.screenshot(path=os.path.join(SHOT_DIR, "02-gaps-run025.png"), full_page=True)

    # ---- 证据视图 ----
    page.click("a[href='#/evidence']")
    page.wait_for_timeout(1500)
    ev_text = page.locator("#view").inner_text()
    check("证据视图含 RCT-S5-DEMO-001", "RCT-S5-DEMO-001" in ev_text, "")
    page.screenshot(path=os.path.join(SHOT_DIR, "03-evidence-run025.png"), full_page=True)

    # ---- 方案对比视图（切 RUN-021 S3 基线）----
    page.select_option("#runSel", value="BB03-RUN-021")
    page.wait_for_timeout(1200)
    page.click("a[href='#/plans']")
    page.wait_for_timeout(1500)
    plans_text = page.locator("#view").inner_text()
    check("方案视图含 PLAN-A", "PLAN-A" in plans_text)
    check("方案视图含 PLAN-B", "PLAN-B" in plans_text)
    check("方案视图含 PLAN-C", "PLAN-C" in plans_text)
    check("PLAN-A 可执行标注", "可执行" in plans_text or "executable" in plans_text)
    page.screenshot(path=os.path.join(SHOT_DIR, "04-plans-run021.png"), full_page=True)

    # ---- run trace 下钻（总览页点击任务列表行展开追溯链路）----
    page.click("a[href='#/overview']")
    page.wait_for_timeout(1200)
    try:
        page.locator("#view tr.click").first.click()
        page.wait_for_selector("#chainSlot", timeout=10000)
        page.wait_for_timeout(800)
        chain = page.locator("#chainSlot").inner_text()
        check("trace 链路面板出现", ("Run Trace" in chain and "T1" in chain),
              chain[:100].replace("\n", " | "))
    except Exception as e:
        check("trace 链路面板出现", False, str(e)[:120])
    page.screenshot(path=os.path.join(SHOT_DIR, "05-trace-run021.png"), full_page=True)

    # ---- 链路面板点「依赖」按钮 → 下钻到缺口视图依赖明细 ----
    try:
        page.locator("#chainSlot button", has_text="依赖").first.click()
        page.wait_for_timeout(1500)
        gaps2 = page.locator("#view").inner_text()
        check("依赖下钻显示 T1-D1", "T1-D1" in gaps2, gaps2[:80].replace("\n", " | "))
        check("依赖下钻定位到 T1", "focus=task%3DT1" in page.evaluate("location.hash"), page.evaluate("location.hash"))
    except Exception as e:
        check("依赖下钻显示 T1-D1", False, str(e)[:120])
    page.screenshot(path=os.path.join(SHOT_DIR, "05b-trace-drilldown.png"), full_page=True)

    # ---- 拓扑视图（Three.js canvas）----
    page.click("a[href='#/topology']")
    page.wait_for_timeout(4000)
    canvas = page.locator("canvas#topo")
    box = canvas.bounding_box()
    check("拓扑 canvas 存在且有尺寸", box is not None and box["width"] > 300 and box["height"] > 300,
          str(box))
    # 读像素确认 WebGL 实际画了内容
    px = page.evaluate("""() => {
        const c = document.getElementById('topo');
        const gl = c.getContext('webgl') || c.getContext('webgl2');
        if (!gl) return 'no-webgl';
        const buf = new Uint8Array(4);
        gl.readPixels(Math.floor(c.width/2), Math.floor(c.height/2), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, buf);
        return Array.from(buf).join(',');
    }""")
    check("拓扑 WebGL 渲染非空", px not in ("no-webgl", "0,0,0,0"), px)
    page.screenshot(path=os.path.join(SHOT_DIR, "06-topology.png"), full_page=True)

    browser.close()

print("\n=== JS console errors ===")
print(console_errors if console_errors else "none")

fails = [r for r in results if not r[1]]
print(f"\n=== 结果: {len(results)-len(fails)}/{len(results)} PASS ===")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
