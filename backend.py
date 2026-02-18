"""
LocalVibes Backend - Final
===========================
Dual-engine downloader:
  Engine 1: yt-dlp with ytsearch (avoids direct YouTube URL bot checks)
  Engine 2: pytubefix ANDROID_MUSIC client (bypasses when yt-dlp fails)
Supports: Spotify track/album/playlist, YouTube track/playlist, YT Music
"""

import os, re, uuid, logging, threading, zipfile, io
from pathlib import Path
from flask import Flask, request, jsonify, send_file, Response
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp
from pytubefix import Search, YouTube
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK, ID3NoHeaderError
import requests as req

# ── Config ─────────────────────────────────────────────────────────────────────
CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID",     "YOUR_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "YOUR_SECRET")
DOWNLOAD_DIR  = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
PORT          = int(os.getenv("PORT", 8888))
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE  = os.path.join(BASE_DIR, 'cookies.txt')

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

log.info("Cookies file: %s — exists: %s", COOKIES_FILE, os.path.exists(COOKIES_FILE))

app  = Flask(__name__)
jobs: dict[str, dict] = {}

# ── CORS ───────────────────────────────────────────────────────────────────────
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin']  = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.before_request
def handle_options():
    if request.method == 'OPTIONS':
        return Response(status=200, headers={
            'Access-Control-Allow-Origin':  '*',
            'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
        })

# ── Spotify ────────────────────────────────────────────────────────────────────
try:
    sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
    ))
    log.info("Spotify client initialized")
except Exception as e:
    log.error("Spotify init failed: %s", e)
    sp = None

# ── Helpers ────────────────────────────────────────────────────────────────────
def safe(s): return re.sub(r'[\\/*?:"<>|]', "_", str(s)).strip()

def _thumb(thumbs):
    if not thumbs: return None
    return max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0)).get("url")

def _yt_base_opts():
    opts = {"quiet": True, "no_warnings": True, "nocheckcertificate": True}
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts

# ── URL Detection ──────────────────────────────────────────────────────────────
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
    if kind == "spotify_playlist": return _sp_playlist(url)
    if kind in ("ytmusic_playlist", "yt_playlist"): return _yt_playlist(url)
    if kind in ("ytmusic_track",   "yt_track"):     return _yt_single(url)
    raise ValueError(f"Unhandled: {kind}")

# ── Spotify metadata ───────────────────────────────────────────────────────────
def _sp_track(url):
    sid  = re.search(r"track/([A-Za-z0-9]+)", url).group(1)
    t    = sp.track(sid)
    art  = ", ".join(a["name"] for a in t["artists"])
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

def _sp_playlist(url):
    sid      = re.search(r"playlist/([A-Za-z0-9]+)", url).group(1)
    playlist = sp.playlist(sid)
    name     = playlist["name"]
    art      = playlist["images"][0]["url"] if playlist["images"] else None
    tracks, results = [], sp.playlist_tracks(sid)
    while results:
        for item in results["items"]:
            t = item.get("track")
            if not t or not t.get("id"): continue
            artists   = ", ".join(a["name"] for a in t["artists"])
            track_art = t["album"]["images"][0]["url"] if t["album"]["images"] else art
            tracks.append({
                "title": t["name"], "artist": artists,
                "album": t["album"]["name"], "art_url": track_art,
                "duration_ms": t["duration_ms"],
                "search_query": f"{t['name']} {artists}",
            })
        results = sp.next(results) if results["next"] else None
    return name, tracks

# ── YouTube metadata ───────────────────────────────────────────────────────────
def _yt_playlist(url):
    opts = {**_yt_base_opts(), "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    name   = info.get("title", "Playlist")
    tracks = []
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
    opts = {**_yt_base_opts(), "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    title  = info.get("title", "Unknown")
    artist = info.get("uploader") or info.get("channel") or "Unknown"
    return title, [{
        "title": title, "artist": artist, "album": "YouTube",
        "art_url": _thumb(info.get("thumbnails", [])),
        "duration_ms": int(info.get("duration") or 0) * 1000,
        "yt_url": url,
    }]

# ── Dual-engine downloader ─────────────────────────────────────────────────────
def download_track(track: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    mp3_path = out_dir / f"{safe(track['artist'])} - {safe(track['title'])}.mp3"
    if mp3_path.exists():
        log.info("Exists: %s", mp3_path.name)
        return mp3_path

    # Build search query — prefer yt_url for YouTube tracks, search query for Spotify
    yt_url       = track.get("yt_url")
    search_query = track.get("search_query") or f"{track['title']} {track['artist']}"

    # ── Engine 1: yt-dlp with ytsearch (avoids direct URL bot checks) ──────────
    try:
        target = yt_url if yt_url else f"ytsearch1:{search_query}"
        ydl_opts = {
            **_yt_base_opts(),
            "format": "140/m4a/bestaudio/best",
            "outtmpl": str(mp3_path.with_suffix(".%(ext)s")),
            "postprocessors": [{"key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3", "preferredquality": "192"}],
            "default_search": "ytsearch",
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([target])
        if mp3_path.exists():
            _embed_tags(mp3_path, track)
            log.info("Engine 1 OK: %s", mp3_path.name)
            return mp3_path
    except Exception as e:
        log.warning("Engine 1 failed (%s), trying Engine 2…", e)

    # ── Engine 2: pytubefix ANDROID_MUSIC client (bypasses bot detection) ──────
    try:
        results = Search(search_query).videos
        if not results:
            raise RuntimeError(f"No search results for: {search_query}")
        yt = YouTube(
            results[0].watch_url,
            client='ANDROID_MUSIC',
            use_oauth=False,
            allow_oauth_cache=False,
        )
        stream = yt.streams.filter(only_audio=True).order_by('abr').last()
        if not stream:
            stream = yt.streams.get_audio_only()
        tmp = stream.download(output_path=str(out_dir), filename=f"_tmp_{safe(track['title'])}")
        # Convert to mp3 via mutagen-friendly rename (ffmpeg not needed for pytubefix)
        Path(tmp).rename(mp3_path)
        _embed_tags(mp3_path, track)
        log.info("Engine 2 OK: %s", mp3_path.name)
        return mp3_path
    except Exception as e:
        log.error("Engine 2 failed: %s", e)
        raise RuntimeError(f"Both engines failed for '{track['title']}': {e}")

def _embed_tags(mp3_path: Path, track: dict):
    try:
        try:    tags = ID3(str(mp3_path))
        except: tags = ID3()
        tags[TIT2] = TIT2(encoding=3, text=track.get("title", ""))
        tags[TPE1] = TPE1(encoding=3, text=track.get("artist", ""))
        tags[TALB] = TALB(encoding=3, text=track.get("album", ""))
        if track.get("art_url"):
            try:
                art = req.get(track["art_url"], timeout=10).content
                tags[APIC] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=art)
            except: pass
        tags.save(str(mp3_path))
    except Exception as e:
        log.warning("Tag embed failed: %s", e)

# ── Background worker ──────────────────────────────────────────────────────────
def _worker(job_id, tracks, out_dir):
    job = jobs[job_id]
    job["total"] = len(tracks)
    for i, track in enumerate(tracks):
        if job.get("cancelled"): break
        job["currentTrack"] = track["title"]
        try:
            path = download_track(track, out_dir)
            job["files"].append({
                "title":   track["title"],
                "artist":  track["artist"],
                "album":   track["album"],
                "art_url": track.get("art_url"),
                "path":    str(path),
            })
        except Exception as e:
            log.warning("Failed '%s': %s", track["title"], e)
            job["failed"].append({"title": track["title"], "error": str(e)})
        job["done"] = i + 1
    job["status"]       = "done"
    job["currentTrack"] = ""
    log.info("Job %s done — %d ok, %d failed", job_id, len(job["files"]), len(job["failed"]))

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return send_file(os.path.join(BASE_DIR, 'localvibes.html'))

@app.get("/health")
def health():
    return jsonify({
        "status":       "ok",
        "downloads":    str(DOWNLOAD_DIR),
        "cookies":      os.path.exists(COOKIES_FILE),
        "spotify":      sp is not None,
    })

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
        "files": [], "failed": [], "currentTrack": "",
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

@app.get("/download-zip/<job_id>")
def download_zip(job_id):
    job = jobs.get(job_id)
    if not job:             return jsonify({"error": "Not found"}), 404
    if job["status"] != "done": return jsonify({"error": "Job not done yet"}), 400
    files = job.get("files", [])
    if not files:           return jsonify({"error": "No files"}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            p = Path(f["path"])
            if p.exists(): zf.write(p, p.name)
    buf.seek(0)
    zip_name = safe(job["name"]) + ".zip"
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={zip_name}"}
    )

@app.delete("/job/<job_id>")
def cancel(job_id):
    job = jobs.get(job_id)
    if not job: return jsonify({"error": "Not found"}), 404
    job["cancelled"] = True
    return jsonify({"cancelled": True})

if __name__ == "__main__":
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    log.info("LocalVibes backend → http://0.0.0.0:%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
