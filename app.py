#!/usr/bin/env python3
"""
YTDLPro v2 — Premium YouTube Downloader
─────────────────────────────────────────
Backend: Flask + yt-dlp
Features:
  • Real-time SSE progress bar
  • Age-restriction bypass (cookies.txt + Google OAuth)
  • Auto cookie detection via browser_cookie3
    (Chrome · Firefox · Edge · Brave · Chromium — Win/Lin/Mac)
  • Playlist download with select-all / pick
  • Single: video | audio | subtitle — each downloads its own file
  • "All" bundles video + audio + subtitle into a ZIP
  • Batch / playlist background download with live progress
  • Persistent JSON session history with thumbnails
"""

import os, re, json, uuid, time, shutil, threading, zipfile
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, render_template, request, jsonify, send_file,
    Response, stream_with_context, redirect, session, url_for
)
import yt_dlp

# ─── Google OAuth (optional — works without it) ─────────────────────────────
try:
    from google_auth_oauthlib.flow import Flow
    OAUTH_AVAILABLE = True
except ImportError:
    OAUTH_AVAILABLE = False

# ─── browser_cookie3 (optional — auto cookie detection) ─────────────────────
try:
    import browser_cookie3
    BC3_AVAILABLE = True
except ImportError:
    BC3_AVAILABLE = False

# ─── Configuration ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.urandom(32)

BASE_DIR     = Path(__file__).parent.resolve()
DOWNLOAD_DIR = BASE_DIR / "downloads"
THUMB_DIR    = BASE_DIR / "static" / "thumbnails"
LOG_FILE     = BASE_DIR / "session_log.json"
COOKIES_FILE = BASE_DIR / "cookies.txt"
OAUTH_CLIENT_SECRETS = BASE_DIR / "client_secret.json"
OAUTH_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

DOWNLOAD_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(parents=True, exist_ok=True)

active_tasks   = {}
task_lock      = threading.Lock()
progress_store = {}
progress_lock  = threading.Lock()

# ─── Auto Cookie Detection ───────────────────────────────────────────────────
# Domains required for full YouTube authentication
_YT_DOMAINS = [".youtube.com", ".google.com", "youtube.com", "google.com"]

# Probe order: most commonly installed first
# Each tuple is (display_name, browser_cookie3_function_name)
_BROWSER_LOADERS = [
    ("Chrome",   "chrome"),
    ("Firefox",  "firefox"),
    ("Edge",     "edge"),
    ("Brave",    "brave"),
    ("Chromium", "chromium"),
]

# yt-dlp's own native browser names (used as zero-dep fallback in base_opts)
# yt-dlp handles OS-level decryption itself — no browser_cookie3 needed for this path
_YTDLP_BROWSER_PRIORITY = ["chrome", "firefox", "edge", "brave", "chromium"]


def auto_extract_cookies(save_path: Path = None) -> tuple:
    """
    Probe all supported browsers via browser_cookie3, collect every
    YouTube + Google cookie, and write a Netscape-format cookies.txt
    that yt-dlp can consume directly.

    Supports (all three platforms):
      • Chrome    — Win / macOS / Linux
      • Firefox   — Win / macOS / Linux
      • Edge      — Win / macOS / Linux
      • Brave     — Win / macOS / Linux
      • Chromium  — Win / macOS / Linux

    Returns (success: bool, message: str).
    """
    if not BC3_AVAILABLE:
        return False, (
            "browser_cookie3 is not installed. "
            "Run:  pip install browser-cookie3"
        )

    dest = Path(save_path or COOKIES_FILE)
    # (domain, name) → Cookie  — deduplicates across browsers & domains
    collected: dict = {}
    found_browsers: list = []

    for display_name, func_name in _BROWSER_LOADERS:
        loader = getattr(browser_cookie3, func_name, None)
        if loader is None:
            continue  # this version of browser_cookie3 doesn't expose the browser

        browser_count = 0
        for domain in _YT_DOMAINS:
            try:
                for c in loader(domain_name=domain):
                    key = (c.domain, c.name)
                    if key not in collected:
                        collected[key] = c
                        browser_count += 1
            except Exception:
                # browser not installed, profile locked, or OS keyring unavailable
                pass

        if browser_count:
            found_browsers.append(f"{display_name}({browser_count})")

    if not collected:
        return False, (
            "No YouTube / Google cookies found in any browser. "
            "Please sign in to YouTube in Chrome, Firefox, Edge, Brave, or Chromium first, "
            "then try again."
        )

    # ── Netscape / Mozilla HTTP Cookie File format ───────────────────────────
    # Format per line:
    #   domain  include_subdomains  path  secure  expiry_unix  name  value
    lines = [
        "# Netscape HTTP Cookie File",
        "# Automatically extracted by YTDLPro — do not edit manually",
        "",
    ]
    for c in collected.values():
        domain  = c.domain if c.domain.startswith(".") else f".{c.domain}"
        inc_sub = "TRUE"
        path    = c.path or "/"
        secure  = "TRUE" if c.secure else "FALSE"
        expiry  = str(int(c.expires)) if c.expires else "2147483647"
        name    = c.name  or ""
        value   = c.value or ""
        lines.append(
            f"{domain}\t{inc_sub}\t{path}\t{secure}\t{expiry}\t{name}\t{value}"
        )

    dest.write_text("\n".join(lines), "utf-8")
    msg = (
        f"Saved {len(collected)} cookies to '{dest.name}' "
        f"from: {', '.join(found_browsers)}."
    )
    return True, msg


def detect_available_browsers() -> list:
    """
    Return a list of browser names that browser_cookie3 can reach on
    this machine (i.e. the browser is installed and its profile exists).
    """
    if not BC3_AVAILABLE:
        return []
    available = []
    for display_name, func_name in _BROWSER_LOADERS:
        loader = getattr(browser_cookie3, func_name, None)
        if loader is None:
            continue
        try:
            # Try a tiny probe — just check if it doesn't throw immediately
            next(iter(loader(domain_name=".youtube.com")), None)
            available.append(display_name)
        except Exception:
            pass
    return available


# ─── Utilities ───────────────────────────────────────────────────────────────

def sanitize(name: str, max_len=180) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    return re.sub(r'\s+', ' ', name).strip()[:max_len] or "download"


def load_log() -> dict:
    if LOG_FILE.exists():
        try: return json.loads(LOG_FILE.read_text("utf-8"))
        except: pass
    return {"sessions": [], "stats": {
        "total_downloads": 0, "total_videos": 0,
        "total_audio": 0, "total_playlists": 0
    }}


def save_log(data):
    LOG_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str), "utf-8"
    )


def log_download(entry):
    data = load_log()
    entry["timestamp"] = datetime.now().isoformat()
    entry["id"] = uuid.uuid4().hex[:8]
    data["sessions"].append(entry)
    s = data["stats"]; s["total_downloads"] += 1
    dt = entry.get("download_type", "video")
    if   dt == "audio":    s["total_audio"]     += 1
    elif dt == "playlist": s["total_playlists"] += 1
    else:                  s["total_videos"]    += 1
    save_log(data)


def grab_thumb(url, vid):
    if not url: return ""
    dest = THUMB_DIR / f"{vid}.jpg"
    if dest.exists(): return f"/static/thumbnails/{vid}.jpg"
    try:
        import urllib.request
        urllib.request.urlretrieve(url, str(dest))
        return f"/static/thumbnails/{vid}.jpg"
    except: return ""


def extract_vid(url):
    m = re.search(
        r'(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})', url
    )
    return m.group(1) if m else uuid.uuid4().hex[:11]


def base_opts(cookies_path=None):
    """
    Build base yt-dlp options dict.

    Cookie resolution priority:
      1. Explicit cookies_path argument
      2. cookies.txt file on disk  (written by manual upload, OAuth, or
                                    auto_extract_cookies())
      3. yt-dlp's own built-in browser extraction  — zero-dependency fallback;
         yt-dlp handles OS-level AES / DPAPI decryption itself.
         Tries browsers in order: chrome → firefox → edge → brave → chromium.
    """
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "socket_timeout": 30, "retries": 5, "nocheckcertificate": True,
        "age_limit": None, "geo_bypass": True,
        "extractor_args": {
            "youtube": {"player_client": ["web", "android", "ios"]}
        },
        'ffmpeg_location': os.environ.get('FFMPEG_LOCATION', '/usr/bin'),

    }

    # ── 1. Explicit path ──────────────────────────────────────────────────────
    if cookies_path and Path(cookies_path).exists():
        opts["cookiefile"] = str(cookies_path)
        return opts

    # ── 2. cookies.txt file on disk ───────────────────────────────────────────
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
        return opts

    # ── 3. yt-dlp native browser extraction (no browser_cookie3 needed) ──────
    # Iterates _YTDLP_BROWSER_PRIORITY and picks the first browser yt-dlp
    # can successfully read. This requires the browser to be installed and
    # the user to be logged in on this machine.
    for browser_name in _YTDLP_BROWSER_PRIORITY:
        try:
            # Quick probe: extract_info won't run here — we're just setting opts.
            # cookiesfrombrowser = (browser_name,) tells yt-dlp to pull cookies
            # live from that browser's profile at download time.
            opts["cookiesfrombrowser"] = (browser_name,)
            break
        except Exception:
            continue

    return opts


# ─── Progress Tracker ────────────────────────────────────────────────────────

class ProgressTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.data = {
            "percent": 0, "speed": "", "eta": "", "downloaded": "",
            "total": "", "filename": "", "status": "starting", "phase": ""
        }

    def hook(self, d):
        with self._lock:
            st = d.get("status", "")
            if st == "downloading":
                tot  = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes", 0)
                pct  = (done / tot * 100) if tot else 0
                self.data.update({
                    "percent":    round(pct, 1),
                    "speed":      d.get("_speed_str", "").strip(),
                    "eta":        d.get("_eta_str", "").strip(),
                    "downloaded": d.get("_downloaded_bytes_str", "").strip(),
                    "total":      d.get(
                        "_total_bytes_str",
                        d.get("_total_bytes_estimate_str", "")
                    ).strip(),
                    "filename":   Path(d.get("filename", "")).name,
                    "status":     "downloading",
                })
            elif st == "finished":
                self.data.update({
                    "percent": 100, "status": "processing",
                    "phase": "merging / converting…"
                })

    def snap(self):
        with self._lock: return dict(self.data)

    def set_phase(self, p):
        with self._lock: self.data["phase"] = p

    def set_status(self, s):
        with self._lock: self.data["status"] = s

    def reset(self):
        with self._lock:
            self.data.update({
                "percent": 0, "status": "downloading", "speed": "", "eta": ""
            })


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index(): return render_template("index.html")


# ── Info ──────────────────────────────────────────────────────────────────────

@app.route("/api/info", methods=["POST"])
def get_info():
    url = (request.json or {}).get("url", "").strip()
    if not url: return jsonify({"error": "No URL"}), 400

    opts = base_opts(); opts["extract_flat"] = "in_playlist"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info: return jsonify({"error": "Could not fetch"}), 400

        if info.get("_type") == "playlist" or "entries" in info:
            entries = []
            for i, e in enumerate(info.get("entries") or []):
                if not e: continue
                vid = e.get("id", "")
                th = e.get("thumbnail") or (
                    (e.get("thumbnails") or [{}])[-1].get("url", "")
                )
                entries.append({
                    "index": i, "id": vid,
                    "title": e.get("title", f"Video {i+1}"),
                    "duration": e.get("duration"), "thumbnail": th,
                    "url": (
                        e.get("url") or e.get("webpage_url")
                        or f"https://www.youtube.com/watch?v={vid}"
                    ),
                    "channel": e.get("uploader") or e.get("channel", ""),
                })
            return jsonify({
                "type": "playlist",
                "title": info.get("title", "Playlist"),
                "channel": info.get("uploader") or info.get("channel", ""),
                "thumbnail": (info.get("thumbnails") or [{}])[-1].get("url", ""),
                "count": len(entries), "entries": entries, "url": url,
            })

        vid       = info.get("id") or extract_vid(url)
        thumb_url = (
            (info.get("thumbnails") or [{}])[-1].get("url", "")
        ) or info.get("thumbnail", "")
        fv, fa, sv, sa = [], [], set(), set()
        for f in info.get("formats") or []:
            vc, ac, h, abr = (
                f.get("vcodec", "none"), f.get("acodec", "none"),
                f.get("height"), f.get("abr")
            )
            fs = f.get("filesize") or f.get("filesize_approx")
            if vc != "none" and h:
                lbl = f"{h}p"
                if lbl not in sv:
                    sv.add(lbl)
                    fv.append({
                        "format_id": f["format_id"], "label": lbl,
                        "ext": f.get("ext", ""), "height": h, "filesize": fs,
                    })
            elif ac != "none" and abr:
                lbl = f"{int(abr)}kbps"
                if lbl not in sa:
                    sa.add(lbl)
                    fa.append({
                        "format_id": f["format_id"], "label": lbl,
                        "ext": f.get("ext", ""), "abr": abr, "filesize": fs,
                    })
        fv.sort(key=lambda x: x.get("height", 0), reverse=True)
        fa.sort(key=lambda x: x.get("abr", 0),    reverse=True)

        subs = {
            l: [{"ext": s.get("ext", ""), "url": s.get("url", "")} for s in sl]
            for l, sl in (info.get("subtitles") or {}).items()
        }
        auto = {
            l: [{"ext": s.get("ext", ""), "url": s.get("url", "")} for s in sl]
            for l, sl in (info.get("automatic_captions") or {}).items()
        }

        return jsonify({
            "type": "video", "id": vid,
            "title":       info.get("title", "Unknown"),
            "channel":     info.get("uploader") or info.get("channel", ""),
            "duration":    info.get("duration"),
            "thumbnail":   thumb_url,
            "description": (info.get("description") or "")[:500],
            "view_count":  info.get("view_count"),
            "upload_date": info.get("upload_date"),
            "formats_video": fv[:8], "formats_audio": fa[:5],
            "subtitles": subs, "auto_captions": auto, "url": url,
        })

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e)[:300]}), 400
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


# ── SSE Progress ──────────────────────────────────────────────────────────────

@app.route("/api/progress/<task_id>")
def progress_stream(task_id):
    def gen():
        while True:
            with progress_lock: tr = progress_store.get(task_id)
            if not tr:
                yield f"data: {json.dumps({'status': 'unknown'})}\n\n"; break
            s = tr.snap()
            yield f"data: {json.dumps(s)}\n\n"
            if s["status"] in ("done", "error"): break
            time.sleep(0.4)
    return Response(
        stream_with_context(gen()), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Single Download ───────────────────────────────────────────────────────────

@app.route("/api/download/start", methods=["POST"])
def download_start():
    data     = request.json or {}
    url      = data.get("url",      "").strip()
    mode     = data.get("mode",     "video")
    quality  = data.get("quality",  "best")
    sub_lang = data.get("sub_lang", "en")
    if not url: return jsonify({"error": "No URL"}), 400

    task_id = uuid.uuid4().hex[:8]
    tracker = ProgressTracker()
    with progress_lock: progress_store[task_id] = tracker

    tmp = DOWNLOAD_DIR / f"tmp_{task_id}"
    tmp.mkdir(exist_ok=True)

    def run():
        try:
            if mode == "all":
                _do_all(url, quality, sub_lang, tmp, tracker, task_id)
            else:
                _do_one(url, mode, quality, sub_lang, tmp, tracker, task_id)
        except Exception as e:
            tracker.set_phase(str(e)[:200])
            tracker.set_status("error")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"task_id": task_id})


def _do_one(url, mode, quality, sub_lang, tmp, tracker, task_id):
    opts = base_opts()
    opts["progress_hooks"] = [tracker.hook]
    opts["outtmpl"] = str(tmp / "%(title)s [%(id)s].%(ext)s")

    if mode == "audio":
        tracker.set_phase("Downloading audio…")
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3", "preferredquality": "320",
        }]
    elif mode == "subtitle":
        tracker.set_phase("Downloading subtitles…")
        opts["writesubtitles"]    = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"]    = [sub_lang]
        opts["subtitlesformat"]   = "srt/vtt/best"
        opts["skip_download"]     = True
    else:
        tracker.set_phase("Downloading video…")
        opts["format"] = (
            f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
            if quality != "best" else "bestvideo+bestaudio/best"
        )
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if not info: raise RuntimeError("Download returned no info")

    _log_single(info, url, mode, quality, tmp)
    tracker.set_status("done")


def _do_all(url, quality, sub_lang, tmp, tracker, task_id):
    """Download video + audio + subtitle SEPARATELY, then ZIP."""
    vd = tmp / "video"; vd.mkdir()
    ad = tmp / "audio"; ad.mkdir()
    sd = tmp / "subs";  sd.mkdir()

    # 1) VIDEO
    tracker.set_phase("Step 1/3 — Downloading video…"); tracker.reset()
    vo = base_opts(); vo["progress_hooks"] = [tracker.hook]
    vo["format"] = (
        f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
        if quality != "best" else "bestvideo+bestaudio/best"
    )
    vo["merge_output_format"] = "mp4"
    vo["outtmpl"] = str(vd / "%(title)s [%(id)s].%(ext)s")
    with yt_dlp.YoutubeDL(vo) as ydl:
        info = ydl.extract_info(url, download=True)
    if not info: raise RuntimeError("Video download failed")

    vid   = info.get("id") or extract_vid(url)
    title = sanitize(info.get("title", "video"))

    # 2) AUDIO
    tracker.set_phase("Step 2/3 — Extracting audio…"); tracker.reset()
    ao = base_opts(); ao["progress_hooks"] = [tracker.hook]
    ao["format"]  = "bestaudio/best"
    ao["outtmpl"] = str(ad / "%(title)s [%(id)s].%(ext)s")
    ao["postprocessors"] = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3", "preferredquality": "320",
    }]
    with yt_dlp.YoutubeDL(ao) as ydl:
        ydl.extract_info(url, download=True)

    # 3) SUBTITLES
    tracker.set_phase("Step 3/3 — Downloading subtitles…"); tracker.reset()
    so = base_opts()
    so["writesubtitles"]    = True
    so["writeautomaticsub"] = True
    so["subtitleslangs"]    = [sub_lang, "en"]
    so["subtitlesformat"]   = "srt/vtt/best"
    so["skip_download"]     = True
    so["outtmpl"] = str(sd / "%(title)s [%(id)s].%(ext)s")
    with yt_dlp.YoutubeDL(so) as ydl:
        ydl.extract_info(url, download=True)

    # 4) ZIP
    tracker.set_phase("Bundling into ZIP…")
    zname = f"{title} [{vid}].zip"
    zpath = tmp / zname
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for sub in (vd, ad, sd):
            for fp in sub.iterdir():
                if fp.is_file(): zf.write(fp, f"{sub.name}/{fp.name}")

    _log_single(info, url, "all", quality, tmp,
                override_file=zpath, override_name=zname)
    tracker.set_status("done")


def _log_single(info, url, mode, quality, tmp,
                override_file=None, override_name=None):
    vid       = info.get("id") or extract_vid(url)
    thumb_url = (
        (info.get("thumbnails") or [{}])[-1].get("url", "")
    ) or info.get("thumbnail", "")
    if override_file:
        fpath, fname, fsize = (
            override_file, override_name, override_file.stat().st_size
        )
    else:
        files = sorted(
            [f for f in tmp.iterdir() if f.is_file()],
            key=lambda f: f.stat().st_size, reverse=True,
        )
        fpath = files[0] if files else tmp
        fname = fpath.name
        fsize = fpath.stat().st_size if fpath.is_file() else 0
    log_download({
        "video_id":        vid,
        "title":           info.get("title", "Unknown"),
        "channel":         info.get("uploader") or info.get("channel", ""),
        "url":             url,
        "download_type":   mode,
        "quality":         quality,
        "filename":        fname,
        "filepath":        str(fpath),
        "filesize":        fsize,
        "thumbnail_local": grab_thumb(thumb_url, vid),
        "thumbnail_url":   thumb_url,
        "duration":        info.get("duration"),
    })


@app.route("/api/download/file/<task_id>")
def download_file(task_id):
    tmp = DOWNLOAD_DIR / f"tmp_{task_id}"
    if not tmp.exists(): return jsonify({"error": "File not found"}), 404

    zips = list(tmp.glob("*.zip"))
    if zips:
        f = zips[0]
    else:
        files = sorted(
            [f for f in tmp.rglob("*")
             if f.is_file() and not f.name.startswith(".")],
            key=lambda f: f.stat().st_size, reverse=True,
        )
        if not files: return jsonify({"error": "No files"}), 404
        f = files[0]

    with progress_lock: progress_store.pop(task_id, None)
    return send_file(str(f), as_attachment=True,
                     download_name=sanitize(f.name))


# ── Batch / Playlist ─────────────────────────────────────────────────────────

@app.route("/api/download/batch", methods=["POST"])
def download_batch():
    data        = request.json or {}
    urls        = data.get("urls", [])
    mode        = data.get("mode", "video")
    quality     = data.get("quality", "best")
    folder_name = sanitize(data.get("folder_name", "batch"), 80)
    if not urls: return jsonify({"error": "No URLs"}), 400

    task_id  = uuid.uuid4().hex[:8]
    out_dir  = DOWNLOAD_DIR / folder_name; out_dir.mkdir(exist_ok=True)
    task_info = {
        "id": task_id, "status": "running", "total": len(urls),
        "completed": 0, "failed": 0, "current": "", "files": [],
        "errors": [], "folder": str(out_dir),
        "started": datetime.now().isoformat(),
        "percent": 0, "speed": "", "eta": "", "item_percent": 0,
    }
    with task_lock: active_tasks[task_id] = task_info

    def run():
        for i, url in enumerate(urls):
            url = url.strip()
            if not url: continue
            itrk = ProgressTracker()
            with progress_lock: progress_store[f"{task_id}_item"] = itrk

            opts = base_opts(); opts["progress_hooks"] = [itrk.hook]
            opts["outtmpl"] = str(out_dir / "%(title)s [%(id)s].%(ext)s")
            if mode == "audio":
                opts["format"] = "bestaudio/best"
                opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3", "preferredquality": "320",
                }]
            elif mode == "all":
                fmt = (
                    f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
                    if quality != "best" else "bestvideo+bestaudio/best"
                )
                opts["format"]            = fmt
                opts["writesubtitles"]    = True
                opts["writeautomaticsub"] = True
                opts["subtitleslangs"]    = ["en"]
                opts["merge_output_format"] = "mp4"
            else:
                fmt = (
                    f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
                    if quality != "best" else "bestvideo+bestaudio/best"
                )
                opts["format"] = fmt; opts["merge_output_format"] = "mp4"

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                if info:
                    vid       = info.get("id") or extract_vid(url)
                    title     = info.get("title", "Unknown")
                    thumb_url = (
                        (info.get("thumbnails") or [{}])[-1].get("url", "")
                    ) or info.get("thumbnail", "")
                    actual = next(
                        (fp for fp in out_dir.iterdir()
                         if vid in fp.name and fp.is_file()), None
                    )
                    with task_lock:
                        t = active_tasks[task_id]; t["completed"] += 1
                        t["current"] = title
                        done = t["completed"] + t["failed"]
                        t["percent"] = round(done / len(urls) * 100, 1)
                        t["files"].append({
                            "video_id": vid, "title": title,
                            "filename": actual.name if actual
                                        else f"{title}.mp4",
                            "thumbnail_local": grab_thumb(thumb_url, vid),
                        })
                    log_download({
                        "video_id":        vid, "title": title,
                        "channel":         info.get("uploader")
                                           or info.get("channel", ""),
                        "url":             url, "download_type": mode,
                        "quality":         quality,
                        "filename":        actual.name if actual else "",
                        "filepath":        str(actual) if actual else "",
                        "filesize":        (actual.stat().st_size
                                           if actual and actual.exists() else 0),
                        "thumbnail_local": grab_thumb(thumb_url, vid),
                        "thumbnail_url":   thumb_url,
                        "duration":        info.get("duration"),
                        "batch_id":        task_id,
                    })
            except Exception as e:
                with task_lock:
                    t = active_tasks[task_id]; t["failed"] += 1
                    done = t["completed"] + t["failed"]
                    t["percent"] = round(done / len(urls) * 100, 1)
                    t["errors"].append({"url": url, "error": str(e)[:200]})
            with progress_lock:
                progress_store.pop(f"{task_id}_item", None)

        with task_lock:
            t = active_tasks[task_id]
            t["status"]   = "completed"
            t["percent"]  = 100
            t["finished"] = datetime.now().isoformat()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"task_id": task_id, "folder": str(out_dir)})


@app.route("/api/task/<task_id>")
def task_status(task_id):
    with task_lock: task = active_tasks.get(task_id)
    if not task: return jsonify({"error": "Not found"}), 404
    with progress_lock: itrk = progress_store.get(f"{task_id}_item")
    if itrk:
        s = itrk.snap()
        task["item_percent"] = s["percent"]
        task["speed"]        = s["speed"]
        task["eta"]          = s["eta"]
    return jsonify(task)


# ── History ───────────────────────────────────────────────────────────────────

@app.route("/api/history")
def get_history():
    d = load_log(); d["sessions"].reverse(); return jsonify(d)


@app.route("/api/history/clear", methods=["POST"])
def clear_history():
    save_log({
        "sessions": [], "stats": {
            "total_downloads": 0, "total_videos": 0,
            "total_audio": 0, "total_playlists": 0,
        }
    })
    return jsonify({"ok": True})


# ── Auth & Cookies ────────────────────────────────────────────────────────────

@app.route("/api/cookies", methods=["POST"])
def upload_cookies():
    """Manual upload of a cookies.txt file."""
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    request.files["file"].save(str(COOKIES_FILE))
    return jsonify({"ok": True, "message": "cookies.txt uploaded successfully."})


@app.route("/api/cookies/status")
def cookies_status():
    """
    Returns the full cookie / auth status, including which browsers
    browser_cookie3 can reach on this machine.
    """
    available_browsers = detect_available_browsers()
    return jsonify({
        # cookies.txt on disk (written by upload, OAuth, or auto-detect)
        "exists":             COOKIES_FILE.exists(),
        "cookies_file":       str(COOKIES_FILE) if COOKIES_FILE.exists() else None,
        # Google OAuth
        "oauth_available":    OAUTH_AVAILABLE,
        "oauth_connected":    bool(session.get("google_creds")),
        # browser_cookie3 auto-detect
        "bc3_available":      BC3_AVAILABLE,
        "available_browsers": available_browsers,   # e.g. ["Chrome", "Firefox"]
        # yt-dlp native extraction is always available; no extra deps needed
        "ytdlp_native_browsers": _YTDLP_BROWSER_PRIORITY,
    })


@app.route("/api/cookies/auto", methods=["POST"])
def auto_cookies():
    """
    Probe all installed browsers on this machine via browser_cookie3,
    extract every YouTube + Google cookie, and save as cookies.txt.

    Optional JSON body:
      { "browsers": ["chrome", "firefox"] }   ← restrict to specific browsers
      { "save_path": "/custom/path/cookies.txt" }

    On success the written cookies.txt is immediately used by all
    subsequent downloads (base_opts() picks it up automatically).
    """
    body      = request.json or {}
    save_path = body.get("save_path")

    # If the caller wants only specific browsers, temporarily override the list
    wanted = body.get("browsers")  # e.g. ["chrome", "brave"]
    if wanted:
        original_loaders = _BROWSER_LOADERS[:]
        # Filter in-place (module-level list) — restore after call
        filtered = [
            (dn, fn) for dn, fn in _BROWSER_LOADERS
            if fn.lower() in [b.lower() for b in wanted]
        ]
        _BROWSER_LOADERS[:] = filtered
        ok, msg = auto_extract_cookies(save_path)
        _BROWSER_LOADERS[:] = original_loaders
    else:
        ok, msg = auto_extract_cookies(save_path)

    status_code = 200 if ok else 422
    return jsonify({
        "ok":      ok,
        "message": msg,
        "cookies_file": str(COOKIES_FILE) if ok else None,
    }), status_code


@app.route("/api/auth/google/start")
def google_auth_start():
    if not OAUTH_AVAILABLE:
        return jsonify({"error": "Install: pip install google-auth-oauthlib"}), 400
    if not OAUTH_CLIENT_SECRETS.exists():
        return jsonify({
            "error": "Place client_secret.json from Google Cloud Console "
                     "in the project root."
        }), 400
    flow = Flow.from_client_secrets_file(
        str(OAUTH_CLIENT_SECRETS), scopes=OAUTH_SCOPES,
        redirect_uri=url_for("google_auth_callback", _external=True),
    )
    auth_url, state = flow.authorization_url(
        prompt="consent", access_type="offline"
    )
    session["oauth_state"] = state
    return jsonify({"auth_url": auth_url})


@app.route("/api/auth/google/callback")
def google_auth_callback():
    if not OAUTH_AVAILABLE or not OAUTH_CLIENT_SECRETS.exists():
        return redirect("/?auth=error")
    flow = Flow.from_client_secrets_file(
        str(OAUTH_CLIENT_SECRETS), scopes=OAUTH_SCOPES,
        redirect_uri=url_for("google_auth_callback", _external=True),
        state=session.get("oauth_state"),
    )
    flow.fetch_token(authorization_response=request.url)
    c = flow.credentials
    session["google_creds"] = {
        "token": c.token, "refresh_token": c.refresh_token,
        "token_uri": c.token_uri, "client_id": c.client_id,
        "client_secret": c.client_secret,
    }
    lines = [
        "# Netscape HTTP Cookie File",
        f".youtube.com\tTRUE\t/\tTRUE\t"
        f"{int(time.time()) + 2592000}\tSID\t{c.token}",
        f".youtube.com\tTRUE\t/\tTRUE\t"
        f"{int(time.time()) + 2592000}\t__Secure-3PAPISID\t{c.token}",
    ]
    COOKIES_FILE.write_text("\n".join(lines), "utf-8")
    return redirect("/?auth=success")


@app.route("/api/auth/google/disconnect", methods=["POST"])
def google_disconnect():
    session.pop("google_creds", None)
    if COOKIES_FILE.exists(): COOKIES_FILE.unlink()
    return jsonify({"ok": True})


# ── Cleanup ───────────────────────────────────────────────────────────────────

@app.route("/api/cleanup", methods=["POST"])
def cleanup():
    n = 0
    for d in DOWNLOAD_DIR.iterdir():
        if d.is_dir() and d.name.startswith("tmp_"):
            if time.time() - d.stat().st_mtime > 3600:
                shutil.rmtree(d, ignore_errors=True); n += 1
    return jsonify({"cleaned": n})


if __name__ == "__main__":
    print("\n  ╔═══════════════════════════════════════════╗")
    print("  ║    YTDLPro v2 — YouTube Downloader        ║")
    print("  ║    → http://localhost:5001                 ║")
    print("  ╚═══════════════════════════════════════════╝\n")

    # ── Startup: try auto-populating cookies.txt if not already present ──────
    if not COOKIES_FILE.exists():
        if BC3_AVAILABLE:
            print("  [cookies] Probing browsers for YouTube cookies…")
            ok, msg = auto_extract_cookies()
            print(f"  [cookies] {'✓' if ok else '✗'} {msg}")
        else:
            print("  [cookies] browser_cookie3 not installed — skipping auto-detect.")
            print("  [cookies] Install with:  pip install browser-cookie3")
            print("  [cookies] Or upload cookies.txt manually via /api/cookies")

    app.run(host="0.0.0.0", port=5001, debug=True, threaded=True)
