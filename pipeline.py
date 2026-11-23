"""
Clip Maker pipeline: download -> transcribe -> pick highlights -> render 9:16 captioned clips.

Every step logs through the `log` callback so the window can show progress.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

CLAUDE_MODEL = "claude-sonnet-5-5"
OUT_W, OUT_H = 1080, 1920


def _no_progress(step, state, detail="", pct=None):
    pass


PROG = _no_progress  # set by run(); the app uses it to animate each step

TRACKER_COLUMNS = [
    "Date", "Campaign", "Clip file", "Title", "Caption",
    "TikTok link", "Instagram link", "YouTube link", "X link",
    "Submitted (Y/N)", "Status", "Notes",
]


# ---------------------------------------------------------------- helpers

def parse_time(text: str) -> float | None:
    """'1:02:03', '62:03', '3723' -> seconds. Blank -> None."""
    text = (text or "").strip()
    if not text:
        return None
    parts = [float(p) for p in text.split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs


def fmt_time(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def slugify(text: str, maxlen: int = 40) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "").strip().lower()
    return re.sub(r"[\s_-]+", "-", text)[:maxlen].strip("-") or "clip"


def require_ffmpeg():
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "FFmpeg isn't installed (or Windows hasn't picked it up yet). "
            "Run 'Install (run once).bat' again, then restart your computer."
        )


def run_ffmpeg(args: list[str], cwd: Path | None = None):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, creationflags=flags)
    if res.returncode != 0:
        raise RuntimeError("FFmpeg failed:\n" + res.stderr[-1500:])


# ---------------------------------------------------------------- 1. download

def _dl_hook(d):
    if d.get("status") == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        pct = int(100 * done / total) if total else None
        PROG("fetch", "working", f"{done / 1e6:.0f} MB" + (f" of {total / 1e6:.0f} MB" if total else ""), pct)


def download(url: str, workdir: Path, start: float | None, end: float | None, log=print):
    import yt_dlp

    workdir.mkdir(parents=True, exist_ok=True)
    marker = workdir / "source_info.json"
    want = {"url": url, "start": start, "end": end}
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        have = [f for f in workdir.glob("source.*") if f.suffix in (".mp4", ".mkv", ".webm")]
        if have and all(info.get(k) == v for k, v in want.items()):
            log("Using the copy of this video downloaded earlier.")
            return have[0], info.get("title") or "video", info.get("uploader") or ""
    except Exception:
        pass
    for old in workdir.glob("source.*"):
        old.unlink()
    for old in workdir.glob("transcript.json"):
        old.unlink()

    opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [_dl_hook],
    }
    if start is not None or end is not None:
        from yt_dlp.utils import download_range_func
        opts["download_ranges"] = download_range_func(None, [(start or 0, end or float("inf"))])
        opts["force_keyframes_at_cuts"] = True
        log(f"Downloading only {fmt_time(start or 0)} to {fmt_time(end) if end else 'end'}...")
    else:
        log("Downloading the full video (long streams take a while)...")

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = sorted(workdir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("Download finished but no video file was found.")
    log(f"Downloaded: {info.get('title', 'video')}")
    marker.write_text(json.dumps({**want, "title": info.get("title"), "uploader": info.get("uploader")}), encoding="utf-8")
    return files[0], info.get("title") or "video", info.get("uploader") or ""


# ---------------------------------------------------------------- 2. transcribe

def load_audio(video: Path):
    """Decode audio with FFmpeg (16 kHz mono float32), so speech-to-text doesn't depend on PyAV versions."""
    import numpy as np

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        capture_output=True, creationflags=flags,
    )
    if res.returncode != 0 or not res.stdout:
        raise RuntimeError("Couldn't read the audio from this video: " + res.stderr.decode(errors="replace")[-400:])
    return np.frombuffer(res.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(video: Path, model_size: str, log=print):
    cache = video.with_name("transcript.json")
    if cache.exists() and cache.stat().st_mtime > video.stat().st_mtime:
        log("Using saved transcript.")
        return json.loads(cache.read_text(encoding="utf-8"))

    from faster_whisper import WhisperModel

    log(f"Loading speech-to-text ({model_size}). First run downloads the model...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    audio = load_audio(video)
    segments, info = model.transcribe(audio, word_timestamps=True, vad_filter=True)

    segs, words, last_pct = [], [], -10
    for s in segments:
        segs.append({"start": s.start, "end": s.end, "text": s.text.strip()})
        for w in s.words or []:
            if w.word.strip():
                words.append({"start": w.start, "end": w.end, "word": w.word.strip()})
        pct = int(100 * s.end / max(info.duration, 1))
        if pct >= last_pct + 10:
            log(f"Transcribing... {pct}%")
            PROG("listen", "working", f"Writing captions... {pct}%", pct)
            last_pct = pct

    data = {"duration": info.duration, "segments": segs, "words": words}
    cache.write_text(json.dumps(data), encoding="utf-8")
    log(f"Transcript done ({len(segs)} lines).")
    return data


# ---------------------------------------------------------------- 3. highlights

def loudness_per_second(video: Path) -> list[float]:
    """Average loudness for each second of audio (0..1-ish)."""
    import numpy as np

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video),
         "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
        capture_output=True, creationflags=flags,
    )
    audio = np.frombuffer(res.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    n = len(audio) // 8000
    if n == 0:
        return []
    rms = np.sqrt((audio[: n * 8000].reshape(n, 8000) ** 2).mean(axis=1))
    return (rms / (rms.max() or 1)).tolist()


PROMO = re.compile(
    r"\b(subscribe|subscribed|notification bell|like and sub|vote|voting|nominat\w*|awards?|link in (the )?(bio|description)|"
    r"sponsor\w*|use (my )?code|promo code|discount|merch|patreon|follow (me|us)|go follow|check out my|"
    r"\.com|dot com|shout ?out to|thanks for watching|see you (guys )?next time|smash that)\b", re.I)


def is_promo(text: str) -> bool:
    return bool(PROMO.search(text or ""))


def near_promo(tr: dict, start: float, end: float, pad: float = 20.0) -> bool:
    """True if a self-promo line sits inside the clip or within `pad` seconds of it."""
    return any(is_promo(x["text"]) for x in tr["segments"] if x["end"] > start - pad and x["start"] < end + pad)


def spread_out(cands: list[dict], n: int, duration: float, max_len: float) -> list[dict]:
    """Pick the best-scoring clips that are well apart, so they come from different parts of the video."""
    gap = max(60.0, duration / (n * 2.5)) if duration > 0 else 60.0
    while True:
        chosen = []
        for c in cands:
            if all(c["start"] >= x["end"] + gap or c["end"] <= x["start"] - gap for x in chosen):
                chosen.append(c)
            if len(chosen) >= n:
                return chosen
        if gap <= 5:
            return chosen
        gap /= 2


def pick_highlights_heuristic(video: Path, tr: dict, n: int, min_len: float, max_len: float, log=print):
    """Free fallback: loud, fast-talking, reaction-heavy stretches."""
    log("Picking moments with the free method (loudness + talking speed)...")
    loud = loudness_per_second(video)
    segs = tr["segments"]
    target = (min_len + max_len) / 2
    candidates = []
    for i, s in enumerate(segs):
        j, end = i, s["end"]
        while j + 1 < len(segs) and segs[j + 1]["end"] - s["start"] <= target:
            j += 1
            end = segs[j]["end"]
        length = end - s["start"]
        if length < min_len:
            continue
        text = " ".join(x["text"] for x in segs[i: j + 1])
        dur = tr.get("duration") or segs[-1]["end"]
        if near_promo(tr, s["start"], end):
            continue  # self-promo, sponsor reads, "vote for me", "subscribe"...
        if dur > 300 and (s["start"] < 20 or end > dur - 60):
            continue  # intros and outros are rarely the good stuff
        wps = len(text.split()) / length
        lo = loud[int(s["start"]): int(end) + 1] or [0]
        energy = sum(lo) / len(lo) + 0.5 * max(lo)
        punch = text.count("!") + text.count("?") * 0.5
        score = energy * 2 + min(wps, 4) * 0.5 + min(punch, 5) * 0.3
        candidates.append({"start": s["start"], "end": end, "score": score, "text": text})

    candidates.sort(key=lambda c: c["score"], reverse=True)
    chosen = spread_out(candidates, n, tr.get("duration") or segs[-1]["end"], max_len)
    for c in chosen:
        first = c["text"].split(".")[0][:60]
        c.update(title=first, caption=first, hook="")
    return sorted(chosen, key=lambda c: c["start"])


def pick_highlights_claude(tr: dict, n: int, min_len: float, max_len: float,
                           api_key: str, title: str, creator: str, rules: str, log=print):
    import anthropic

    log("Asking Claude to pick the best moments...")
    lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in tr["segments"]]
    transcript = "\n".join(lines)
    if len(transcript) > 400_000:  # keep very long streams within limits
        transcript = transcript[:400_000]

    prompt = f"""You are an expert short-form video clipper. Below is a timestamped transcript of
"{title}" by {creator or "a creator"}. Pick the {n} best moments to post as TikTok/Reels/Shorts clips.

Good clips: strong hook in the first 2 seconds, a complete thought or story, funny/surprising/emotional
or highly quotable, understandable without extra context, and a satisfying ending.
Each clip must be between {min_len:.0f} and {max_len:.0f} seconds. Clips must not overlap.
Spread the clips across the WHOLE video: different parts, at least a few minutes apart when possible.
Never pick intros, outros, sponsor reads, or self-promotion (asking people to subscribe, vote for awards,
follow, buy merch, use a code, visit a link).
Start and end on line boundaries from the transcript.
Campaign rules to respect: {rules or "none given"}

Reply with ONLY a JSON array, no other text. Each item:
{{"start": seconds, "end": seconds, "title": "short punchy title",
  "hook": "funny on-screen caption shown over the video, max 70 characters, written like a viewer reacting, e.g. 'they are having a little too much fun with this challenge'",
  "caption": "1-2 sentence post caption with 2-4 relevant hashtags", "why": "one line"}}

TRANSCRIPT:
{transcript}"""

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    raw = text[text.find("["): text.rfind("]") + 1]
    items = json.loads(raw)

    starts = [s["start"] for s in tr["segments"]]
    ends = [s["end"] for s in tr["segments"]]
    out = []
    for it in items:
        s = min(starts, key=lambda x: abs(x - float(it["start"])))
        e = min(ends, key=lambda x: abs(x - float(it["end"])))
        if e - s > max_len:
            e = s + max_len
        if e - s < min_len * 0.7:
            continue
        if near_promo(tr, s, e):
            continue
        out.append({"start": s, "end": e, "title": it.get("title", ""), "hook": it.get("hook", ""),
                    "caption": it.get("caption", ""), "why": it.get("why", ""), "score": 1.0 / (len(out) + 1)})
    out = spread_out(out, n, tr.get("duration") or 0, max_len) or out
    return sorted(out[:n], key=lambda c: c["start"])


# ---------------------------------------------------------------- 4. render

def ass_time(t: float) -> str:
    t = max(t, 0)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def clean_hook(text: str) -> str:
    return re.sub(r"[{}\\]", "", (text or "").replace("\n", " ")).strip()[:140]


def build_subtitles(words: list[dict], start: float, end: float, path: Path, hook: str = ""):
    """Word-by-word captions: 3-word chunks, current word highlighted yellow."""
    ws = [dict(w, start=w["start"] - start, end=w["end"] - start)
          for w in words if w["end"] > start and w["start"] < end]
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial Black,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,3,2,60,60,560,1
Style: Hook,Arial,62,&H00111111,&H00111111,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,16,0,8,120,120,470,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    chunks, cur = [], []
    for w in ws:
        gap = cur and w["start"] - cur[-1]["end"] > 0.6
        if cur and (len(cur) >= 3 or gap):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)

    clean = lambda s: re.sub(r"[{}\\]", "", s).upper()
    events = []
    for chunk in chunks:
        for i, w in enumerate(chunk):
            t0 = w["start"]
            t1 = chunk[i + 1]["start"] if i + 1 < len(chunk) else w["end"] + 0.15
            parts = [("{\\c&H00FFFF&}" + clean(x["word"]) + "{\\c&HFFFFFF&}") if j == i else clean(x["word"])
                     for j, x in enumerate(chunk)]
            events.append([t0, t1, " ".join(parts)])
    lines = []
    if clean_hook(hook):
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(end - start + 1)},Hook,,0,0,0,,{clean_hook(hook)}")
    for k, (t0, t1, text) in enumerate(events):
        if k + 1 < len(events):
            t1 = min(t1, events[k + 1][0])  # never show two caption lines at once
        if t1 - t0 >= 0.01:
            lines.append(f"Dialogue: 0,{ass_time(t0)},{ass_time(t1)},Cap,,0,0,0,,{text}")
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def face_track(cut: Path, out: Path, log=print) -> bool:
    """Crop a 9:16 window that follows the main face. Returns False if no clear face."""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(cut))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    crop_w = int(h * 9 / 16) // 2 * 2
    if crop_w >= w:
        cap.release()
        return False

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if cascade.empty():
        log("  Face finder not available (run 'Install (run once).bat' again). Using blurred background.")
        cap.release()
        return False
    step = max(int(fps // 3), 1)
    scale = 360 / h
    samples, hits, idx = {}, 0, 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step == 0:
            _, frame = cap.retrieve()
            small = cv2.resize(frame, (int(w * scale), 360))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.15, 5, minSize=(int(360 * 0.12),) * 2)
            if len(faces):
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                samples[idx] = (x + fw / 2) / scale
                hits += 1
        idx += 1
    sampled = idx // step + 1
    if hits < sampled * 0.5:
        cap.release()
        return False

    # Interpolate face centre across all frames, then smooth so the camera glides.
    keys = sorted(samples)
    centres = np.interp(np.arange(total or idx), keys, [samples[k] for k in keys])
    win = int(fps * 1.0) | 1
    pad = np.pad(centres, win // 2, mode="edge")
    smooth = np.convolve(pad, np.ones(win) / win, mode="valid")

    cap.release()
    # Write the cropped frames through FFmpeg (OpenCV's own video writer is unreliable on Windows).
    cap = cv2.VideoCapture(str(cut))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{crop_w}x{h}", "-r", f"{fps:.3f}", "-i", "-", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", str(out)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    i = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            cx = smooth[min(i, len(smooth) - 1)]
            x0 = int(min(max(cx - crop_w / 2, 0), w - crop_w))
            proc.stdin.write(np.ascontiguousarray(frame[:, x0: x0 + crop_w]).tobytes())
            i += 1
    finally:
        cap.release()
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        proc.wait()
    if proc.returncode != 0 or i == 0:
        raise RuntimeError("face crop encode failed: " + err[-300:])
    return True


def render_clip(src: Path, clip: dict, words: list[dict], out_file: Path, layout: str, log=print):
    start, end = clip["start"], clip["end"]
    hook = clip.get("hook", "")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        cut = td / "cut.mp4"
        run_ffmpeg(["-ss", f"{start:.2f}", "-i", str(src), "-t", f"{end - start:.2f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                    "-c:a", "aac", "-b:a", "192k", str(cut)])
        build_subtitles(words, start, end, td / "subs.ass", hook)

        used = "blur"
        if layout in ("Auto", "Follow face"):
            try:
                if face_track(cut, td / "face.mp4", log):
                    used = "face"
                elif layout == "Follow face":
                    log("  No clear face found, using blurred-background layout instead.")
            except Exception as e:
                log(f"  Face tracking failed ({str(e)[:120]}). Using blurred background for this clip.")

        common = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_file.resolve())]
        if used == "face":
            run_ffmpeg(["-i", "face.mp4", "-i", "cut.mp4", "-map", "0:v", "-map", "1:a",
                        "-vf", f"scale={OUT_W}:{OUT_H},ass=subs.ass", *common], cwd=td)
        else:
            fc = (f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                  f"crop={OUT_W}:{OUT_H},boxblur=25:2,eq=brightness=-0.15[bg];"
                  f"[0:v]scale={OUT_W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2-120,ass=subs.ass[v]")
            run_ffmpeg(["-i", "cut.mp4", "-filter_complex", fc, "-map", "[v]", "-map", "0:a", *common], cwd=td)
    return used


def save_render_info(out_file: Path, src: Path, clip: dict, words: list[dict], layout: str):
    """Everything needed to re-render this clip later (e.g. with a new on-screen caption)."""
    info = {"src": str(src), "start": clip["start"], "end": clip["end"], "layout": layout,
            "hook": clip.get("hook", ""),
            "words": [w for w in words if w["end"] > clip["start"] and w["start"] < clip["end"]]}
    out_file.with_suffix(".render.json").write_text(json.dumps(info), encoding="utf-8")


def rerender(out_file: Path, hook: str, log=print):
    info_file = out_file.with_suffix(".render.json")
    if not info_file.exists():
        raise RuntimeError("This clip was made before on-screen captions existed. Make it again to add one.")
    info = json.loads(info_file.read_text(encoding="utf-8"))
    src = Path(info["src"])
    if not src.exists():
        raise RuntimeError("The original video for this clip was replaced or deleted. Make the clip again.")
    require_ffmpeg()
    clip = {"start": info["start"], "end": info["end"], "hook": hook}
    tmp = out_file.with_name(out_file.stem + ".new.mp4")
    render_clip(src, clip, info["words"], tmp, info.get("layout", "Auto"), log)
    for _ in range(10):
        try:
            os.replace(tmp, out_file)
            break
        except PermissionError:
            import time
            time.sleep(1)
    else:
        raise RuntimeError("Couldn't replace the clip file. Close any player that has it open and try again.")
    info["hook"] = hook
    info_file.write_text(json.dumps(info), encoding="utf-8")
    txt = out_file.with_suffix(".txt")
    if txt.exists():
        raw = re.sub(r"ON-SCREEN:.*?\n\n", "", txt.read_text(encoding="utf-8"), flags=re.S)
        if clean_hook(hook):
            raw = raw.replace("\nSource:", f"\nON-SCREEN: {clean_hook(hook)}\n\nSource:", 1)
        txt.write_text(raw, encoding="utf-8")


# ---------------------------------------------------------------- tracker

def add_to_tracker(tracker: Path, rows: list[dict], log=print):
    new = not tracker.exists()
    try:
        with tracker.open("a", newline="", encoding="utf-8-sig") as f:
            wr = csv.DictWriter(f, fieldnames=TRACKER_COLUMNS)
            if new:
                wr.writeheader()
            wr.writerows(rows)
    except PermissionError:
        backup = tracker.with_name(f"tracker_add_{dt.datetime.now():%H%M%S}.csv")
        with backup.open("w", newline="", encoding="utf-8-sig") as f:
            wr = csv.DictWriter(f, fieldnames=TRACKER_COLUMNS)
            wr.writeheader()
            wr.writerows(rows)
        log(f"Tracker was open in Excel, so new rows went to {backup.name}. Paste them in later.")


# ---------------------------------------------------------------- full run

def run(settings: dict, base: Path, log=print, progress=None):
    global PROG
    PROG = progress or _no_progress
    require_ffmpeg()
    campaign = settings.get("campaign") or "No campaign"
    n = int(settings.get("num_clips", 5))
    min_len = float(settings.get("min_len", 20))
    max_len = float(settings.get("max_len", 60))
    start = parse_time(settings.get("start", ""))
    end = parse_time(settings.get("end", ""))

    work = base / "work" / slugify(settings["url"][-60:], 60)
    PROG("fetch", "working", "Grabbing the video...")
    src, title, creator = download(settings["url"], work, start, end, log)
    PROG("fetch", "done", title[:60], 100)
    PROG("listen", "working", "Listening...")
    tr = transcribe(src, settings.get("model", "base"), log)
    if not tr["segments"]:
        raise RuntimeError("No speech found in this video, so there's nothing to caption or pick from.")
    PROG("listen", "done", f"{len(tr['words'])} words heard", 100)
    PROG("scout", "working", "Hunting for the best moments...")

    key = (settings.get("api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    clips = []
    if key:
        try:
            clips = pick_highlights_claude(tr, n, min_len, max_len, key, title, creator,
                                           settings.get("rules", ""), log)
        except Exception as e:  # fall back rather than fail the whole run
            log(f"Claude didn't work ({e}). Using the free method instead.")
    if not clips:
        clips = pick_highlights_heuristic(src, tr, n, min_len, max_len, log)
    if not clips:
        raise RuntimeError("Couldn't find any moments that fit your clip length. Try a wider min/max.")
    PROG("scout", "done", f"Found {len(clips)} moments", 100)

    stamp = dt.datetime.now()
    out_dir = base / "output" / slugify(campaign) / f"{stamp:%Y-%m-%d_%H%M}_{slugify(title, 30)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tags = settings.get("required_text", "").strip()
    rows = []

    for i, c in enumerate(clips, 1):
        name = f"clip_{i:02d}_{slugify(c.get('title', ''), 30)}"
        out_file = out_dir / f"{name}.mp4"
        log(f"Rendering clip {i}/{len(clips)} ({fmt_time(c['start'])}-{fmt_time(c['end'])})...")
        PROG("edit", "working", f"Cutting clip {i} of {len(clips)}", int(100 * (i - 1) / len(clips)))
        used = render_clip(src, c, tr["words"], out_file, settings.get("layout", "Auto"), log)
        save_render_info(out_file, src, c, tr["words"], settings.get("layout", "Auto"))
        caption = " ".join(x for x in [c.get("caption", ""), tags] if x).strip()
        (out_dir / f"{name}.txt").write_text(
            f"TITLE: {c.get('title', '')}\n\nCAPTION (copy this):\n{caption}\n\n"
            + (f"ON-SCREEN: {clean_hook(c['hook'])}\n\n" if c.get("hook") else "") +
            f"Source: {settings['url']} at {fmt_time(c['start'] + (start or 0))}\n"
            f"Why this moment: {c.get('why', 'high energy')}\n", encoding="utf-8")
        rows.append({
            "Date": f"{stamp:%Y-%m-%d}", "Campaign": campaign,
            "Clip file": str(out_file.relative_to(base)), "Title": c.get("title", ""),
            "Caption": caption, "Status": "Ready to post",
            "Notes": f"layout={used}",
        })

    add_to_tracker(base / "output" / "tracker.csv", rows, log)
    PROG("edit", "done", f"{len(clips)} clips ready", 100)
    log(f"Done! {len(clips)} clips saved.")
    return out_dir
