"""Assemble demo/preflight-demo.mp4 (v2): footage area 1920x960 above a fixed 120 px caption strip.

Each scene lasts exactly as long as its narration (+ a short pause). UI scenes use the real 4K
recorded frames at their captured timing and pan/zoom smoothly onto the focus region the recorder
logged, so body text reads at about 28 px; "running" waits are compressed, key-moment holds keep
their real length, nothing is reordered or edited. Captions are the narration, in the strip.

Usage: python scripts/video/build.py [scene_id ...]   (reads demo/storyboard.json from storyboard.py;
       with scene ids, only those scenes are re-rendered)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
from cards import font  # noqa: E402

VOICE = "Karen (Premium)"
TEMPO = 1.08  # pitch-preserving speed-up of the voice-over (Premium voices ignore say -r)
FPS = 30
PAD = 0.6
W, H, AREA_H, STRIP_H = 1920, 1080, 960, 120
SRC_SCALE = 2.5  # recorder device scale: CSS px -> source px
MIN_CROP_W, MAX_CROP_W = 1920, 2760  # never upscale; keep 16px body text >= ~28px in the final frame
BG, STRIP = (11, 18, 32), (11, 18, 32)
WORK = Path("demo/build")
AUDIO = WORK / "audio"


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd[:6])} ...\n{r.stderr[-1500:]}")


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out)


def narrate(sid: str, text: str) -> tuple[Path, float]:
    aiff, wav = AUDIO / f"{sid}.aiff", AUDIO / f"{sid}.wav"
    run(["say", "-v", VOICE, "-o", str(aiff), text])
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff), "-af", f"atempo={TEMPO}", "-ar", "48000", "-ac", "2", str(wav)])
    return wav, duration(wav)


def caption_chunks(text: str) -> list[str]:
    """Chunks that each fit two lines of the caption strip (84 chars per line); no words dropped."""
    chunks: list[str] = []
    for s in re.split(r"(?<=[.!?])\s+", text.strip()):
        cur = ""
        for w in s.split():
            trial = f"{cur} {w}".strip()
            if len(textwrap.wrap(trial, 84)) > 2:
                chunks.append(cur)
                cur = w
            else:
                cur = trial
        if cur:
            chunks.append(cur)
    return chunks


def caption_strip(text: str) -> Image.Image:
    im = Image.new("RGB", (W, STRIP_H), STRIP)
    d = ImageDraw.Draw(im)
    f = font(34, True)
    lines = textwrap.wrap(text, 84)[:2] if text else []
    y = (STRIP_H - 46 * len(lines)) / 2 + 2
    for ln in lines:
        d.text(((W - d.textlength(ln, font=f)) / 2, y), ln, font=f, fill=(255, 255, 255))
        y += 46
    return im


def caption_timeline(text: str, speech: float) -> list[tuple[float, Image.Image]]:
    chunks = caption_chunks(text)
    total = sum(len(c) for c in chunks)
    out, t = [], 0.0
    for c in chunks:
        out.append((t, caption_strip(c)))
        t += speech * len(c) / total
    out.append((speech + 0.05, caption_strip("")))
    return out


# --- visuals: each returns a function t -> 1920x960 RGB image ------------------------

def image_visual(spec: dict, total: float):
    im = Image.open(spec["path"]).convert("RGB")
    if im.size != (W, AREA_H):
        im = im.resize((W, int(im.height * W / im.width)), Image.LANCZOS).crop((0, 0, W, AREA_H))
    return lambda t: im


def pan_visual(spec: dict, total: float):
    src = Image.open(spec["path"]).convert("RGB")
    h = spec.get("height", 840)
    src = src.resize((int(src.width * h / src.height), h), Image.LANCZOS)
    canvas_bg = Image.new("RGB", (W, AREA_H), (255, 255, 255))
    span = max(0, src.width - W)

    def at(t: float) -> Image.Image:
        u = min(1.0, max(0.0, (t - 1.0) / max(1.0, total - 3.0)))
        u = u * u * (3 - 2 * u)  # ease in/out
        x = int(span * u)
        frame = canvas_bg.copy()
        frame.paste(src.crop((x, 0, x + W, h)), (0, (AREA_H - h) // 2))
        return frame

    return at


def crop_for(focus: list[float] | None, src_w: int, src_h: int) -> tuple[float, float, float, float]:
    """Source-pixel crop (x, y, w, h) at 2:1 around the focus, clamped for legibility and bounds."""
    if not focus:
        fx, fy, fw, fh = 0, 0, src_w, src_h
    else:
        fx, fy, fw, fh = [v * SRC_SCALE for v in focus]
    cx, cy = fx + fw / 2, fy + fh / 2
    w = max(fw, fh * 2)
    w = min(max(w, MIN_CROP_W), MAX_CROP_W, src_w)
    h = w / 2
    x = min(max(cx - w / 2, 0), src_w - w)
    y = min(max(cy - h / 2, 0), src_h - h)
    if focus and fy < y:  # if the focus is taller than the crop, keep its top in view
        y = max(0, fy - 12 * SRC_SCALE)
    return x, y, w, h


def frames_visual(spec: dict, total: float):
    folder = Path(spec["dir"])
    timeline = json.loads((folder / "timeline.json").read_text())
    picked = [f for f in timeline if any(f["tag"].startswith(t) for t in spec["tags"])]
    if not picked:
        raise SystemExit(f"no frames for {spec['tags']} in {folder}")
    durs = [max(0.033, b["t"] - a["t"]) for a, b in zip(picked, picked[1:])] + [0.4]
    fast, keep = spec.get("fast_tags", []), spec.get("keep_real", [])
    fast_total = sum(d for f, d in zip(picked, durs) if f["tag"] in fast)
    if fast_total > spec.get("fast_max", 2.0):  # compress agent "thinking" waits only
        k = spec.get("fast_max", 2.0) / fast_total
        durs = [d * k if f["tag"] in fast else d for f, d in zip(picked, durs)]
    fixed = sum(d for f, d in zip(picked, durs) if f["tag"] in keep)
    flex = sum(d for f, d in zip(picked, durs) if f["tag"] not in keep)
    k = min(1.6, max(0.25, (total - fixed) / flex)) if flex else 1.0
    durs = [d if f["tag"] in keep else d * k for f, d in zip(picked, durs)]
    durs[-1] += max(0.0, total - sum(durs))
    starts = [sum(durs[:i]) for i in range(len(durs))]
    cache: dict[str, Image.Image] = {}
    state: dict = {"rect": None}

    def source(i: int) -> Image.Image:
        name = picked[i]["frame"]
        if name not in cache:
            cache.clear()
            cache[name] = Image.open(folder / name).convert("RGB")
        return cache[name]

    def at(t: float) -> Image.Image:
        i = max(0, min(len(picked) - 1, sum(1 for s in starts if s <= t) - 1))
        src = source(i)
        target = crop_for(picked[i].get("focus"), *src.size)
        if state["rect"] is None:
            state["rect"] = target
        else:  # smooth pan/zoom toward the target
            state["rect"] = tuple(r + (g - r) * 0.14 for r, g in zip(state["rect"], target))
        x, y, w, h = state["rect"]
        return src.crop((int(x), int(y), int(x + w), int(y + h))).resize((W, AREA_H), Image.LANCZOS)

    return at


VISUALS = {"image": image_visual, "pan": pan_visual, "frames": frames_visual}


def render_scene(scene: dict) -> Path:
    sid, text, vis = scene["id"], scene["narration"], scene["visual"]
    wav, speech = narrate(sid, text)
    total = round(speech + PAD, 3)
    visual = VISUALS[vis["type"]](vis, total)
    caps = caption_timeline(text, speech)
    out = WORK / f"{sid}.mp4"
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-i", str(wav), "-af", "apad", "-t", f"{total}", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", str(out)],
        stdin=subprocess.PIPE)
    frame = Image.new("RGB", (W, H), BG)
    for n in range(int(round(total * FPS))):
        t = n / FPS
        frame.paste(visual(t), (0, 0))
        frame.paste(next(img for start, img in reversed(caps) if start <= t), (0, AREA_H))
        ff.stdin.write(frame.tobytes())
    ff.stdin.close()
    if ff.wait() != 0:
        raise SystemExit(f"ffmpeg failed for {sid}")
    print(f"  {sid}: {total:5.1f}s  ({vis['type']})", flush=True)
    return out


def main() -> None:
    AUDIO.mkdir(parents=True, exist_ok=True)
    board = json.loads(Path("demo/storyboard.json").read_text())
    only = set(sys.argv[1:])
    parts = []
    for s in board["scenes"]:
        out = WORK / f"{s['id']}.mp4"
        parts.append(render_scene(s) if (not only or s["id"] in only or not out.exists()) else out)
    lst = WORK / "scenes.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    final = Path("demo/preflight-demo.mp4")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
         "-movflags", "+faststart", str(final)])
    print(f"done: {final}  {duration(final):.1f}s")


if __name__ == "__main__":
    main()
