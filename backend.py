"""
LocalVibes Backend v3
======================
Supports:
  Spotify track/album  → via spotipy (client credentials, no login)
  YouTube Music playlist/track → via yt-dlp (no API key)
  YouTube playlist/track       → via yt-dlp (no API key)

Install:
    pip install flask spotipy pytubefix yt-dlp mutagen requests

Run:
    export SPOTIFY_CLIENT_ID="xxx"
    export SPOTIFY_CLIENT_SECRET="yyy"
    python backend.py
"""

import os, re, uuid, logging, threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from pytubefix import Search
import yt_dlp
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK, ID3NoHeaderError
import requests as req

# ── Config ─────────────────────────────────────────────────────────────────────
CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID",     "YOUR_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "YOUR_SECRET")
DOWNLOAD_DIR  = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
PORT          = int(os.getenv("PORT", 8888))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

app  = Flask(__name__)
jobs: dict[str, dict] = {}

# Allow browser requests from file:// and any origin
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin']  = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.before_request
def handle_options():
    if request.method == 'OPTIONS':
        from flask import Response
        return Response(status=200, headers={
            'Access-Control-Allow-Origin':  '*',
            'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
        })

sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
))


# ══════════════════════════════════════════════════════════════════════════════
# URL routing
# ══════════════════════════════════════════════════════════════════════════════

def detect_type(url: str) -> str:
    if "spotify.com/track"          in url: return "spotify_track"
    if "spotify.com/album"          in url: return "spotify_album"
    if "spotify.com/playlist"       in url: return "spotify_playlist"
    if "music.youtube.com/playlist" in url: return "ytmusic_playlist"
    if "music.youtube.com"          in url: return "ytmusic_track"
    if "youtube.com/playlist"       in url: return "yt_playlist"
    if "youtube.com" in url or "youtu.be" in url: return "yt_track"
    raise ValueError(f"Unrecognised URL: {url}")


def get_tracks(url: str) -> tuple[str, list[dict]]:
    kind = detect_type(url)
    if kind == "spotify_track":    return _sp_track(url)
    if kind == "spotify_album":    return _sp_album(url)
    if kind == "spotify_playlist":
        raise ValueError(
            "Spotify playlists are blocked by their API for dev apps. "
            "Open the playlist on music.youtube.com and paste that URL instead."
        )
    if kind in ("ytmusic_playlist", "yt_playlist"): return _yt_playlist(url)
    if kind in ("ytmusic_track",    "yt_track"):    return _yt_single(url)
    raise ValueError(f"Unhandled type: {kind}")


# ── Spotify ────────────────────────────────────────────────────────────────────

def _sp_track(url):
    sid = re.search(r"track/([A-Za-z0-9]+)", url).group(1)
    t   = sp.track(sid)
    art = t["artist"] = ", ".join(a["name"] for a in t["artists"])
    return t["name"], [{
        "title": t["name"], "artist": art,
        "album": t["album"]["name"],
        "art_url": t["album"]["images"][0]["url"] if t["album"]["images"] else None,
        "duration_ms": t["duration_ms"],
        "search_query": f"{t['name']} {art}",
    }]


def _sp_album(url):
    sid   = re.search(r"album/([A-Za-z0-9]+)", url).group(1)
    album = sp.album(sid)
    art   = album["images"][0]["url"] if album["images"] else None
    tracks, results = [], sp.album_tracks(sid)
    while results:
        for item in results["items"]:
            artists = ", ".join(a["name"] for a in item["artists"])
            tracks.append({
                "title": item["name"], "artist": artists,
                "album": album["name"], "art_url": art,
                "duration_ms": item["duration_ms"],
                "search_query": f"{item['name']} {artists}",
            })
        results = sp.next(results) if results["next"] else None
    return album["name"], tracks


# ── YouTube / YouTube Music ────────────────────────────────────────────────────

def _yt_playlist(url):
    with yt_dlp.YoutubeDL(_yt_opts({"extract_flat": True})) as ydl:
        info = ydl.extract_info(url, download=False)
    name    = info.get("title", "Playlist")
    tracks  = []
    for e in (info.get("entries") or []):
        if not e: continue
        tracks.append({
            "title":       e.get("title", "Unknown"),
            "artist":      e.get("uploader") or e.get("channel") or "Unknown",
            "album":       name,
            "art_url":     _thumb(e.get("thumbnails", [])),
            "duration_ms": int(e.get("duration") or 0) * 1000,
            "yt_url":      f"https://www.youtube.com/watch?v={e['id']}",
        })
    return name, tracks


def _yt_single(url):
    with yt_dlp.YoutubeDL(_yt_opts({"skip_download": True})) as ydl:
        info = ydl.extract_info(url, download=False)
    title  = info.get("title", "Unknown")
    artist = info.get("uploader") or info.get("channel") or "Unknown"
    return title, [{
        "title": title, "artist": artist, "album": "YouTube",
        "art_url": _thumb(info.get("thumbnails", [])),
        "duration_ms": int(info.get("duration") or 0) * 1000,
        "yt_url": url,
    }]


def _thumb(thumbs):
    if not thumbs: return None
    return max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0)).get("url")


# ══════════════════════════════════════════════════════════════════════════════
# Downloader
# ══════════════════════════════════════════════════════════════════════════════

COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')
COOKIES_OPTS = {"cookiefile": COOKIES_FILE} if os.path.exists(COOKIES_FILE) else {}

def _yt_opts(extra=None):
    opts = {"quiet": True, "no_warnings": True, **COOKIES_OPTS}
    if extra: opts.update(extra)
    return opts


def download_track(track: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    mp3_path = out_dir / f"{safe(track['artist'])} - {safe(track['title'])}.mp3"
    if mp3_path.exists():
        log.info("Exists: %s", mp3_path.name); return mp3_path

    yt_url = track.get("yt_url")
    if not yt_url:
        q       = track.get("search_query") or f"{track['title']} {track['artist']}"
        results = Search(q).videos
        if not results: raise RuntimeError(f"No results: {q}")
        yt_url  = results[0].watch_url

    ydl_opts = _yt_opts({
        "format": "bestaudio/best",
        "outtmpl": str(mp3_path.with_suffix(".%(ext)s")),
        "postprocessors": [{"key": "FFmpegExtractAudio",
                             "preferredcodec": "mp3", "preferredquality": "192"}],
    })
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([yt_url])

    _tags(mp3_path, track)
    return mp3_path


def _tags(mp3_path, track):
    try:    tags = ID3(str(mp3_path))
    except: tags = ID3()
    tags[TIT2] = TIT2(encoding=3, text=track["title"])
    tags[TPE1] = TPE1(encoding=3, text=track["artist"])
    tags[TALB] = TALB(encoding=3, text=track["album"])
    if track.get("art_url"):
        try:
            art = req.get(track["art_url"], timeout=10).content
            tags[APIC] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=art)
        except: pass
    tags.save(str(mp3_path))


# ══════════════════════════════════════════════════════════════════════════════
# Background worker
# ══════════════════════════════════════════════════════════════════════════════

def _worker(job_id, tracks, out_dir):
    job = jobs[job_id]
    job["total"] = len(tracks)
    for i, track in enumerate(tracks):
        if job.get("cancelled"): break
        try:
            path = download_track(track, out_dir)
            job["files"].append({
                "title": track["title"], "artist": track["artist"],
                "album": track["album"], "art_url": track.get("art_url"),
                "path": str(path),
            })
        except Exception as e:
            log.warning("Failed '%s': %s", track["title"], e)
            job["failed"].append({"title": track["title"], "error": str(e)})
        job["done"] = i + 1
    job["status"] = "done"
    log.info("Job %s done — %d ok, %d failed", job_id, len(job["files"]), len(job["failed"]))


# ══════════════════════════════════════════════════════════════════════════════
# API routes
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/")
def index():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'localvibes.html')
    return send_file(html_path)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "downloads": str(DOWNLOAD_DIR)})


@app.post("/metadata")
def metadata():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url: return jsonify({"error": "Missing url"}), 400
    try:
        name, tracks = get_tracks(url)
        return jsonify({"name": name, "total": len(tracks), "tracks": tracks})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/download")
def download():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url: return jsonify({"error": "Missing url"}), 400
    try:
        name, tracks = get_tracks(url)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    job_id  = str(uuid.uuid4())[:8]
    out_dir = DOWNLOAD_DIR / safe(name)
    jobs[job_id] = {
        "status": "running", "name": name,
        "total": len(tracks), "done": 0,
        "files": [], "failed": [],
    }
    threading.Thread(target=_worker, args=(job_id, tracks, out_dir), daemon=True).start()
    return jsonify({"job_id": job_id, "name": name, "total": len(tracks)})


@app.get("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job: return jsonify({"error": "Not found"}), 404
    return jsonify(job)


@app.get("/library")
def library():
    files = [{"path": str(f), "name": f.stem, "folder": f.parent.name,
               "size_mb": round(f.stat().st_size / 1_000_000, 1)}
             for f in DOWNLOAD_DIR.rglob("*.mp3")]
    return jsonify({"total": len(files), "files": files})


@app.delete("/job/<job_id>")
def cancel(job_id):
    job = jobs.get(job_id)
    if not job: return jsonify({"error": "Not found"}), 404
    job["cancelled"] = True
    return jsonify({"cancelled": True})


if __name__ == "__main__":
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    log.info("LocalVibes backend v3 → http://0.0.0.0:%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
