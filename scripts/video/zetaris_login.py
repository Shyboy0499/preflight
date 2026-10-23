"""One-time Zetaris UI login for the demo recorder. The user types the password; nothing is logged.

Opens a visible Chromium at http://localhost:3000. Once the user is signed in (or the file
demo/.auth/SAVE appears), saves cookies + localStorage to demo/.auth/zetaris.json (gitignored).
"""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

AUTH = Path("demo/.auth")
STATE, FLAG = AUTH / "zetaris.json", AUTH / "SAVE"


def logged_in(page) -> bool:
    url = page.url.lower()
    if "login" in url or "signin" in url or "auth" in url:
        return False
    try:
        has_storage = page.evaluate("() => Object.keys(window.localStorage).length > 0")
        has_password_box = page.locator("input[type=password]").count() > 0
    except Exception:
        return False
    return has_storage and not has_password_box


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.goto("http://localhost:3000")
    print("READY: sign in to Zetaris in the opened browser window.", flush=True)
    deadline, stable = time.time() + 20 * 60, 0
    while time.time() < deadline:
        time.sleep(2)
        stable = stable + 1 if logged_in(page) else 0
        if FLAG.exists() or stable >= 3:
            context.storage_state(path=str(STATE))
            print(f"SAVED session to {STATE} (url {page.url})", flush=True)
            break
    else:
        print("TIMEOUT: no login detected", flush=True)
    browser.close()
