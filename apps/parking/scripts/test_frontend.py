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
  - "Where to park" (view=park), per encoding and for all 24 hours: every cell with hourly counts and 2019
    sensor bays carries the % full band an independent computation from data.js gives (OCC_BANDS: green
    < 35 / 35-55 / 55-70 with white dots, red 70-80 / 80-90 / >= 90), every other parking cell is grey;
    hotspots (heavy cells) and only they have the thick dark outline; the legend; the two best-bets groups
    (quiet / busy: entries, order, empty-group line, street names, click zooms and opens the panel); a CVD
    simulation of every red band vs every green shade; the view is unchanged when the ground truth in
    data.js is replaced by nonsense; switching back restores the occupancy map.
Screenshots go to the git-ignored apps/parking/.run/screenshots/; --update-screenshots writes them to the
committed apps/parking/reports/screenshots/ instead.

    .\\.venv\\Scripts\\python.exe -m pip install playwright      # once; uses the installed Chrome
    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\test_frontend.py [--headed] [--update-screenshots]
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
SHOTS_COMMITTED = pc.REPORTS / "screenshots"
SHOTS_SCRATCH = pc.APP / ".run" / "screenshots"  # git-ignored (apps/parking/.run/)
HOUR = 11
DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 390, "height": 844}
OCC_BANDS = [35, 55, 70, 80, 90]   # must equal index.html's OCC_BANDS (checked)
BAND_NAMES = ["g0", "g1", "g2", "r0", "r1", "r2"]
BEST_N = 3  # per group
PARK_HOURS = (8, 17)  # screenshots and best bets
# replaces every true occupancy in data.js before the page reads it: the Where-to-park view must not change
POISON_TRUTH = """(() => { let v; Object.defineProperty(window, 'PARKING_DATA', { configurable: true,
  get() { return v; },
  set(x) { for (const e of Object.values(x.encodings)) for (const t of e.truth) t.occupied = t.occupied.map(() => 99999); v = x; } }); })();"""
PARK_STATE_JS = """() => { const o = {bands: {}, untagged: 0, hot: [], fills: {}, wide: []};
  document.querySelectorAll('.leaflet-park-pane path').forEach((p) => {
    const b = p.getAttribute('data-band'), bits = p.getAttribute('data-bits');
    if (!b) { o.untagged++; return; }
    (o.bands[b] = o.bands[b] || []).push(bits);
    (o.fills[b] = o.fills[b] || []).push(getComputedStyle(p).fill);
    if (p.getAttribute('data-hotspot') === '1') o.hot.push(bits);
    if (parseFloat(p.getAttribute('stroke-width')) >= 2.5) o.wide.push(bits); });
  return o; }"""
BETS_JS = """() => Object.fromEntries([...document.querySelectorAll('#betList .betgroup')].map((g) => [g.dataset.group, {
  entries: [...g.querySelectorAll('.bet')].map((b) => [b.dataset.bits, b.dataset.kind, b.querySelector('.bet-label').innerText, b.innerText]),
  empty: (g.querySelector('.empty') || {}).innerText || null }]))"""


def band_of(c, h):
    """The Where-to-park band of a record with hourly counts at hour h, or None (grey)."""
    if not c or not c["capacity"]:
        return None
    p = 100 * c["cars"][h] / c["capacity"]
    if p < OCC_BANDS[2] and c["capacity"] - c["cars"][h] < 1:
        return None
    return BAND_NAMES[sum(p >= x for x in OCC_BANDS)]


def expected_park(e, h):
    """{band: set of bits} at hour h ("grey" for the rest), computed from data.js independently of the page."""
    recs = {c["bits"]: c for c in e["last_level"]["candidates"] if c["outline"]}
    recs.update({c["bits"]: c for c in e["cells"]})
    drawn = {p["bits"] for p in e["parking"]} | {c["bits"] for c in e["cells"]}
    out = {}
    for bits in drawn:
        out.setdefault(band_of(recs.get(bits), h) or "grey", set()).add(bits)
    return out


def street_title(e, c):
    s = e["streets"].get(c["bits"]) or []
    return " & ".join(s) if s else c["label"]


def expected_bets(e, h):
    """{"quiet": [(bits, kind, title, free)], "busy": [...]}: green cells only, top BEST_N per group."""
    def pick(recs, kind):
        g = [c for c in recs if c["outline"] and (band_of(c, h) or "r")[0] == "g"]
        g.sort(key=lambda c: (-(c["capacity"] - c["cars"][h]), 100 * c["cars"][h] / c["capacity"], street_title(e, c)))
        return [(c["bits"], kind, street_title(e, c), c["capacity"] - c["cars"][h]) for c in g[:BEST_N]]
    return {"quiet": pick(e["last_level"]["candidates"], "candidate"), "busy": pick(e["cells"], "mastic")}


# --- colour-vision deficiency: Machado, Oliveira & Fernandes (2009), severity 1, distance in OKLab x 100 ---
MACHADO = {
    "protan": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
    "deutan": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
    "tritan": ((1.255528, -0.076749, -0.178779), (-0.078411, 0.930809, 0.147602), (0.004733, 0.691367, 0.303900)),
}


def _lin(hex_):
    c = [int(hex_.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]


def _oklab(rgb):
    r, g, b = (min(max(x, 0.0), 1.0) for x in rgb)
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def cvd_distance(a, b, kind=None):
    def sim(h):
        x = _lin(h)
        return [sum(m * v for m, v in zip(row, x)) for row in MACHADO[kind]] if kind else x
    pa, pb = _oklab(sim(a)), _oklab(sim(b))
    return 100 * sum((p - q) ** 2 for p, q in zip(pa, pb)) ** 0.5


def main():
    pc.configure_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--update-screenshots", action="store_true",
                    help="overwrite the committed screenshots in apps/parking/reports/screenshots/ "
                         "(default: the git-ignored apps/parking/.run/screenshots/)")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    data = bf.parse_data_js(bf.OUT.read_text(encoding="utf-8"))
    url = PAGE.resolve().as_uri()
    SHOTS = SHOTS_COMMITTED if args.update_screenshots else SHOTS_SCRATCH
    SHOTS.mkdir(parents=True, exist_ok=True)
    written = []
    _shot = lambda page, name, **kw: (page.screenshot(path=str(SHOTS / name), **kw), written.append(name))  # noqa: E731
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
            _shot(page, f"{name}_1130.png")
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
                _shot(page, "morton_1130_cell_truth.png")
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
        _shot(page, f"morton_crawl_level{mid}.png")
        # the occupancy map draws nothing in the Where-to-park pane
        expect(count(page, ".leaflet-park-pane path") == 0, "map view: Where-to-park cells drawn")

        # ---- Where to park
        def set_hour(pg, h):
            pg.evaluate("(h) => window.parkingApp.setHour(h)", h)
            pg.wait_for_timeout(30)

        def norm(st):
            return {b: sorted(v) for b, v in st["bands"].items()}

        park_ref = {}  # (name, hour) -> (bands, bets), compared with the poisoned-truth page below
        for name in pc.ENCODINGS:
            e = data["encodings"][name]
            heavy = {c["bits"] for c in e["cells"]}
            load(page, f"enc={name}&hour={PARK_HOURS[0]}&view=park")
            tab = page.locator("#viewSwitch [aria-selected=true]").inner_text()
            expect(tab == "Where to park", f"{name}: view=park opens tab {tab!r}")
            expect(page.evaluate("window.parkingApp.OCC_BANDS") == OCC_BANDS, f"{name}: page OCC_BANDS != {OCC_BANDS}")
            expect(page.locator("#truthToggle").is_disabled() and page.locator("#crawlToggle").is_disabled(),
                   f"{name}: truth / collector toggles not disabled in the Where-to-park view")
            for pane in ("cells", "truth", "crawl", "hatch"):
                expect(count(page, f".leaflet-{pane}-pane path") == 0, f"{name}: {pane} pane drawn in the Where-to-park view")
            counts, greens, empties = {}, {}, {"quiet": [], "busy": []}
            for h in range(24):
                set_hour(page, h)
                st = page.evaluate(PARK_STATE_JS)
                want = expected_park(e, h)
                got = {b: set(v) for b, v in st["bands"].items()}
                ok = got == want and st["untagged"] == 0 and all(len(st["bands"][b]) == len(want[b]) for b in want)
                expect(ok, f"{name} {h:02d}:30: drawn bands { {b: len(v) for b, v in st['bands'].items()} } "
                           f"(untagged {st['untagged']}), expected { {b: len(v) for b, v in want.items()} }")
                # colour-blind cue: every green cell has the dotted pattern of its own shade; red and grey none
                for b, fills in st["fills"].items():
                    ok = all(f"parkFree{b[1]}" in f for f in fills) if b in BAND_NAMES[:3] else not any("parkFree" in f for f in fills)
                    expect(ok, f"{name} {h:02d}:30: pattern wrong on band {b}")
                # hotspots: exactly the heavy cells carry data-hotspot and the thick outline
                expect(set(st["hot"]) == heavy and set(st["wide"]) == heavy,
                       f"{name} {h:02d}:30: hotspot outline on {len(st['wide'])} cells (data-hotspot {len(st['hot'])}), heavy {len(heavy)}")
                # best bets, both groups, every hour (entries, order, titles, free counts, empty-group line)
                bets, wb = page.evaluate(BETS_JS), expected_bets(e, h)
                for g in ("quiet", "busy"):
                    got_e = [(b, k, t) for b, k, t, _ in bets[g]["entries"]]
                    ok = got_e == [(b, k, t) for b, k, t, _ in wb[g]]
                    ok &= all(f"{free:,} free" in txt for (*_, txt), (*_, free) in zip(bets[g]["entries"], wb[g]))
                    if wb[g]:
                        ok &= bets[g]["empty"] is None
                    else:
                        ok &= bool(bets[g]["empty"]) and f"at {h:02d}:30" in bets[g]["empty"]
                        empties[g].append(h)
                    expect(ok, f"{name} {h:02d}:30 {g}: best bets {got_e} / empty {bets[g]['empty']!r}, expected {wb[g]}")
                if h in (4, 8, 12, 17):
                    counts[h] = {b: len(want.get(b, ())) for b in BAND_NAMES + ["grey"]}
                greens[h] = set().union(*(want.get(b, set()) for b in BAND_NAMES[:3]))
            expect(greens[4] != greens[12], f"{name}: green set identical at 04:30 and 12:30")
            notes.append(f"{name}: Where to park checked for 24 hours (bands, patterns, hotspot outlines, best bets); " + "; ".join(
                f"{h:02d}:30 red {sum(counts[h][b] for b in BAND_NAMES[3:])} (>=90 {counts[h]['r2']}, 80-90 {counts[h]['r1']}, "
                f"70-80 {counts[h]['r0']}) green {sum(counts[h][b] for b in BAND_NAMES[:3])} (<35 {counts[h]['g0']}, "
                f"35-55 {counts[h]['g1']}, 55-70 {counts[h]['g2']}) grey {counts[h]['grey']}" for h in (4, 8, 12, 17)))
            notes.append(f"{name}: empty best-bet groups: quiet at {len(empties['quiet'])} hours {empties['quiet']}, "
                         f"busy at {len(empties['busy'])} hours {empties['busy']}")

            # legend
            lg = page.locator("#legendPark")
            expect(lg.is_visible() and not page.locator("#legendMap").is_visible(), f"{name}: park legend not shown")
            lg_txt = lg.inner_text().lower()  # the group headings are uppercased by CSS
            for must in ("Parking exists here", "learned during the search", "no hourly data", "Hotspot", "Space likely",
                         "under 35 %", "35–55 %", "55–70 %", "70–80 %", "80–90 %", "90 % or more", "thick dark outline"):
                expect(must.lower() in lg_txt, f"{name}: legend lacks {must!r}")
            hot_tip = page.locator("[data-k=legend-hotspot]").get_attribute("title") or ""
            expect("can be green when it has space" in hot_tip, f"{name}: hotspot legend tooltip {hot_tip!r}")

            # tooltip of a hotspot says "Hotspot" (hover one clear of the legend)
            set_hour(page, PARK_HOURS[0])
            boxes = page.evaluate("""() => [...document.querySelectorAll('.leaflet-park-pane path[data-hotspot="1"]')]
                .map((p) => { const r = p.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })""")
            lgbox = page.locator(".legend").bounding_box()
            spot = next((b for b in boxes if 60 < b[1] < DESKTOP["height"] - 40 and b[0] < 880
                         and not (lgbox["x"] <= b[0] <= lgbox["x"] + lgbox["width"] and lgbox["y"] <= b[1] <= lgbox["y"] + lgbox["height"])), None)
            if expect(spot is not None, f"{name}: no hotspot clear of the legend to hover"):
                page.mouse.move(*spot)
                page.wait_for_timeout(200)
                tip = page.locator(".leaflet-tooltip").last.inner_text() if count(page, ".leaflet-tooltip") else ""
                expect("Hotspot" in tip, f"{name}: hotspot tooltip {tip!r}")
                page.mouse.move(5, 5)

            # best bets and screenshots at 08:30 and 17:30
            for h in PARK_HOURS:
                set_hour(page, h)
                page.wait_for_timeout(350)  # fill transitions
                title = page.locator("#bestBetsTitle").inner_text()
                expect(title == f"Best bets at {h:02d}:30", f"{name}: best-bets heading {title!r}")
                park_ref[(name, h)] = (norm(page.evaluate(PARK_STATE_JS)), page.evaluate(BETS_JS))
                _shot(page, f"park_{name}_{h:02d}30.png")
            notes.append(f"{name}: best bets " + " | ".join(
                f"{h:02d}:30 {g}: " + (", ".join(f"{t} ({free} free)" for _, _, t, free in expected_bets(e, h)[g]) or "none")
                for h in PARK_HOURS for g in ("quiet", "busy")))

            # clicking a best bet zooms to the cell and opens its panel (street title, cell ID, kind)
            wb = expected_bets(e, PARK_HOURS[-1])
            group = "quiet" if wb["quiet"] else "busy"
            bits, kind, title, _ = wb[group][0]
            rec = next(c for c in (e["cells"] if kind == "mastic" else e["last_level"]["candidates"]) if c["bits"] == bits)
            page.locator(f"#betList .betgroup[data-group={group}] .bet").first.click()
            page.wait_for_timeout(700)
            st = page.evaluate("() => ({cell: parkingApp.state.cell, kind: parkingApp.state.cellKind, zoom: parkingApp.map.getZoom()})")
            pts = [p for r in rec["outline"] for p in r]
            ctr = [sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)]
            inside = page.evaluate("(c) => parkingApp.map.getBounds().contains(c)", ctr)
            expect(st["cell"] == bits and st["kind"] == kind and st["zoom"] >= 16 and inside,
                   f"{name}: best-bet click gave {st}, centre inside {inside}")
            h2 = page.locator("#cellPanel h2").inner_text()
            cid = page.locator("#cellPanel .cellid").inner_text()
            want_tag = "Hotspot" if kind == "mastic" else "Revealed during the search"
            expect(h2 == title and rec["label"] in cid and want_tag in cid,
                   f"{name}: panel title {h2!r} / {cid!r}, expected {title!r} / {rec['label']!r} / {want_tag!r}")
            expect(count(page, "#cellPanel .chart rect.hit") == 24, f"{name}: best-bet panel lacks the 24-hour chart")
            sel_w = page.evaluate(f"""() => document.querySelector('.leaflet-park-pane path[data-bits="{bits}"]').getAttribute('stroke-width')""")
            expect(sel_w == "4", f"{name}: selected cell outline width {sel_w}")

            # back to the occupancy map: heavy cells again, Where-to-park pane empty, toggles enabled
            page.locator("#viewSwitch [data-view=map]").click()
            page.wait_for_timeout(150)
            expect(count(page, ".leaflet-cells-pane path") == len(e["cells"]) and count(page, ".leaflet-park-pane path") == 0
                   and page.locator("#truthToggle").is_enabled(), f"{name}: switching back to the occupancy map failed")
            expect("view=" not in page.evaluate("location.hash"), f"{name}: map view keeps view= in the URL")
            if kind == "mastic":  # the occupancy map keeps its own panel title
                h2m = page.locator("#cellPanel h2").inner_text()
                expect(h2m == f"Area {rec['label']}", f"{name}: occupancy-map panel title changed to {h2m!r}")

        # colour-vision check: every red band vs every green shade (read from the page's CSS)
        cols = page.evaluate("""() => { const s = getComputedStyle(document.documentElement);
            return ['--pr0','--pr1','--pr2','--pg0','--pg1','--pg2'].map((v) => s.getPropertyValue(v).trim()); }""")
        reds, greens_c = cols[:3], cols[3:]
        rows = []
        for r, rn in zip(reds, ("70-80", "80-90", ">=90")):
            for g, gn in zip(greens_c, ("<35", "35-55", "55-70")):
                rows.append((rn, gn, r, g, cvd_distance(r, g), cvd_distance(r, g, "protan"), cvd_distance(r, g, "deutan"),
                             cvd_distance(r, g, "tritan")))
        worst = {k: min(rows, key=lambda x: x[i]) for k, i in (("normal", 4), ("protan", 5), ("deutan", 6), ("tritan", 7))}
        notes.append("CVD (Machado 2009, OKLab ΔE×100), red band × green shade [normal / protan / deutan / tritan]: " + "; ".join(
            f"{rn} {r} × {gn} {g}: {n:.1f}/{pr:.1f}/{de:.1f}/{tr:.1f}" for rn, gn, r, g, n, pr, de, tr in rows))
        notes.append("CVD worst pairs: " + ", ".join(f"{k} {w[4 + i]:.1f} ({w[0]} × {w[1]})" for i, (k, w) in enumerate(worst.items()))
                     + "; green cells also carry white dots (checked on every green cell, every hour)")
        page.close()

        # the Where-to-park view does not depend on the ground truth: nonsense truth, identical view
        page = new_page(DESKTOP)
        page.add_init_script(POISON_TRUTH)
        for name in pc.ENCODINGS:
            load(page, f"enc={name}&hour={PARK_HOURS[0]}&view=park")
            poisoned = page.evaluate(f"window.PARKING_DATA.encodings.{name}.truth.every((t) => t.occupied.every((v) => v === 99999))")
            expect(poisoned, f"{name}: truth replacement did not apply")
            for h in PARK_HOURS:
                set_hour(page, h)
                same = (norm(page.evaluate(PARK_STATE_JS)), page.evaluate(BETS_JS)) == park_ref[(name, h)]
                expect(same, f"{name} {h:02d}:30: Where-to-park view changes when the ground truth changes")
        notes.append("Where to park with every true occupancy replaced by 99999: identical bands and best bets (3 encodings × 08:30, 17:30)")
        page.close()

        # an empty best-bets group shows one line (no hour of the real data has one): every last-level candidate full
        page = new_page(DESKTOP)
        page.add_init_script("""(() => { let v; Object.defineProperty(window, 'PARKING_DATA', { configurable: true,
          get() { return v; },
          set(x) { for (const e of Object.values(x.encodings)) for (const c of e.last_level.candidates)
                     if (c.capacity) c.cars = c.cars.map(() => c.capacity); v = x; } }); })();""")
        load(page, f"enc=morton&hour={PARK_HOURS[0]}&view=park")
        bets = page.evaluate(BETS_JS)
        busy_want = [(b, k, t) for b, k, t, _ in expected_bets(data["encodings"]["morton"], PARK_HOURS[0])["busy"]]
        expect(bets["quiet"]["entries"] == [] and bets["quiet"]["empty"] == f"No quiet area with space at {PARK_HOURS[0]:02d}:30."
               and [(b, k, t) for b, k, t, _ in bets["busy"]["entries"]] == busy_want,
               f"empty quiet group: {bets['quiet']}, busy {len(bets['busy']['entries'])} entries")
        notes.append(f"empty group: with every last-level candidate full, the quiet group reads {bets['quiet']['empty']!r}")
        page.close()

        # phone width
        page = new_page(PHONE)
        load(page, f"enc=morton&hour={HOUR}")
        width = page.evaluate("document.documentElement.scrollWidth")
        expect(width <= PHONE["width"], f"phone: page is {width}px wide (horizontal scroll)")
        expect(count(page, ".leaflet-cells-pane path") == len(data["encodings"]["morton"]["cells"]), "phone: cells missing")
        _shot(page, "phone_morton_1130.png", full_page=True)
        page.close()

        # phone width, Where to park
        page = new_page(PHONE)
        load(page, f"enc=morton&hour={PARK_HOURS[0]}&view=park")
        width = page.evaluate("document.documentElement.scrollWidth")
        expect(width <= PHONE["width"], f"phone park: page is {width}px wide (horizontal scroll)")
        st = page.evaluate(PARK_STATE_JS)
        want = expected_park(data["encodings"]["morton"], PARK_HOURS[0])
        expect({b: set(v) for b, v in st["bands"].items()} == want, "phone park: bands differ")
        wb = expected_bets(data["encodings"]["morton"], PARK_HOURS[0])
        n_bets = len(wb["quiet"]) + len(wb["busy"])
        expect(page.locator("#betList .bet").count() == n_bets and page.locator("#betList .bet").first.is_visible(),
               "phone park: best bets missing")
        page.wait_for_timeout(350)
        _shot(page, f"phone_park_morton_{PARK_HOURS[0]:02d}30.png", full_page=True)
        page.close()
        browser.close()

    for n in notes:
        print(f"  {n}")
    print(f"  screenshots ({len(written)}) in {pc.relpath(SHOTS)}: {', '.join(sorted(written))}")
    for x in problems:
        print(f"  FAIL {x}")
    print("frontend test: pass" if not problems else f"frontend test: {len(problems)} problem(s)")
    print(json.dumps({"ok": not problems, "problems": len(problems)}))
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
