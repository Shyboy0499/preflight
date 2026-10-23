"""Render the README's Mermaid architecture diagram (exact text) to demo/assets/architecture.png."""
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

src = re.search(r"```mermaid\n(.*?)```", Path("README.md").read_text(), re.S).group(1)
html = f"""<!doctype html><html><head><meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>body{{margin:0;background:#fff;display:flex;align-items:center;justify-content:center;height:100vh;font-family:'Avenir Next',sans-serif}}
#d svg{{max-width:1760px;max-height:880px;width:1760px;height:auto}}</style></head>
<body><div id="d" class="mermaid">{src}</div>
<script>mermaid.initialize({{startOnLoad:true, theme:'neutral', flowchart:{{curve:'basis', nodeSpacing:40, rankSpacing:60}}, themeVariables:{{fontSize:'20px'}}}});</script>
</body></html>"""
Path("demo/assets/architecture.html").write_text(html)
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=2)
    pg.goto(Path("demo/assets/architecture.html").resolve().as_uri())
    pg.wait_for_selector("#d svg", timeout=30000)
    pg.wait_for_timeout(500)
    pg.locator("#d svg").screenshot(path="demo/assets/architecture.png")  # tight crop at 2x, panned in the video
    b.close()
print("ok")
