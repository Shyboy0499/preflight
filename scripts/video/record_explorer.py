"""Record the Zetaris Data Explorer showing Preflight's three sources (uses the saved, gitignored session).

Output: demo/frames/explorer/NNNNN.png + timeline.json (tag "explorer").
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("demo/frames/explorer")
OUT.mkdir(parents=True, exist_ok=True)
for old in OUT.glob("*.png"):
    old.unlink()
timeline: list[dict] = []
t0 = time.time()


def snap(page, tag="explorer"):
    path = OUT / f"{len(timeline):05d}.png"
    page.screenshot(path=str(path))
    timeline.append({"frame": path.name, "t": round(time.time() - t0, 3), "tag": tag})


def hold(page, seconds):
    end = time.time() + seconds
    while time.time() < end:
        snap(page)


def expand(page, label: str):
    node = page.get_by_text(label, exact=True).first
    node.wait_for(timeout=15000)
    node.hover()
    hold(page, 0.4)
    node.click()
    page.wait_for_timeout(700)
    hold(page, 1.2)


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(storage_state="demo/.auth/zetaris.json", viewport={"width": 1536, "height": 864}, device_scale_factor=1.25)
    page = ctx.new_page()
    page.goto("http://localhost:3000/", wait_until="networkidle")
    page.wait_for_timeout(1500)
    hold(page, 1.0)
    expand(page, "File Source & API")
    expand(page, "PREFLIGHT_FILES")
    expand(page, "PREFLIGHT_API")
    if page.get_by_text("preflight_pg", exact=True).count() == 0:
        expand(page, "Database")
    expand(page, "preflight_pg")
    hold(page, 2.5)
    b.close()

(OUT / "timeline.json").write_text(json.dumps(timeline, indent=0))
print(f"explorer: {len(timeline)} frames over {timeline[-1]['t']:.1f}s")
