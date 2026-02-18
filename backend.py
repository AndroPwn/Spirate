import os
import re
import uuid
import logging
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK
import requests as req

# ── Config ─────────────────────────────────────────────────────────────────────
# These MUST be set in Render -> Dashboard -> Settings -> Environment Variables
CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "your_id_here")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "your_secret_here")
DOWNLOAD_DIR  = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
PORT          = int(os.getenv("PORT", 8888))
COOKIE_FILE   = "cookies.txt" 

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app) 
jobs = {}

# ── SERVING THE UI (Fixes "Not Found") ────────────────────────────────────────

@app.route('/')
def serve_ui():
    # This serves YOUR LocalVibes interface
    # Ensure the file is named exactly 'localvibes.html' in your folder
    return send_from_directory('.', 'localvibes.html')

@app.route('/<path:path>')
def serve_static(path):
    # This serves CSS, JS, or images automatically
    return send_from_directory('.', path)

# ── CORE LOGIC ────────────────────────────────────────────────────────────────

auth_manager = SpotifyClientCredentials(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
sp = spotipy.Spotify(auth_manager=auth_manager)

def safe(name): return re.sub(r'[\\/*?:"<>|]', "_", str(name))

def get_tracks(url):
    """Fetches track metadata."""
    if "spotify.com" in url:
        # NOTE: Playlists MUST BE PUBLIC. The server is a bot, not "You".
        if "track" in url:
            t = sp.track(url)
            return t['name'], [{"title": t['name'], "artist": t['artists'][0]['name'], "album": t['album']['name'], "art": t['album']['images'][0]['url']}]
        elif "playlist" in url:
            p = sp.playlist(url)
            return p['name'], [{"title": i['track']['name'], "artist": i['track']['artists'][0]['name'], "album": i['track']['album']['name'], "art": i['track']['album']['images'][0]['url']} for i in p['tracks']['items'] if i['track']]
    
    # YouTube Logic with your Cookies
    ydl_opts = {'quiet': True, 'extract_flat': True, 'cookiefile': COOKIE_FILE}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        meta = ydl.extract_info(url, download=False)
        title = meta.get('title', 'Download')
        entries = meta.get('entries', [meta])
        return title, [{"title": e.get('title'), "artist": e.get('uploader', 'Unknown'), "url": e.get('url') or e.get('webpage_url')} for e in entries]

def _worker(job_id, tracks, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, track in enumerate(tracks):
        try:
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': str(out_dir / f"{safe(track['title'])}.%(ext)s"),
                'cookiefile': COOKIE_FILE, # CRITICAL: Bypasses the "Sign In" bot check
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
                'quiet': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                search = track.get('url') or f"ytsearch:{track['title']} {track['artist']} lyrics"
                ydl.download([search])
            jobs[job_id]["done"] += 1
        except Exception as e:
            log.error(f"Error: {e}")
            jobs[job_id]["failed"].append(track['title'])
    jobs[job_id]["status"] = "completed"

@app.route("/download", methods=["POST"])
def download():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url: return jsonify({"error": "No URL provided"}), 400
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
