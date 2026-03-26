import os

# ==============================
# BASE DIRECTORY (VERY IMPORTANT)
# ==============================

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ==============================
# CHANNEL CONFIG
# ==============================

CHANNEL_NAME = "Minute Mysteries"

SHORTS_PER_DAY = 3
SHORT_DURATION = 40  # seconds

UPLOAD_TIMES = [
    "10:00",
    "14:00",
    "19:00"
]

# ==============================
# VIDEO SETTINGS
# ==============================

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920

# ==============================
# FOLDERS (ABSOLUTE PATHS)
# ==============================

# TEMP
TEMP_DIR = os.path.join(BASE_DIR, "temp")
TEMP_CLIPS_DIR = os.path.join(TEMP_DIR, "clips")
TEMP_AUDIO_DIR = os.path.join(TEMP_DIR, "audio")
TEMP_SUBTITLE_DIR = os.path.join(TEMP_DIR, "subtitles")

# OUTPUT
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
OUTPUT_VIDEO_DIR = os.path.join(OUTPUT_DIR, "videos")
APPROVED_VIDEO_DIR = os.path.join(OUTPUT_DIR, "approved")

# ASSETS
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
MUSIC_DIR = os.path.join(ASSETS_DIR, "music")
CLIPS_LIBRARY_DIR = os.path.join(ASSETS_DIR, "clips_library")

# DATA
DATA_DIR = os.path.join(BASE_DIR, "data")
IDEA_MEMORY_FILE = os.path.join(DATA_DIR, "idea_memory.json")

# ==============================
# AUTO CREATE FOLDERS
# ==============================

os.makedirs(TEMP_CLIPS_DIR, exist_ok=True)
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
os.makedirs(TEMP_SUBTITLE_DIR, exist_ok=True)

os.makedirs(OUTPUT_VIDEO_DIR, exist_ok=True)
os.makedirs(APPROVED_VIDEO_DIR, exist_ok=True)

os.makedirs(MUSIC_DIR, exist_ok=True)
os.makedirs(CLIPS_LIBRARY_DIR, exist_ok=True)

os.makedirs(DATA_DIR, exist_ok=True)

# ==============================
# DEBUG (OPTIONAL)
# ==============================

# print("BASE_DIR:", BASE_DIR)
# print("TEMP_AUDIO_DIR:", TEMP_AUDIO_DIR)