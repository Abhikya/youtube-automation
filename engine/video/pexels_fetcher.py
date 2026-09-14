import os
import requests
from dotenv import load_dotenv
import re
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

    cached_files = []

    # 🔥 STEP 1: Check cache
    for i in range(max_clips):
        cache_path = os.path.join(CLIPS_LIBRARY_DIR, f"{safe_query}_{i}.mp4")
        if os.path.exists(cache_path):
            cached_files.append(cache_path)

    # If enough cached clips → return immediately
    if len(cached_files) == max_clips:
        print(f"Using cached clips for: {query}")
        return cached_files

    # 🔥 STEP 2: Otherwise download
    params = {
        "query": query,
        "per_page": max_clips,
        "orientation": "portrait"
    }

    response = requests.get(PEXELS_URL, headers=headers, params=params, timeout=(15, 90))
    response.raise_for_status()
    data = response.json()

    clips = []

    for i, video in enumerate(data.get("videos", [])):

        video_url = video["video_files"][0]["link"]

        file_name = f"{safe_query}_{i}.mp4"

        cache_path = os.path.join(CLIPS_LIBRARY_DIR, file_name)
        output_path = os.path.join(output_folder, file_name)

        # Download clip
        video_response = requests.get(video_url, timeout=(15, 180))
        video_response.raise_for_status()
        video_data = video_response.content

        # Save to cache
        with open(cache_path, "wb") as f:
            f.write(video_data)

        # Copy to temp folder
        with open(output_path, "wb") as f:
            f.write(video_data)

        clips.append(output_path)

    return clips