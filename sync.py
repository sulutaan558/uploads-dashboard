#!/usr/bin/env python
"""Collect every channel's state and publish it, encrypted, for the dashboard.

Runs on a schedule in GitHub Actions. It walks the operator's
`tiktok-yt-automation-*` repos (skipping archived ones), reads each one's
channels.yaml and committed SQLite state, optionally asks the YouTube Data API for
subscriber/view counts, and writes `docs/data.enc`.

Why encrypted: the page itself is hosted on public GitHub Pages, but the payload
names the TikTok creators being reposted and the exact publish schedule. The key
never reaches the server -- it lives in the URL fragment the operator bookmarks, and
fragments are not sent in HTTP requests.

Env:
    GITHUB_TOKEN     classic PAT with repo scope (to read the private channel repos)
    DASHBOARD_KEY    base64 32-byte AES key, stable across runs
    YOUTUBE_API_KEY  optional; without it the YouTube columns are simply absent
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

API = "https://api.github.com"
YT = "https://www.googleapis.com/youtube/v3"
DAYS = 30
GRACE_MINUTES = 100     # a slot is only "due" once its retry window has passed


def gh(path: str, **kw):
    r = requests.get(
        f"{API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"token {os.environ['GITHUB_TOKEN']}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
        **kw,
    )
    r.raise_for_status()
    return r.json()


def file_bytes(repo: str, path: str) -> bytes | None:
    try:
        d = gh(f"/repos/{repo}/contents/{path}")
    except requests.HTTPError:
        return None
    if isinstance(d, list) or d.get("encoding") != "base64":
        return None
    return base64.b64decode(d["content"])


# ---------------------------------------------------------------------------
# YouTube (optional)
# ---------------------------------------------------------------------------

def yt_channel_stats(channel_ids: list[str], key: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not key or not channel_ids:
        return out
    r = requests.get(
        f"{YT}/channels",
        params={"part": "snippet,statistics", "id": ",".join(channel_ids), "key": key},
        timeout=30,
    )
    if not r.ok:
        print(f"  youtube channels.list failed: {r.status_code} {r.text[:160]}")
        return out
    for item in r.json().get("items", []):
        s = item.get("statistics", {})
        out[item["id"]] = {
            "title": item["snippet"]["title"],
            "subs": int(s.get("subscriberCount", 0) or 0),
            "views": int(s.get("viewCount", 0) or 0),
            "videos": int(s.get("videoCount", 0) or 0),
        }
    return out


def yt_video_views(video_ids: list[str], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    if not key or not video_ids:
        return out
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i : i + 50]
        r = requests.get(
            f"{YT}/videos", params={"part": "statistics", "id": ",".join(chunk), "key": key}, timeout=30
        )
        if not r.ok:
            print(f"  youtube videos.list failed: {r.status_code}")
            break
        for item in r.json().get("items", []):
            out[item["id"]] = int(item.get("statistics", {}).get("viewCount", 0) or 0)
    return out


# ---------------------------------------------------------------------------
# One channel
# ---------------------------------------------------------------------------

def slot_due(hhmm: str, day: str, now: datetime) -> bool:
    h, m = (int(x) for x in hhmm.split(":"))
    d = datetime.strptime(day, "%Y-%m-%d").replace(hour=h, minute=m, tzinfo=timezone.utc)
    return (now - d).total_seconds() >= GRACE_MINUTES * 60


def read_channel(repo: str, cfg: dict, db_bytes: bytes | None, now: datetime) -> dict:
    cid = cfg["id"]
    slots = {int(k): str(v) for k, v in (cfg.get("slot_publish_times_utc") or {}).items()}
    out = {
        "id": cid,
        "repo": repo,
        "name": cfg.get("youtube_channel_name") or cid,
        "youtube_channel_id": cfg.get("youtube_channel_id"),
        "tiktok": cfg.get("tiktok_username"),
        "tiktok_slot2": cfg.get("tiktok_username_slot2"),
        "owner_email": cfg.get("owner_email"),
        "videos_per_day": int(cfg.get("videos_per_day") or 2),
        "enabled": bool(cfg.get("enabled", True)),
        "slots": {str(k): v for k, v in sorted(slots.items())},
        "days": [],
        "totals": {},
        "recent": [],
        "issues": [],
    }
    if not db_bytes:
        out["issues"].append("no state database yet -- this channel has never run")
        return out

    tmp = Path(tempfile.mkdtemp()) / "state.db"
    tmp.write_bytes(db_bytes)
    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row

    out["totals"] = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM posted_videos WHERE channel_id=? GROUP BY status", (cid,))}

    # uploads per day, and how many slots were actually due that day
    uploads = Counter()
    for r in conn.execute(
        "SELECT substr(updated_at,1,10) d, COUNT(*) c FROM posted_videos"
        " WHERE channel_id=? AND status='uploaded' GROUP BY d", (cid,)):
        uploads[r["d"]] = r["c"]

    # A slot is only "due" on days the channel actually existed. Without this the
    # window shows a month of misses for a channel that went live this morning --
    # 1/59 rather than 1/1 -- which reads as a broken system.
    first = conn.execute(
        "SELECT MIN(run_date) d FROM runs WHERE channel_id=?", (cid,)
    ).fetchone()["d"]

    for i in range(DAYS - 1, -1, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        live = bool(first) and day >= first
        due = sum(1 for t in slots.values() if slot_due(t, day, now)) if live else 0
        out["days"].append({"day": day, "uploaded": uploads.get(day, 0), "due": due, "live": live})
    out["since"] = first

    for r in conn.execute(
        "SELECT tiktok_id,title,view_count,youtube_video_id,youtube_url,slot,updated_at"
        " FROM posted_videos WHERE channel_id=? AND status='uploaded'"
        " ORDER BY updated_at DESC LIMIT 12", (cid,)):
        out["recent"].append({
            "title": r["title"], "url": r["youtube_url"], "yt_id": r["youtube_video_id"],
            "tiktok_views": r["view_count"], "slot": r["slot"], "at": r["updated_at"],
        })

    for r in conn.execute(
        "SELECT slot,status,detail,run_date FROM runs WHERE channel_id=? AND run_date>=?"
        " ORDER BY id DESC LIMIT 12", (cid, (now - timedelta(days=3)).strftime("%Y-%m-%d"))):
        if r["status"] in ("failed", "no_content"):
            out["issues"].append(f"{r['run_date']} slot {r['slot']}: {r['status']} — {(r['detail'] or '')[:150]}")

    pend = conn.execute(
        "SELECT COUNT(*) c FROM posted_videos WHERE channel_id=? AND status='pending_retry'", (cid,)
    ).fetchone()["c"]
    if pend:
        out["issues"].append(f"{pend} video(s) queued for retry")

    conn.close()
    return out


# ---------------------------------------------------------------------------

def build() -> dict:
    now = datetime.now(timezone.utc)
    # /user/repos, not /users/<name>/repos -- the latter only ever returns PUBLIC
    # repos, and every channel repo is private, so it silently found nothing.
    repos, page = [], 1
    while True:
        batch = gh(f"/user/repos?per_page=100&affiliation=owner&page={page}")
        if not batch:
            break
        repos += batch
        page += 1
    repos = [r for r in repos
             if r["name"].startswith("tiktok-yt-automation-") and not r["archived"]]
    print(f"found {len(repos)} channel repo(s)")

    channels = []
    for r in sorted(repos, key=lambda x: x["name"]):
        full = r["full_name"]
        raw = file_bytes(full, "channels.yaml")
        if not raw:
            print(f"  {full}: no channels.yaml, skipping")
            continue
        for cfg in (yaml.safe_load(raw.decode("utf-8")) or {}).get("channels") or []:
            db = file_bytes(full, f"data/{cfg['id']}.db")
            ch = read_channel(full, cfg, db, now)
            channels.append(ch)
            print(f"  {full}: {ch['name']} ({sum(d['uploaded'] for d in ch['days'])} uploads/{DAYS}d)")

    key = (os.getenv("YOUTUBE_API_KEY") or "").strip()
    stats = yt_channel_stats([c["youtube_channel_id"] for c in channels if c["youtube_channel_id"]], key)
    vid_views = yt_video_views(
        [v["yt_id"] for c in channels for v in c["recent"] if v.get("yt_id")], key
    )
    for c in channels:
        c["yt"] = stats.get(c["youtube_channel_id"] or "", None)
        for v in c["recent"]:
            v["yt_views"] = vid_views.get(v.get("yt_id") or "")

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_days": DAYS,
        "has_youtube_stats": bool(stats),
        "channels": channels,
    }


def encrypt(payload: dict, key_b64: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = base64.urlsafe_b64decode(key_b64 + "=" * (-len(key_b64) % 4))
    if len(key) != 32:
        raise SystemExit(f"DASHBOARD_KEY must decode to 32 bytes, got {len(key)}")
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, json.dumps(payload).encode("utf-8"), None)
    return nonce + ct          # the page splits the first 12 bytes back off


def main() -> int:
    payload = build()
    docs = Path(__file__).resolve().parent / "docs"
    docs.mkdir(exist_ok=True)

    key_b64 = (os.getenv("DASHBOARD_KEY") or "").strip()
    if not key_b64:
        raise SystemExit("DASHBOARD_KEY is not set")
    (docs / "data.enc").write_bytes(encrypt(payload, key_b64))

    # a build stamp the page polls, so an installed/pinned tab picks up new code
    (docs / "version.json").write_text(
        json.dumps({"generated_at": payload["generated_at"]}), encoding="utf-8"
    )
    print(f"\nwrote docs/data.enc ({(docs / 'data.enc').stat().st_size} bytes)"
          f" for {len(payload['channels'])} channel(s)"
          f"; youtube stats: {'yes' if payload['has_youtube_stats'] else 'no API key'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
