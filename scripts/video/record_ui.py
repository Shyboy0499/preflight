"""Drive the real Streamlit UI (live mode) and capture it as timestamped 1080p frames.

Nothing is staged: the UI talks to the running app, which talks to real Zetaris and the
real model provider. Frames are page screenshots at 125% zoom (1536x864 CSS px at
device scale 1.25 = 1920x1080). Each frame is tagged with the scene it belongs to, so the
editor can speed up waits (drop frames) without changing what is shown.

Usage: python scripts/video/record_ui.py <scene>  where scene is A or B
Output: demo/frames/<scene>/NNNNN.png and demo/frames/<scene>/timeline.json
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
for old in OUT.glob("*.png"):
    old.unlink()

timeline: list[dict] = []
t0 = time.time()


def snap(page, tag: str) -> None:
    path = OUT / f"{len(timeline):05d}.png"
    page.screenshot(path=str(path))
    timeline.append({"frame": path.name, "t": round(time.time() - t0, 3), "tag": tag})


def hold(page, tag: str, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        snap(page, tag)


def scroll(page, tag: str, pixels: int, steps: int = 12) -> None:
    page.mouse.move(900, 600)
    for _ in range(steps):
        page.mouse.wheel(0, pixels / steps)
        page.wait_for_timeout(60)
        snap(page, tag)


def scroll_to(page, text: str, tag: str) -> None:
    """Smoothly scroll until an element with this text is near the top."""
    loc = page.get_by_text(text, exact=False).first
    for _ in range(40):
        box = loc.bounding_box()
        if box is None or box["y"] < 160:
            break
        page.mouse.move(900, 600)
        page.mouse.wheel(0, min(160, box["y"] - 120))
        page.wait_for_timeout(60)
        snap(page, tag)
    hold(page, tag, 0.6)


def wait_for_run(page, tag: str) -> None:
    """Snap while the agent runs; done when the spinner is gone and results render."""
    page.wait_for_timeout(400)
    for _ in range(600):  # up to ~2 minutes
        snap(page, tag)
        spinning = page.get_by_text("Checking before acting").count() > 0
        if not spinning and page.get_by_text("Verifier: candidates").count() > 0:
            break
        page.wait_for_timeout(150)
    hold(page, tag + ":done", 0.8)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1536, "height": 864}, device_scale_factor=1.25)
    page = ctx.new_page()
    page.goto(URL)
    page.get_by_role("button", name="Run Preflight").wait_for(timeout=60000)
    page.wait_for_timeout(800)
    box = page.get_by_label("Cover request")
    box.fill("")
    snap(page, "intro")
    box.type("Ward 4B needs night cover tomorrow", delay=35)
    hold(page, "typed", 0.8)
    page.get_by_role("button", name="Run Preflight").click()
    wait_for_run(page, "running")

    # Outcome: naive agent vs Preflight, with the prepared action(s)
    page.mouse.wheel(0, -5000)
    page.wait_for_timeout(300)
    scroll_to(page, "Naive agent", "outcome")
    hold(page, "outcome", 2.0)
    if SCENE == "B":
        if page.get_by_text("Earlier actions for this shift").count():
            scroll_to(page, "Earlier actions for this shift", "recheck")
            hold(page, "recheck", 3.0)
    scroll_to(page, "Verifier: candidates", "verifier")
    hold(page, "verifier", 2.5)
    scroll(page, "verifier", 900)
    hold(page, "verifier", 1.5)
    if SCENE == "B":
        # Bring the Meterless section (H-MEM recall + World Model facts) to the top, smoothly
        page.evaluate("""() => { const h = [...document.querySelectorAll('h1,h2,h3')].find(e => e.textContent.includes('H-MEM recall'));
                                  if (h) h.scrollIntoView({block: 'start', behavior: 'smooth'}); }""")
        for _ in range(20):
            page.wait_for_timeout(60)
            snap(page, "memory")
        hold(page, "memory", 4.0)
    # Manager sends the prepared action
    page.mouse.wheel(0, -8000)
    page.wait_for_timeout(300)
    scroll_to(page, "Naive agent", "send")
    send = page.get_by_role("button", name="Send")
    if send.count():
        send.first.hover()
        hold(page, "send", 0.8)
        send.first.click()
        page.wait_for_timeout(1500)
        page.get_by_text("Sent (simulated)").first.wait_for(timeout=20000)
        hold(page, "sent", 2.0)
    browser.close()

(OUT / "timeline.json").write_text(json.dumps(timeline, indent=0))
print(f"{SCENE}: {len(timeline)} frames over {timeline[-1]['t']:.1f}s -> {OUT}")
