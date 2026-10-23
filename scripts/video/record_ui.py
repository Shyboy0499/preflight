"""Drive the real Streamlit UI (live mode) and capture it as timestamped 4K frames with focus regions.

Nothing is staged: the UI talks to the running app, which talks to real Zetaris and the real model.
Frames are page screenshots of a 1536x864 CSS viewport at device scale 2.5 (3840x2160, the same
layout as 125% zoom at 1080p). Each frame records the scene tag and a "focus" rectangle (CSS px,
viewport coordinates) of what the narration is about; the editor pans/zooms onto it, never edits it.

Usage: python scripts/video/record_ui.py <A|B>
Output: demo/frames/<scene>/NNNNN.jpg + timeline.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "http://localhost:8599"
SCENE = sys.argv[1]
OUT = Path("demo/frames") / SCENE
OUT.mkdir(parents=True, exist_ok=True)
for old in [*OUT.glob("*.png"), *OUT.glob("*.jpg")]:
    old.unlink()
VIEW_W, VIEW_H, SCALE = 1536, 864, 2.5
COLUMN = (372, 1470)  # x range of Streamlit's main column (CSS px)

timeline: list[dict] = []
t0 = time.time()
focus: list[float] | None = None


def box(page, locator) -> list[float] | None:
    try:
        b = locator.first.bounding_box(timeout=1500)
    except Exception:
        return None
    return [b["x"], b["y"], b["width"], b["height"]] if b else None


def union(*boxes, pad: float = 18) -> list[float] | None:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes) - pad
    y0 = min(b[1] for b in boxes) - pad
    x1 = max(b[0] + b[2] for b in boxes) + pad
    y1 = max(b[1] + b[3] for b in boxes) + pad
    return [max(0, x0), max(0, y0), min(VIEW_W, x1) - max(0, x0), min(VIEW_H, y1) - max(0, y0)]


def snap(page, tag: str) -> None:
    path = OUT / f"{len(timeline):05d}.jpg"
    page.screenshot(path=str(path), type="jpeg", quality=88)
    timeline.append({"frame": path.name, "t": round(time.time() - t0, 3), "tag": tag, "focus": focus})


def hold(page, tag: str, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        snap(page, tag)


def heading(page, text: str):
    return page.locator("h1,h2,h3", has_text=text)


def scroll_heading_to(page, text: str, tag: str, top: int = 70) -> None:
    """Smoothly scroll so a heading sits near the top; snap while scrolling."""
    page.evaluate(f"""() => {{ const h = [...document.querySelectorAll('h1,h2,h3')].find(e => e.textContent.includes({json.dumps(text)}));
        if (h) {{ const c = h.closest('[data-testid="stMain"]') || document.scrollingElement;
                  const y = h.getBoundingClientRect().top - {top};
                  (c.scrollBy ? c : window).scrollBy({{top: y, behavior: 'smooth'}}); }} }}""")
    for _ in range(14):
        page.wait_for_timeout(60)
        snap(page, tag)


def column_band(page, top_locator, height: float = 470) -> list[float] | None:
    b = box(page, top_locator)
    if not b:
        return None
    return [COLUMN[0], max(0, b[1] - 16), COLUMN[1] - COLUMN[0], height]


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=SCALE)
    page = ctx.new_page()
    page.goto(URL)
    run_btn = page.get_by_role("button", name="Run Preflight")
    run_btn.wait_for(timeout=60000)
    page.wait_for_timeout(800)
    req = page.get_by_label("Cover request")
    focus = union(box(page, req), box(page, run_btn), box(page, page.get_by_text("A shift-cover agent that checks")), pad=30)
    req.fill("")
    snap(page, "intro")
    req.type("Ward 4B needs night cover tomorrow", delay=35)
    hold(page, "typed", 0.8)
    run_btn.click()
    page.wait_for_timeout(400)
    for _ in range(800):  # the agent runs: real wait, compressed in the edit
        snap(page, "running")
        if page.get_by_text("Checking before acting").count() == 0 and heading(page, "Verifier: candidates").count() > 0:
            break
        page.wait_for_timeout(150)

    def outcome_focus():
        alerts = page.locator('[data-testid="stAlert"]')
        verifier_y = (box(page, heading(page, "Verifier: candidates")) or [0, VIEW_H])[1]
        top_alerts = [b for i in range(min(alerts.count(), 6)) if (b := box(page, alerts.nth(i))) and b[1] < verifier_y]
        return union(box(page, heading(page, "Naive agent")), box(page, heading(page, "Preflight")), *top_alerts,
                     box(page, page.get_by_role("button", name="Send")))

    page.mouse.wheel(0, -5000)
    page.wait_for_timeout(400)
    if SCENE == "B" and heading(page, "Earlier actions for this shift").count():
        # Key moment first: the withdrawn offer and the leave row that proves it
        scroll_heading_to(page, "Earlier actions for this shift", "recheck", top=140)
        focus = union(box(page, heading(page, "Earlier actions for this shift")),
                      box(page, page.locator('[data-testid="stAlert"]', has_text="Withdrawn")),
                      box(page, page.locator('[data-testid="stDataFrame"]')), pad=24)
        hold(page, "recheck", 4.5)
        page.mouse.wheel(0, -5000)
        page.wait_for_timeout(400)
    scroll_heading_to(page, "Naive agent", "outcome", top=90)
    focus = outcome_focus()
    hold(page, "outcome", 3.5)

    scroll_heading_to(page, "Verifier: candidates", "verifier", top=70)
    first = page.locator('[data-testid="stAlert"]', has_text="Re-plan") if SCENE == "B" else heading(page, "Verifier: candidates")
    focus = column_band(page, first if first.count() else heading(page, "Verifier: candidates"))
    hold(page, "verifier", 2.5)
    for _ in range(5 if SCENE == "B" else 2):  # read down the refusals / checks (A's page is short)
        page.mouse.move(900, 600)
        for _ in range(10):
            page.mouse.wheel(0, 30 if SCENE == "B" else 25)
            page.wait_for_timeout(50)
            snap(page, "verifier")
        hold(page, "verifier", 0.9)
    if SCENE == "B":
        scroll_heading_to(page, "H-MEM recall", "memory", top=70)
        focus = union(box(page, heading(page, "H-MEM recall")), box(page, heading(page, "World Model facts")),
                      [COLUMN[0], 70, COLUMN[1] - COLUMN[0], 470])
        hold(page, "memory", 4.0)

    # Manager sends the prepared action
    page.mouse.wheel(0, -8000)
    page.wait_for_timeout(400)
    scroll_heading_to(page, "Naive agent", "send", top=90)
    focus = outcome_focus()
    send = page.get_by_role("button", name="Send")
    if send.count():
        send.first.hover()
        hold(page, "send", 1.0)
        send.first.click()
        page.get_by_text("Sent (simulated)").first.wait_for(timeout=20000)
        page.wait_for_timeout(500)
        focus = outcome_focus()
        hold(page, "sent", 3.0)
    browser.close()

(OUT / "timeline.json").write_text(json.dumps(timeline, indent=0))
print(f"{SCENE}: {len(timeline)} frames over {timeline[-1]['t']:.1f}s -> {OUT}")
