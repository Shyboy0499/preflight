"""Assemble demo/preflight-demo.mp4 from narration (macOS say), cards and real UI frames.

Each scene lasts exactly as long as its narration (+ a short pause). UI scenes use the real
recorded frames at their captured timing; "running" waits are compressed and the whole scene is
uniformly re-timed to fit, never reordered or edited. Captions are the narration, burned in.

Usage: python scripts/video/build.py     (reads demo/storyboard.json written by storyboard.py)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cards import caption_png  # noqa: E402

TEMPO_NOTE = "voice-over sped up 8% with ffmpeg atempo (pitch preserved)"

VOICE = "Karen (Premium)"
FPS = 30
PAD = 0.6  # seconds of silence after each scene's narration
TEMPO = 1.08  # pitch-preserving speed-up of the voice-over (Premium voices ignore say -r)
WORK = Path("demo/build")
AUDIO = WORK / "audio"
CAPS = WORK / "captions"
SCENES_OUT = WORK / "scenes"


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
    """Split narration into chunks that each fit the two-line caption bar (84 chars per line)."""
    import textwrap

    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks: list[str] = []
    for s in sentences:
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


def captions(sid: str, text: str, speech: float, total: float) -> Path:
    """Concat list of caption overlays timed proportionally to their length in the narration."""
    chunks = caption_chunks(text)
    weights = [len(c) for c in chunks]
    lines, used = [], 0.0
    for i, (c, w) in enumerate(zip(chunks, weights)):
        png = CAPS / f"{sid}_{i:02d}.png"
        caption_png(png, c)
        dur = speech * w / sum(weights)
        lines += [f"file '{png.resolve()}'", f"duration {dur:.3f}"]
        used += dur
    blank = CAPS / "blank.png"
    if not blank.exists():
        from PIL import Image

        Image.new("RGBA", (1920, 1080), (0, 0, 0, 0)).save(blank)
    lines += [f"file '{blank.resolve()}'", f"duration {max(0.05, total - used):.3f}", f"file '{blank.resolve()}'"]
    path = CAPS / f"{sid}.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


def frames_list(sid: str, spec: dict, total: float) -> Path:
    """Real UI frames for the given tags, re-timed to fill `total` seconds."""
    folder = Path(spec["dir"])
    timeline = json.loads((folder / "timeline.json").read_text())
    picked = [f for f in timeline if any(f["tag"].startswith(t) for t in spec["tags"])]
    if not picked:
        raise SystemExit(f"{sid}: no frames for tags {spec['tags']} in {folder}")
    durs = [max(0.033, b["t"] - a["t"]) for a, b in zip(picked, picked[1:])] + [0.4]
    fast = spec.get("fast_tags", [])
    slow_total = sum(d for f, d in zip(picked, durs) if any(f["tag"] == t for t in fast))
    if slow_total > spec.get("fast_max", 2.0):  # compress agent "thinking" waits
        k = spec.get("fast_max", 2.0) / slow_total
        durs = [d * k if any(f["tag"] == t for t in fast) else d for f, d in zip(picked, durs)]
    real = sum(durs)
    k = min(total / real, 1.6) if real < total else total / real
    durs = [d * k for d in durs]
    durs[-1] += max(0.0, total - sum(durs))
    lines = []
    for f, d in zip(picked, durs):
        lines += [f"file '{(folder / f['frame']).resolve()}'", f"duration {d:.4f}"]
    lines.append(f"file '{(folder / picked[-1]['frame']).resolve()}'")
    path = WORK / f"{sid}_frames.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


def render_scene(scene: dict) -> Path:
    sid, text, vis = scene["id"], scene["narration"], scene["visual"]
    wav, speech = narrate(sid, text)
    total = round(speech + PAD, 3)
    caps = captions(sid, text, speech, total)
    out = SCENES_OUT / f"{sid}.mp4"
    if vis["type"] == "image":
        video_in = ["-loop", "1", "-t", f"{total}", "-i", vis["path"]]
        base = "[0:v]scale=1920:1080,setsar=1"
    elif vis["type"] == "pan":
        video_in = ["-loop", "1", "-t", f"{total}", "-i", vis["path"]]
        h = vis.get("height", 860)
        base = (f"[0:v]scale=-2:{h},crop=1920:{h}:'min(iw-1920,max(0,(iw-1920)*(t-1)/{max(1.0, total - 3):.2f}))':0,"
                f"pad=1920:1080:0:{(1080 - h) // 2}:color=white,setsar=1")
    elif vis["type"] == "frames":
        video_in = ["-f", "concat", "-safe", "0", "-i", str(frames_list(sid, vis, total))]
        base = "[0:v]scale=1920:1080,setsar=1"
    else:
        raise SystemExit(f"unknown visual type {vis['type']}")
    graph = f"{base},fps={FPS},format=rgba[v0];[1:v]fps={FPS},format=rgba[c];[v0][c]overlay=0:0,format=yuv420p[v]"
    run(["ffmpeg", "-y", "-loglevel", "error", *video_in, "-f", "concat", "-safe", "0", "-i", str(caps), "-i", str(wav),
         "-filter_complex", graph, "-map", "[v]", "-map", "2:a", "-af", "apad", "-t", f"{total}",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
         str(out)])
    print(f"  {sid}: {total:5.1f}s  ({vis['type']})", flush=True)
    return out


def main() -> None:
    for d in (AUDIO, CAPS, SCENES_OUT):
        d.mkdir(parents=True, exist_ok=True)
    board = json.loads(Path("demo/storyboard.json").read_text())
    parts = [render_scene(s) for s in board["scenes"]]
    lst = WORK / "scenes.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    final = Path("demo/preflight-demo.mp4")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-r", str(FPS), "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(final)])
    print(f"done: {final}  {duration(final):.1f}s")


if __name__ == "__main__":
    main()
