"""1920x1080 cards and caption overlays for the demo video (Pillow). Text only; data comes from callers."""
from __future__ import annotations

import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
SANS = "/System/Library/Fonts/Avenir Next.ttc"
MONO = "/System/Library/Fonts/Menlo.ttc"
INK, MUTED, ACCENT, BG, CODE_BG = (17, 24, 39), (90, 100, 115), (220, 38, 38), (250, 250, 248), (17, 24, 39)


def font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    if mono:
        return ImageFont.truetype(MONO, size, index=1 if bold else 0)
    # Avenir Next.ttc on this Mac: 2 = Demi Bold, 7 = Regular
    return ImageFont.truetype(SANS, size, index=2 if bold else 7)


def _canvas(bg=BG) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im = Image.new("RGB", (W, H), bg)
    return im, ImageDraw.Draw(im)


def title_card(path: Path, title: str, subtitle: str, lines: list[str], footer: str = "") -> None:
    im, d = _canvas()
    d.rectangle([0, 0, 24, H], fill=ACCENT)
    d.text((140, 300), title, font=font(120, True), fill=INK)
    d.text((140, 450), subtitle, font=font(52), fill=INK)
    y = 560
    for line in lines:
        d.text((140, y), line, font=font(38), fill=MUTED)
        y += 60
    if footer:
        d.text((140, 790), footer, font=font(30), fill=MUTED)  # above the caption bar
    im.save(path)


def bullets_card(path: Path, heading: str, items: list[str], note: str = "") -> None:
    im, d = _canvas()
    d.rectangle([0, 0, 24, H], fill=ACCENT)
    d.text((140, 150), heading, font=font(72, True), fill=INK)
    y = 300
    for it in items:
        d.ellipse([150, y + 22, 168, y + 40], fill=ACCENT)
        for i, ln in enumerate(textwrap.wrap(it, 62)):
            d.text((200, y + i * 56), ln, font=font(44), fill=INK)
        y += 56 * len(textwrap.wrap(it, 62)) + 34
    if note:
        d.text((140, 790), note, font=font(32), fill=MUTED)  # above the caption bar
    im.save(path)


def code_card(path: Path, heading: str, lines: list[str], note: str = "", size: int = 26) -> None:
    im, d = _canvas()
    d.text((100, 70), heading, font=font(56, True), fill=INK)
    d.rounded_rectangle([90, 160, W - 90, 770], radius=18, fill=CODE_BG)  # leaves room for the caption bar
    y = 190
    f = font(size, mono=True)
    for ln in lines:
        colour = (134, 239, 172) if "verifier" in ln and ("PASS" in ln or "fired" in ln) else (229, 231, 235)
        if "planner" in ln:
            colour = (147, 197, 253)
        if "FAIL" in ln or "withdraw" in ln:
            colour = (252, 165, 165)
        d.text((130, y), ln[:118], font=f, fill=colour)
        y += int(size * 1.55)
        if y > 730:
            break
    if note:
        d.text((100, 790), note, font=font(30), fill=MUTED)
    im.save(path)


def scoreboard_card(path: Path, rows: list[tuple[str, str, str]], heading: str, note: str) -> None:
    im, d = _canvas()
    d.rectangle([0, 0, 24, H], fill=ACCENT)
    d.text((140, 120), heading, font=font(64, True), fill=INK)
    x0, x1, x2 = 140, 1100, 1450
    d.text((x1, 270), "Preflight", font=font(40, True), fill=INK)
    d.text((x2, 270), "Naive agent", font=font(40, True), fill=INK)
    y = 350
    for label, a, b in rows:
        d.line([x0, y - 18, W - 140, y - 18], fill=(220, 220, 220), width=2)
        d.text((x0, y), label, font=font(40), fill=INK)
        d.text((x1, y - 8), a, font=font(60, True), fill=(22, 163, 74))
        d.text((x2, y - 8), b, font=font(60, True), fill=ACCENT)
        y += 130
    d.text((140, 760), note, font=font(32), fill=MUTED)
    im.save(path)


def caption_png(path: Path, text: str) -> None:
    """Transparent full frame with a compact caption bar at the bottom."""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    f = font(34, True)
    lines = textwrap.wrap(text, 84)[:2]
    widest = max(d.textlength(ln, font=f) for ln in lines)
    bar_w, bar_h = widest + 80, 26 + 48 * len(lines)
    x0, y0 = (W - bar_w) / 2, H - bar_h - 28
    d.rounded_rectangle([x0, y0, x0 + bar_w, y0 + bar_h], radius=14, fill=(0, 0, 0, 175))
    y = y0 + 13
    for ln in lines:
        w = d.textlength(ln, font=f)
        d.text(((W - w) / 2, y), ln, font=f, fill=(255, 255, 255, 255))
        y += 48
    im.save(path)
