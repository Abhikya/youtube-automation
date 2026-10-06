import os
import re
import shutil
import tempfile
from filelock import FileLock
import requests
from dotenv import load_dotenv
from config.settings import CLIPS_LIBRARY_DIR


load_dotenv()

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
PEXELS_URL = "https://api.pexels.com/videos/search"

def get_safe_filename(text):
    # Replace spaces with underscores and remove special characters
    safe_query = re.sub(r'[^a-zA-Z0-9_]', '', text.replace(" ", "_"))[:50]
    return safe_query

def download_pexels_clips(query, output_folder, max_clips=1):

    headers = {
        "Authorization": PEXELS_API_KEY
    }

    safe_query = get_safe_filename(query)

    os.makedirs(output_folder, exist_ok=True)
    # Cache files are read-only inputs. Serialize writers and publish complete bytes.
    with FileLock(os.path.join(CLIPS_LIBRARY_DIR, safe_query + ".lock"), timeout=600):
        cache_paths = [os.path.join(CLIPS_LIBRARY_DIR, f"{safe_query}_{i}.mp4") for i in range(max_clips)]
        if not all(os.path.isfile(path) for path in cache_paths):
            response = requests.get(PEXELS_URL, headers=headers,
                params={"query": query, "per_page": max_clips, "orientation": "portrait"}, timeout=(15, 90))
            response.raise_for_status()
            cache_paths = []
            for i, video in enumerate(response.json().get("videos", [])):
                cache_path = os.path.join(CLIPS_LIBRARY_DIR, f"{safe_query}_{i}.mp4")
                if not os.path.isfile(cache_path):
                    downloaded = requests.get(video["video_files"][0]["link"], timeout=(15, 180))
                    downloaded.raise_for_status()
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(dir=CLIPS_LIBRARY_DIR, delete=False) as stream:
                            temporary = stream.name
                            stream.write(downloaded.content)
                        os.replace(temporary, cache_path)
                    finally:
                        if temporary and os.path.exists(temporary):
                            os.unlink(temporary)
                cache_paths.append(cache_path)
        clips = []
        for cache_path in cache_paths:
            working = os.path.join(output_folder, os.path.basename(cache_path))
            if os.path.realpath(working) == os.path.realpath(cache_path):
                raise ValueError("Working clips must be separate from shared cache")
            shutil.copyfile(cache_path, working)
            clips.append(working)
        return clips
