"""
Clip Maker app: a small local server that runs the clip pipeline and serves the dashboard
(ui/index.html) in its own window. Only reachable from this computer.
"""
from __future__ import annotations

import csv
import datetime as dt
import importlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
if sys.stdout is None or sys.stderr is None:  # running windowless (pythonw): keep library output in the log
    _sink = open(BASE / "app_log.txt", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _sink
    sys.stderr = sys.stderr or _sink
import pipeline  # noqa: E402
import poster  # noqa: E402
import requirements_reader  # noqa: E402

OUT = BASE / "output"
TRACKER = OUT / "tracker.csv"
SETTINGS = BASE / "settings.json"
UI_FILE = BASE / "ui" / "index.html"
PORT_FILE = BASE / ".port"
LOG_FILE = BASE / "app_log.txt"
TOKEN = secrets.token_urlsafe(24)
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def code_version() -> str:
    files = list(BASE.glob("*.py")) + [UI_FILE]
    return str(int(max((f.stat().st_mtime for f in files if f.exists()), default=0)))


CODE_VER = code_version()
SECRET_KEYS = ("api_key", "up_key")
STEPS = ["fetch", "listen", "scout", "edit", "post", "handoff"]
SUBMIT_MIN = 30

LOCK = threading.RLock()
STATE = {
    "steps": {k: {"state": "idle", "detail": "", "pct": None} for k in STEPS},
    "busy": None,            # "make" | "post" | "requirements" | None
    "log": deque(maxlen=500),
    "seq": 0,
    "last_ping": time.time() + 60,
    "version": 0,            # bumps when clips/tracker change
}


# ------------------------------------------------------------------ helpers

def log(msg: str):
    with LOCK:
        STATE["seq"] += 1
        STATE["log"].append({"seq": STATE["seq"], "t": time.strftime("%H:%M:%S"), "msg": str(msg)})
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def set_step(step, state, detail="", pct=None):
    with LOCK:
        STATE["steps"][step] = {"state": state, "detail": detail, "pct": pct}


def bump():
    with LOCK:
        STATE["version"] += 1


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(new: dict) -> dict:
    cur = load_settings()
    for k, v in new.items():
        if k in SECRET_KEYS:
            if v:                       # blank means "keep the saved one"
                cur[k] = v.strip()
            continue
        if k.startswith("clear_") and v:
            cur.pop(k[6:], None)
            continue
        cur[k] = v
    SETTINGS.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return cur


def public_settings() -> dict:
    s = load_settings()
    out = {k: v for k, v in s.items() if k not in SECRET_KEYS}
    for k in SECRET_KEYS:
        out["has_" + k] = bool(s.get(k))
    out.setdefault("up_platforms", ["youtube", "tiktok", "instagram", "x"])
    return out


def rel(p: Path) -> str:
    return p.resolve().relative_to(BASE).as_posix()


def from_rel(r: str) -> Path:
    p = (BASE / r).resolve()
    if OUT.resolve() not in p.parents:
        raise PermissionError("Not a clip file.")
    return p


def read_tracker() -> tuple[list[str], list[dict]]:
    if not TRACKER.exists():
        return pipeline.TRACKER_COLUMNS, []
    with TRACKER.open(newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        return (rd.fieldnames or pipeline.TRACKER_COLUMNS), list(rd)


def write_tracker(cols, rows):
    with TRACKER.open("w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(rows)


def norm(p: str) -> str:
    return (p or "").replace("\\", "/").lower()


def posted_at(row) -> str | None:
    m = re.search(r"posted=(\S+)", row.get("Notes") or "")
    return m.group(1) if m else None


def clip_list() -> list[dict]:
    _, rows = read_tracker()
    by_file = {norm(r.get("Clip file")): r for r in rows}
    clips = []
    if not OUT.exists():
        return clips
    for mp4 in sorted(OUT.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        if mp4.name.endswith(".new.mp4"):
            continue
        title, caption = poster.read_caption_file(mp4)
        hook, can_hook = "", False
        info = mp4.with_suffix(".render.json")
        if info.exists():
            can_hook = True
            try:
                hook = json.loads(info.read_text(encoding="utf-8")).get("hook", "")
            except Exception:
                pass
        r = rel(mp4)
        row = by_file.get(norm(r)) or by_file.get(norm(str(mp4.relative_to(BASE)))) or {}
        links = {p: row.get(col, "") for p, col in poster.LINK_COL.items() if row.get(col)}
        parts = mp4.relative_to(OUT).parts
        clips.append({
            "id": r, "file": mp4.name, "title": title, "caption": caption,
            "campaign": row.get("Campaign") or (parts[0] if len(parts) > 2 else ""),
            "batch": parts[-2] if len(parts) > 1 else "",
            "made": dt.datetime.fromtimestamp(mp4.stat().st_mtime).isoformat(timespec="minutes"),
            "mb": round(mp4.stat().st_size / 1e6, 1),
            "links": links, "posted_at": posted_at(row),
            "submitted": (row.get("Submitted (Y/N)") or "").strip().upper() == "Y",
            "status": row.get("Status") or "Ready to post",
            "hook": hook, "can_hook": can_hook, "v": int(mp4.stat().st_mtime),
        })
    return clips


def stats(clips) -> dict:
    posted = [c for c in clips if c["links"]]
    waiting = [c for c in posted if not c["submitted"]]
    return {"made": len(clips), "posted": len(posted), "waiting": len(waiting),
            "submitted": len([c for c in clips if c["submitted"]])}


def campaigns() -> list[dict]:
    out = []
    if not OUT.exists():
        return out
    s = load_settings()
    for req in sorted(OUT.glob("*/requirements.txt")):
        raw = req.read_text(encoding="utf-8", errors="replace")
        src = re.match(r"Source:\s*(\S+)", raw)
        body = raw.split("\n", 2)[-1]
        info = requirements_reader.parse_free(body)
        slug = req.parent.name
        n = len(list(req.parent.rglob("*.mp4")))
        name = s.get("campaign") if pipeline.slugify(s.get("campaign") or "") == slug else slug.replace("-", " ").title()
        out.append({"slug": slug, "name": name, "source": src.group(1) if src else "",
                    "tags": info["required_text"], "warnings": info["warnings"][:12],
                    "rules": info["rules"][:900], "clips": n,
                    "updated": dt.datetime.fromtimestamp(req.stat().st_mtime).isoformat(timespec="minutes")})
    return out


def handoff_refresh():
    """The last robot shows links that still need submitting, with their deadline."""
    waiting = [c for c in clip_list() if c["links"] and not c["submitted"]]
    if not waiting:
        if STATE["steps"]["handoff"]["state"] != "idle":
            set_step("handoff", "done", "Everything is submitted", 100)
        return
    now = dt.datetime.now()
    late = 0
    for c in waiting:
        if c["posted_at"]:
            try:
                if now > dt.datetime.fromisoformat(c["posted_at"]) + dt.timedelta(minutes=SUBMIT_MIN):
                    late += 1
            except ValueError:
                pass
    detail = f"{len(waiting)} link{'s' if len(waiting) != 1 else ''} to submit" + (f" ({late} past 30 min)" if late else "")
    set_step("handoff", "waiting", detail)


# ------------------------------------------------------------------ jobs

def run_job(kind, fn):
    with LOCK:
        if STATE["busy"]:
            raise RuntimeError(f"Busy with {STATE['busy']} - wait for it to finish.")
        STATE["busy"] = kind

    def worker():
        try:
            fn()
        except Exception as e:
            with LOCK:
                for k, v in STATE["steps"].items():
                    if v["state"] == "working":
                        STATE["steps"][k] = {"state": "error", "detail": str(e)[:140], "pct": None}
            log(f"Problem: {e}")
            (BASE / "last_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            log("Full details saved to last_error.txt.")
        finally:
            with LOCK:
                STATE["busy"] = None
            bump()
            handoff_refresh()

    threading.Thread(target=worker, daemon=True).start()


def reload_code():
    """Pick up updated pipeline/poster/reader code without restarting the app."""
    for m in (pipeline, poster, requirements_reader):
        try:
            importlib.reload(m)
        except Exception as e:
            log(f"Couldn't reload {m.__name__}: {e}")


def job_make(s):
    reload_code()
    for k in ["fetch", "listen", "scout", "edit"]:
        set_step(k, "idle")
    log("New batch started.")
    out = pipeline.run(s, BASE, log, progress=set_step)
    log(f"Clips are in {out.relative_to(BASE)}")


def job_post(ids):
    reload_code()
    s = load_settings()
    if not (s.get("up_key") and s.get("up_profile") and s.get("up_platforms")):
        raise RuntimeError("Set up posting first (Settings > Posting).")
    total, ok = len(ids), 0
    for i, cid in enumerate(ids, 1):
        clip = from_rel(cid)
        set_step("post", "working", f"Posting clip {i} of {total}", int(100 * (i - 1) / total))
        title, cap = poster.read_caption_file(clip)
        try:
            links = poster.post_clip(s["up_key"], s["up_profile"], clip, title, cap, s["up_platforms"], log)
        except Exception as e:
            log(f"  {clip.name}: {e}")
            continue
        poster.update_tracker(BASE, clip, links, log)
        stamp_posted(clip)
        for p, res in links.items():
            name = poster.PLATFORMS.get(p, p)
            log(f"  {name}: {res['url'] or 'posted'}" if res["ok"] else f"  {name}: FAILED - {res['msg']}")
        if any(r["ok"] for r in links.values()):
            ok += 1
        bump()
    if ok == 0:
        raise RuntimeError("None of the clips posted. Check the messages above.")
    set_step("post", "done", f"{ok} of {total} posted", 100)
    log(f"Posted {ok} clip(s). Submit the links within {SUBMIT_MIN} minutes!")


def stamp_posted(clip: Path):
    cols, rows = read_tracker()
    key = norm(rel(clip))
    for r in rows:
        if norm(r.get("Clip file")) in (key, norm(str(clip.relative_to(BASE)))):
            notes = re.sub(r"\s*posted=\S+", "", r.get("Notes") or "").strip()
            r["Notes"] = (notes + f" posted={dt.datetime.now().isoformat(timespec='minutes')}").strip()
    try:
        write_tracker(cols, rows)
    except PermissionError:
        pass


def mark_submitted(cid: str, value: bool):
    clip = from_rel(cid)
    cols, rows = read_tracker()
    key = norm(rel(clip))
    hit = False
    for r in rows:
        if norm(r.get("Clip file")) in (key, norm(str(clip.relative_to(BASE)))):
            r["Submitted (Y/N)"] = "Y" if value else ""
            r["Status"] = f"Submitted {time.strftime('%H:%M')}" if value else "Posted"
            hit = True
    if not hit:
        raise RuntimeError("That clip isn't in the tracker yet.")
    write_tracker(cols, rows)
    bump()
    handoff_refresh()


def save_caption(cid: str, title: str, caption: str):
    clip = from_rel(cid)
    txt = clip.with_suffix(".txt")
    tail = ""
    if txt.exists():
        raw = txt.read_text(encoding="utf-8")
        m = re.search(r"\n((?:ON-SCREEN:|Source:).*)$", raw, re.S)
        tail = m.group(1) if m else ""
    txt.write_text(f"TITLE: {title.strip()}\n\nCAPTION (copy this):\n{caption.strip()}\n\n{tail}".rstrip() + "\n",
                   encoding="utf-8")
    bump()


def job_hook(cid: str, hook: str):
    reload_code()
    clip = from_rel(cid)
    set_step("edit", "working", "Adding the on-screen caption", 30)
    log(f"Re-rendering {clip.name} with on-screen caption: {hook or '(none)'}")
    pipeline.rerender(clip, hook, log)
    set_step("edit", "done", "On-screen caption added", 100)
    log("Done. Watch the clip again before posting.")


def load_requirements(url: str) -> dict:
    s = load_settings()
    data, text = requirements_reader.read_requirements(url, s.get("api_key", ""), log)
    name = data.get("campaign") or s.get("campaign") or "No campaign"
    folder = OUT / pipeline.slugify(name)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "requirements.txt").write_text(f"Source: {url}\n\n{text}", encoding="utf-8")
    upd = {"req_url": url, "campaign": name}
    for k in ("required_text", "rules"):
        if data.get(k):
            upd[k] = data[k]
    for k in ("min_len", "max_len"):
        if data.get(k):
            upd[k] = str(int(data[k]))
    save_settings(upd)
    log(f"Loaded rules for {name}.")
    for w in data.get("warnings", []):
        log(f"  ! {w}")
    bump()
    return {"campaign": name, "warnings": data.get("warnings", []), **upd}


# ------------------------------------------------------------------ http

class Handler(BaseHTTPRequestHandler):
    server_version = "ClipMaker"

    def log_message(self, *a):
        pass

    # --- security: only this computer, only our window
    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _token_ok(self, q=None):
        t = self.headers.get("X-Token") or (q or {}).get("t", [""])[0]
        return secrets.compare_digest(t or "", TOKEN)

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _err(self, msg, code=400):
        self._json({"error": str(msg)}, code)

    def do_GET(self):
        if not self._host_ok():
            return self._err("forbidden", 403)
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/ping":
            return self._json({"app": "clipmaker", "base": str(BASE), "ver": CODE_VER})
        if u.path in ("/", "/index.html"):
            html = UI_FILE.read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
            b = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(b)
        if not self._token_ok(q):
            return self._err("forbidden", 403)
        try:
            if u.path == "/api/state":
                since = int(q.get("since", ["0"])[0])
                with LOCK:
                    STATE["last_ping"] = time.time()
                    logs = [e for e in STATE["log"] if e["seq"] > since]
                    return self._json({"steps": STATE["steps"], "busy": STATE["busy"], "log": logs,
                                       "seq": STATE["seq"], "version": STATE["version"]})
            if u.path == "/api/data":
                clips = clip_list()
                return self._json({"clips": clips, "stats": stats(clips), "campaigns": campaigns(),
                                   "settings": public_settings(), "submit_min": SUBMIT_MIN,
                                   "ffmpeg": bool(shutil.which("ffmpeg"))})
            if u.path.startswith("/media/"):
                return self._media(from_rel(urllib.parse.unquote(u.path[len("/media/"):])))
        except Exception as e:
            return self._err(e)
        self._err("not found", 404)

    def _media(self, p: Path):
        size = p.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = int(b) if a and b else size - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with p.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                left -= len(chunk)

    def do_POST(self):
        if not (self._host_ok() and self._token_ok()):
            return self._err("forbidden", 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/settings":
                save_settings(body)
                return self._json({"settings": public_settings()})
            if path == "/api/requirements":
                with LOCK:
                    if STATE["busy"]:
                        raise RuntimeError(f"Busy with {STATE['busy']}.")
                return self._json(load_requirements(body["url"].strip()))
            if path == "/api/make":
                s = save_settings({k: v for k, v in body.items() if k not in SECRET_KEYS})
                s = {**s, "url": body.get("url", "").strip()}
                if not s["url"]:
                    raise RuntimeError("Paste a video link first.")
                for k in ("num_clips", "min_len", "max_len"):
                    float(s.get(k) or 0)
                pipeline.parse_time(s.get("start", ""))
                pipeline.parse_time(s.get("end", ""))
                run_job("make", lambda: job_make(s))
                return self._json({"ok": True})
            if path == "/api/post":
                ids = [c for c in body.get("clips", []) if c]
                if not ids:
                    raise RuntimeError("Pick at least one clip.")
                for c in ids:
                    from_rel(c)
                run_job("post", lambda: job_post(ids))
                return self._json({"ok": True})
            if path == "/api/caption":
                save_caption(body["clip"], body.get("title", ""), body.get("caption", ""))
                return self._json({"ok": True})
            if path == "/api/hook":
                cid = body["clip"]
                from_rel(cid)
                hook = (body.get("hook") or "").strip()[:140]
                run_job("edit", lambda: job_hook(cid, hook))
                return self._json({"ok": True})
            if path == "/api/submitted":
                mark_submitted(body["clip"], bool(body.get("value", True)))
                return self._json({"ok": True})
            if path == "/api/open":
                target = OUT if body.get("what") == "output" else from_rel(body["clip"]).parent
                target.mkdir(parents=True, exist_ok=True)
                if hasattr(os, "startfile"):
                    os.startfile(target)
                return self._json({"ok": True})
            if path == "/api/reset-steps":
                for k in STEPS[:-1]:
                    set_step(k, "idle")
                handoff_refresh()
                return self._json({"ok": True})
        except PermissionError as e:
            return self._err(e, 403)
        except Exception as e:
            return self._err(e)
        self._err("not found", 404)


# ------------------------------------------------------------------ window

def open_window(url: str):
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for exe in candidates:
        if exe and os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=1440,920"])
            return
    webbrowser.open(url)


def existing_instance() -> int | None:
    try:
        port = int(PORT_FILE.read_text().strip())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            info = json.loads(r.read())
        if info.get("app") == "clipmaker" and Path(info.get("base", "")) == BASE and info.get("ver") == CODE_VER:
            return port
    except Exception:
        pass
    return None


def watchdog(srv):
    while True:
        time.sleep(15)
        with LOCK:
            idle = time.time() - STATE["last_ping"] > 90 and not STATE["busy"]
        if idle:
            log("Window closed - shutting down.")
            try:
                PORT_FILE.unlink()
            except Exception:
                pass
            os._exit(0)


class AppServer(ThreadingHTTPServer):
    """Never share a port with another copy of the app (Windows allows that by default)."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    port = existing_instance()
    if port:
        open_window(f"http://127.0.0.1:{port}/")
        return
    srv = None
    for p in list(range(8765, 8776)) + [0]:
        try:
            srv = AppServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    port = srv.server_address[1]
    PORT_FILE.write_text(str(port))
    log(f"Clip Maker started (port {port}).")
    if not shutil.which("ffmpeg"):
        log("FFmpeg not found yet. If you just ran the installer, restart your computer.")
    handoff_refresh()
    threading.Thread(target=watchdog, args=(srv,), daemon=True).start()
    if "--no-window" not in sys.argv:
        open_window(f"http://127.0.0.1:{port}/")
    srv.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG_FILE.open("a", encoding="utf-8").write(traceback.format_exc())
        raise
