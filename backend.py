import os, re, uuid, logging, threading
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK
import requests as req

# ── Config ─────────────────────────────────────────────────────────────────────
CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "YOUR_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "YOUR_SECRET")
DOWNLOAD_DIR  = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
PORT          = int(os.getenv("PORT", 8888))
COOKIE_FILE   = "cookies.txt" 

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app) # This fixes the 'flask_cors' error once you add the requirements.txt
jobs = {}

# ── SERVING YOUR UI ────────────────────────────────────────────────────────────

@app.route('/')
def serve_ui():
    # This serves YOUR file. Ensure it is named exactly 'localvibes.html'
    return send_from_directory('.', 'localvibes.html')

@app.route('/<path:path>')
def serve_static(path):
    # This serves CSS, JS, or images automatically
    return send_from_directory('.', path)

# ── LOGIC ──────────────────────────────────────────────────────────────────────

sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=CLIENT_ID, client_secret=CLIENT_SECRET
))

def safe(s): return re.sub(r'[\\/*?:"<>|]', "_", s).strip()

def get_tracks(url):
    """Fetches track metadata. Note: Spotify playlists MUST be Public."""
    if "spotify.com" in url:
        if "track" in url:
            sid = re.search(r"track/([A-Za-z0-9]+)", url).group(1)
            t = sp.track(sid)
            art = ", ".join(a["name"] for a in t["artists"])
            return t["name"], [{"title": t["name"], "artist": art, "album": t["album"]["name"], "art_url": t["album"]["images"][0]["url"] if t["album"]["images"] else None, "search_query": f"{t['name']} {art}"}]
        elif "playlist" in url:
            # SERVER CANNOT SEE PRIVATE PLAYLISTS. User must click 'Share' -> 'Make Public'
            sid = re.search(r"playlist/([A-Za-z0-9]+)", url).group(1)
            p = sp.playlist(sid)
            return p['name'], [{"title": i['track']['name'], "artist": i['track']['artists'][0]['name'], "album": i['track']['album']['name'], "art_url": i['track']['album']['images'][0]['url']} for i in p['tracks']['items'] if i['track']]
    
    # YouTube Logic with Cookies
    ydl_opts = {'quiet': True, 'extract_flat': True, 'cookiefile': COOKIE_FILE}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        meta = ydl.extract_info(url, download=False)
        return meta.get('title', 'Download'), [{"title": e.get('title'), "artist": e.get('uploader', 'Unknown'), "yt_url": e.get('url') or e.get('webpage_url')} for e in meta.get('entries', [meta])]

def _worker(job_id, tracks, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, track in enumerate(tracks):
        try:
            mp3_path = out_dir / f"{safe(track['artist'])} - {safe(track['title'])}.mp3"
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': str(mp3_path.with_suffix(".%(ext)s")),
                'cookiefile': COOKIE_FILE, # CRITICAL: Bypasses the bot check
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
                'quiet': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                search = track.get('yt_url') or f"ytsearch:{track['title']} {track['artist']}"
                ydl.download([search])
            jobs[job_id]["done"] += 1
        except Exception as e:
            log.error(f"Error: {e}")
            jobs[job_id]["failed"].append(track['title'])
    jobs[job_id]["status"] = "completed"

@app.route("/download", methods=["POST"])
def download():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url: return jsonify({"error": "No URL"}), 400
    try:
        name, tracks = get_tracks(url)
        job_id = str(uuid.uuid4())[:8]
        jobs[job_id] = {"status": "running", "name": name, "total": len(tracks), "done": 0, "failed": []}
        threading.Thread(target=_worker, args=(job_id, tracks, DOWNLOAD_DIR / safe(name)), daemon=True).start()
        return jsonify({"job_id": job_id, "name": name, "total": len(tracks)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/status/<job_id>")
def status(job_id):
    return jsonify(jobs.get(job_id, {"error": "Not found"}))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
