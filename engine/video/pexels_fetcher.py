import os
import re
import shutil
import tempfile
import time
import logging
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

def _download_landscape(url, destination):
    """Two bounded transfers; publish cache bytes only after a complete response."""
    for attempt in range(2):
        temporary = None
        started = time.monotonic()
        try:
            with requests.get(url, stream=True, timeout=(15, 15)) as response:
                response.raise_for_status()
                size = 0
                with tempfile.NamedTemporaryFile(dir=CLIPS_LIBRARY_DIR, delete=False) as stream:
                    temporary = stream.name
                    for chunk in response.iter_content(64 * 1024):
                        size += len(chunk)
                        if time.monotonic() - started > 90 or size > 96 * 1024 * 1024:
                            raise TimeoutError("Landscape download exceeded its time/size budget")
                        stream.write(chunk)
                if not size:
                    raise ValueError("Empty landscape media response")
            os.replace(temporary, destination)
            return
        except (requests.RequestException, TimeoutError):
            if attempt == 1:
                raise
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


def download_pexels_clips(query, output_folder, max_clips=1, *, orientation="portrait"):
    if orientation not in ("portrait", "landscape"):
        raise ValueError("Unsupported media orientation")

    headers = {
        "Authorization": PEXELS_API_KEY
    }

    safe_query = get_safe_filename(query)
    if orientation == "landscape":
        safe_query = "landscape_" + safe_query

    os.makedirs(output_folder, exist_ok=True)
    # Cache files are read-only inputs. Serialize writers and publish complete bytes.
    with FileLock(os.path.join(CLIPS_LIBRARY_DIR, safe_query + ".lock"), timeout=600):
        cache_paths = [os.path.join(CLIPS_LIBRARY_DIR, f"{safe_query}_{i}.mp4") for i in range(max_clips)]
        if not all(os.path.isfile(path) for path in cache_paths):
            response = requests.get(PEXELS_URL, headers=headers,
                params={"query": query, "per_page": max_clips, "orientation": orientation}, timeout=(15, 90))
            response.raise_for_status()
            cache_paths = []
            for i, video in enumerate(response.json().get("videos", [])):
                cache_path = os.path.join(CLIPS_LIBRARY_DIR, f"{safe_query}_{i}.mp4")
                if not os.path.isfile(cache_path):
                    files = video["video_files"]
                    if orientation == "landscape":
                        files = [f for f in files if f.get("file_type") == "video/mp4" and f.get("width", 0) > f.get("height", 0)]
                        files.sort(key=lambda f: abs(f["width"] - 1920))
                        if not files:
                            continue
                    if orientation == "landscape":
                        try:
                            _download_landscape(files[0]["link"], cache_path)
                        except (requests.RequestException, TimeoutError) as exc:
                            logging.getLogger(__name__).warning("Skipping unavailable landscape clip (%s)", type(exc).__name__)
                            continue
                        cache_paths.append(cache_path)
                        continue
                    downloaded = requests.get(files[0]["link"], timeout=(15, 180))
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
