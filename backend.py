"""
LocalVibes Backend - Fixed for Render
=====================================
Includes cookie support for YouTube to bypass bot detection.
"""

import os, re, uuid, logging, threading
from pathlib import Path
from flask import Flask, request, jsonify
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK, ID3NoHeaderError
import requests as req

# ── Config ─────────────────────────────────────────────────────────────────────
# Ensure these are set in your Render Environment Variables
CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "YOUR_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "YOUR_SECRET")
DOWNLOAD_DIR  = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
PORT          = int(os.getenv("PORT", 8888))
COOKIE_FILE   = "cookies.txt" # The file you uploaded 

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
jobs = {}

# ── Spotify Setup ──────────────────────────────────────────────────────────────
auth_manager = SpotifyClientCredentials(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
sp = spotipy.Spotify(auth_manager=auth_manager)

def safe(name): return re.sub(r'[\\/*?:"<>|]', "", str(name))

def get_tracks(url):
    """Fetches track metadata from Spotify or YouTube."""
    if "spotify.com" in url:
        if "track" in url:
            t = sp.track(url)
            return t['name'], [{"title": t['name'], "artist": t['artists'][0]['name'], 
                               "album": t['album']['name'], "art": t['album']['images'][0]['url']}]
        elif "album" in url:
            a = sp.album(url)
            return a['name'], [{"title": t['name'], "artist": t['artists'][0]['name'], 
                                "album": a['name'], "art": a['images'][0]['url']} for t in a['tracks']['items']]
        elif "playlist" in url:
            p = sp.playlist(url)
            return p['name'], [{"title": i['track']['name'], "artist": i['track']['artists'][0]['name'], 
                                "album": i['track']['album']['name'], "art": i['track']['album']['images'][0]['url']} 
                               for i in p['tracks']['items'] if i['track']]
    
    # YouTube / YouTube Music logic with Cookies
    ydl_opts = {
        'quiet': True, 
        'extract_flat': True, 
        'cookiefile': COOKIE_FILE # <--- CRITICAL FIX [cite: 2, 3]
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        meta = ydl.extract_info(url, download=False)
        title = meta.get('title', 'YouTube Download')
        entries = meta.get('entries', [meta])
        return title, [{"title": e.get('title'), "artist": e.get('uploader', 'Unknown'), "url": e.get('url') or e.get('webpage_url')} for e in entries]

def _worker(job_id, tracks, out_dir):
    """Background downloader."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, track in enumerate(tracks):
        try:
            search_query = f"{track['title']} {track['artist']} lyrics"
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': str(out_dir / f"{safe(track['title'])}.%(ext)s"),
                'cookiefile': COOKIE_FILE, # <--- CRITICAL FIX [cite: 2, 3]
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
                'quiet': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([track.get('url') or f"ytsearch:{search_query}"])
            
            # Update job status
            jobs[job_id]["done"] += 1
            log.info(f"Downloaded: {track['title']}")
        except Exception as e:
            log.error(f"Failed {track['title']}: {e}")
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
