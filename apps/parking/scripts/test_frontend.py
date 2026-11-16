#!/usr/bin/env python3
"""Headless test of the parking frontend (apps/parking/frontend/index.html, P6).

Opens the page from file:// in the installed Chrome (Playwright channel "chrome", fallback "msedge";
no browser download) and fails on any console error, page error, failed request or HTTP error. Checks:
  - per encoding: one map polygon per heavy cell in data.js, the "not revealed" hatch, the privacy
    figures in the "What's private" panel;
  - clicking a cell opens the panel with its label, capacity, free spaces at the hour and 3 top hours;
  - the ground-truth toggle draws one polygon per below-threshold sensor cell and shows the banner;
  - "What the collector saw": for every encoding and level, the drawn regions = the drawable candidates
    in data.js, and the counter shows the level's and the total number of revealed regions.
Screenshots go to apps/parking/reports/screenshots/.

    .\\.venv\\Scripts\\python.exe -m pip install playwright      # once; uses the installed Chrome
    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\test_frontend.py [--headed]
"""

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_frontend as bf  # noqa: E402
import parking_common as pc  # noqa: E402

PAGE = bf.FRONTEND / "index.html"
SHOTS = pc.REPORTS / "screenshots"
HOUR = 11
DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 390, "height": 844}


def main():
    pc.configure_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    data = bf.parse_data_js(bf.OUT.read_text(encoding="utf-8"))
    url = PAGE.resolve().as_uri()
    SHOTS.mkdir(parents=True, exist_ok=True)
    problems, notes = [], []
    fail = problems.append

    def expect(cond, msg):
        if not cond:
            fail(msg)
        return cond

    with sync_playwright() as p:
        browser = None
        for channel in ("chrome", "msedge"):
            try:
                browser = p.chromium.launch(channel=channel, headless=not args.headed)
                notes.append(f"browser: {channel} {browser.version}")
                break
            except Exception as exc:  # noqa: BLE001 - try the next installed browser
                notes.append(f"{channel} unavailable: {str(exc).splitlines()[0]}")
        if browser is None:
            raise SystemExit("no installed Chrome or Edge found for Playwright")

        def new_page(viewport):
            page = browser.new_page(viewport=viewport)
            page.on("console", lambda m: m.type == "error" and fail(f"console error: {m.text}"))
            page.on("pageerror", lambda e: fail(f"page error: {e}"))
            page.on("requestfailed", lambda r: fail(f"request failed: {r.url} ({r.failure})"))
            page.on("response", lambda r: r.status >= 400 and fail(f"HTTP {r.status}: {r.url}"))
            return page

        def load(page, hash_):
            page.goto("about:blank")  # a hash-only change would not reload the page
            page.goto(f"{url}#{hash_}")
            page.wait_for_load_state("networkidle")
            page.wait_for_function("window.parkingApp !== undefined")
            page.wait_for_timeout(300)

        def count(page, sel):
            return page.locator(sel).count()

        page = new_page(DESKTOP)
        for name in pc.ENCODINGS:
            e = data["encodings"][name]
            load(page, f"enc={name}&hour={HOUR}")
            n_cells = count(page, ".leaflet-cells-pane path")
            expect(n_cells == len(e["cells"]), f"{name}: {n_cells} cell polygons, data.js has {len(e['cells'])}")
            expect(count(page, ".leaflet-hatch-pane path.not-revealed") == 1, f"{name}: no 'not revealed' hatch")
            expect(page.locator("#hourLabel").inner_text() == f"{HOUR:02d}:30", f"{name}: hour label")
            pressed = page.locator("#encSwitch button[aria-pressed=true]").inner_text()
            expect(pressed == e["name"], f"{name}: encoding switch shows {pressed!r}")
            pv = e["privacy"]
            expect(page.locator("[data-k=heavy-cells]").inner_text() == f"{pv['heavy_cells']:,}", f"{name}: heavy-cell figure")
            expect(page.locator("[data-k=privacy-revealed]").inner_text() == f"{pv['revealed']:,}", f"{name}: revealed figure")
            page.screenshot(path=str(SHOTS / f"{name}_1130.png"))
            notes.append(f"{name}: {n_cells} cell polygons, privacy {pv['revealed']} revealed / {pv['heavy_cells']} heavy")

            # click the cell drawn first (= data.js order) and read the panel
            c = e["cells"][0]
            page.locator(".leaflet-cells-pane path").first.click()
            page.wait_for_timeout(150)
            panel = page.locator("#cellPanel")
            title = panel.locator("h2").inner_text()
            expect(c["label"] in title, f"{name}: panel title {title!r}, expected {c['label']!r}")
            kv = panel.locator(".kv b").all_inner_texts()
            want_kv = [f"{c['capacity']:,}", f"{c['free'][HOUR]:,}", f"{round(c['pct'][HOUR])} %"]
            expect(kv == want_kv, f"{name}: panel figures {kv}, expected {want_kv}")
            top = panel.locator("ul.tight li").all_inner_texts()
            ranked = sorted(range(24), key=lambda h: (-c["free"][h], h))[:3]
            want_top = [f"{h:02d}:30: {c['free'][h]:,} free" for h in ranked]
            expect(len(top) == 3 and all(t.startswith(w) for t, w in zip(top, want_top)),
                   f"{name}: top hours {top}, expected {want_top}")
            expect(count(page, "#cellPanel .chart rect.hit") == 24, f"{name}: chart lacks 24 hour bars")

            # ground truth toggle
            page.locator("#truthToggle").check()
            page.wait_for_timeout(150)
            below = sum(1 for t in e["truth"] if not t["heavy"])
            n_truth = count(page, ".leaflet-truth-pane path")
            expect(n_truth == below, f"{name}: {n_truth} truth polygons, expected {below}")
            expect(page.locator("#banner").is_visible(), f"{name}: ground-truth banner not visible")
            if name == "morton":
                page.screenshot(path=str(SHOTS / "morton_1130_cell_truth.png"))
            page.locator("#truthToggle").uncheck()
            expect(count(page, ".leaflet-truth-pane path") == 0, f"{name}: truth polygons remain after toggling off")

            # collector layer: every level
            page.locator("#crawlToggle").check()
            for lvl in e["crawl"]:
                page.evaluate("""(v) => { const s = document.getElementById('level');
                                 s.value = v; s.dispatchEvent(new Event('input')); }""", lvl["level"])
                drawable = sum(1 for x in lvl["candidates"] if x["outline"] is not None)
                drawn = count(page, ".leaflet-crawl-pane path[data-crawl]")
                kept = count(page, ".leaflet-crawl-pane path[data-crawl=kept]")
                lv_txt = page.locator("[data-k=level-revealed]").inner_text()
                tot_txt = page.locator("[data-k=total-revealed]").inner_text()
                ok = (drawn == drawable and kept == sum(1 for x in lvl["candidates"] if x["heavy"] and x["outline"])
                      and lv_txt == f"{len(lvl['candidates']):,}" and tot_txt == f"{pv['revealed']:,}")
                nr = len(lvl["candidates"]) - drawable
                nr_el = page.locator("[data-k=level-noregion]")
                ok &= (nr_el.count() == 1 and nr_el.inner_text() == str(nr)) if nr else nr_el.count() == 0
                expect(ok, f"{name} level {lvl['level']}: drawn {drawn} (kept {kept}), data.js drawable {drawable}; "
                           f"counter {lv_txt} / {tot_txt}")
            notes.append(f"{name}: collector layer checked on {len(e['crawl'])} levels "
                         f"({sum(len(lv['candidates']) for lv in e['crawl'])} regions, "
                         f"{e['privacy']['no_region']} without outline)")
            page.locator("#crawlToggle").uncheck()
            expect(count(page, ".leaflet-crawl-pane path") == 0, f"{name}: crawl polygons remain after toggling off")

        # play button advances the hour
        load(page, f"enc=morton&hour={HOUR}")
        page.locator("#play").click()
        page.wait_for_timeout(2000)
        page.locator("#play").click()
        expect(page.locator("#hourLabel").inner_text() != f"{HOUR:02d}:30", "play button did not advance the hour")

        # collector layer screenshot: Morton, middle level
        mid = data["encodings"]["morton"]["crawl"][len(data["encodings"]["morton"]["crawl"]) // 2 - 1]["level"]
        load(page, f"enc=morton&hour={HOUR}&crawl={mid}")
        expect(count(page, ".leaflet-crawl-pane path[data-crawl]") > 0, "crawl screenshot: no regions drawn")
        page.screenshot(path=str(SHOTS / f"morton_crawl_level{mid}.png"))
        page.close()

        # phone width
        page = new_page(PHONE)
        load(page, f"enc=morton&hour={HOUR}")
        width = page.evaluate("document.documentElement.scrollWidth")
        expect(width <= PHONE["width"], f"phone: page is {width}px wide (horizontal scroll)")
        expect(count(page, ".leaflet-cells-pane path") == len(data["encodings"]["morton"]["cells"]), "phone: cells missing")
        page.screenshot(path=str(SHOTS / "phone_morton_1130.png"), full_page=True)
        page.close()
        browser.close()

    for n in notes:
        print(f"  {n}")
    shots = sorted(x.name for x in SHOTS.glob("*.png"))
    print(f"  screenshots ({len(shots)}): {', '.join(shots)}")
    for x in problems:
        print(f"  FAIL {x}")
    print("frontend test: pass" if not problems else f"frontend test: {len(problems)} problem(s)")
    print(json.dumps({"ok": not problems, "problems": len(problems)}))
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
