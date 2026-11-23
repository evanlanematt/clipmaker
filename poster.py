"""
Posts finished clips to YouTube Shorts / TikTok / Instagram Reels / X through Upload-Post
(upload-post.com), then records the live links in the tracker.
"""
from __future__ import annotations

import csv
import datetime as dt
import re
import time
import uuid
from pathlib import Path

API = "https://api.upload-post.com/api"
PLATFORMS = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram", "x": "X"}
LINK_COL = {"youtube": "YouTube link", "tiktok": "TikTok link", "instagram": "Instagram link", "x": "X link"}


def read_caption_file(video: Path) -> tuple[str, str]:
    """Returns (title, caption) from the clip's .txt file."""
    txt = video.with_suffix(".txt")
    if not txt.exists():
        return video.stem, ""
    raw = txt.read_text(encoding="utf-8")
    title = re.search(r"^TITLE:\s*(.*)$", raw, re.M)
    cap = re.search(r"CAPTION \(copy this\):\s*\n(.*?)(?:\n\s*\n|\nSource:|\Z)", raw, re.S)
    return (title.group(1).strip() if title else video.stem), (cap.group(1).strip() if cap else "")


def missing_required(caption: str, required: str) -> list[str]:
    need = [t for t in required.split() if t.startswith(("#", "@"))]
    low = caption.lower()
    return [t for t in need if t.lower() not in low]


def _headers(key):
    return {"Authorization": f"Apikey {key}"}


def _extract_links(results) -> dict:
    """Handles both the sync dict-of-platforms shape and the status list shape."""
    out = {}
    items = results.items() if isinstance(results, dict) else \
        [((r or {}).get("platform"), r) for r in (results or [])]
    for plat, r in items:
        if not plat or not isinstance(r, dict):
            continue
        plat = str(plat).lower()
        url = r.get("url") or r.get("post_url") or r.get("permalink") or ""
        if r.get("skipped"):
            out[plat] = {"ok": False, "url": "", "msg": "skipped: " + str(r.get("skip_reason") or "account not connected")}
        elif r.get("success") or url:
            out[plat] = {"ok": True, "url": url, "msg": r.get("message") or ""}
        else:
            out[plat] = {"ok": False, "url": "", "msg": str(r.get("error") or r.get("message") or "failed")}
    return out


def post_clip(api_key: str, profile: str, video: Path, title: str, caption: str,
              platforms: list[str], log=print, timeout_min: int = 20) -> dict:
    import requests

    yt_title = (title or video.stem)[:100]
    data = [
        ("user", profile),
        ("title", caption or title),            # caption text for TikTok / Instagram / X
        ("youtube_title", yt_title),
        ("youtube_description", caption),
        ("privacyStatus", "public"),
        ("selfDeclaredMadeForKids", "false"),
        ("privacy_level", "PUBLIC_TO_EVERYONE"),
        ("media_type", "REELS"),
        ("x_title", (caption or title)[:280]),
        ("async_upload", "true"),
        ("external_id", video.name[:255]),
    ]
    for p in platforms:
        data.append(("platform[]", p))
    request_id = str(uuid.uuid4())
    data.append(("request_id", request_id))

    log(f"  Uploading {video.name} to {', '.join(PLATFORMS[p] for p in platforms)}...")
    with video.open("rb") as f:
        r = requests.post(f"{API}/upload", headers={**_headers(api_key), "Idempotency-Key": request_id},
                          data=data, files={"video": (video.name, f, "video/mp4")}, timeout=600)
    if r.status_code == 401:
        raise RuntimeError("Upload-Post says the API key is wrong or expired. Check Posting settings.")
    if r.status_code == 403:
        raise RuntimeError(f"Upload-Post refused this on your plan (TikTok needs a paid plan): {r.text[:300]}")
    if r.status_code == 404:
        raise RuntimeError("Upload-Post couldn't find that profile name. Check Posting settings.")
    if r.status_code == 429:
        raise RuntimeError("Upload-Post monthly or daily limit reached.")
    if r.status_code >= 400:
        raise RuntimeError(f"Upload-Post error {r.status_code}: {r.text[:400]}")
    body = r.json()

    if isinstance(body.get("results"), dict) and body["results"]:
        return _extract_links(body["results"])

    rid = body.get("request_id") or request_id
    deadline = time.time() + timeout_min * 60
    last = None
    while time.time() < deadline:
        time.sleep(6)
        s = requests.get(f"{API}/uploadposts/status", headers=_headers(api_key),
                         params={"request_id": rid}, timeout=60).json()
        status = s.get("status")
        if status != last:
            log(f"  ...{status} ({s.get('completed', 0)}/{s.get('total', len(platforms))} platforms)")
            last = status
        if status in ("completed", "failed", "not_found"):
            links = _extract_links(s.get("results"))
            if status != "completed" and not links:
                raise RuntimeError(s.get("message") or f"Upload {status}.")
            return links
    raise RuntimeError("Still processing after 20 minutes. Check your Upload-Post dashboard.")


def update_tracker(base: Path, video: Path, links: dict, log=print):
    tracker = base / "output" / "tracker.csv"
    rel = str(video.relative_to(base)) if video.is_relative_to(base) else str(video)
    now = dt.datetime.now()
    status = f"Posted {now:%H:%M} - submit by {(now + dt.timedelta(minutes=30)):%H:%M}"
    rows, cols = [], None
    if tracker.exists():
        with tracker.open(newline="", encoding="utf-8-sig") as f:
            rd = csv.DictReader(f)
            cols = rd.fieldnames
            rows = list(rd)
    import pipeline
    cols = cols or pipeline.TRACKER_COLUMNS
    row = next((r for r in rows if r.get("Clip file") == rel), None)
    if row is None:
        row = {c: "" for c in cols}
        row.update({"Date": f"{now:%Y-%m-%d}", "Clip file": rel})
        rows.append(row)
    for p, res in links.items():
        if res["ok"] and p in LINK_COL:
            row[LINK_COL[p]] = res["url"] or "posted (link pending - check profile)"
    row["Status"] = status
    try:
        with tracker.open("w", newline="", encoding="utf-8-sig") as f:
            wr = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            wr.writeheader()
            wr.writerows(rows)
    except PermissionError:
        log("  Tracker is open in Excel, so links were only saved next to the clip.")
    with video.with_name(video.stem + "_links.txt").open("w", encoding="utf-8") as f:
        f.write(f"{status}\n\n")
        for p, res in links.items():
            f.write(f"{PLATFORMS.get(p, p)}: {res['url'] if res['ok'] else 'FAILED - ' + res['msg']}\n")
