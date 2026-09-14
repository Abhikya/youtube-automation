"""Offline configuration diagnostics. Never prints secret values or calls APIs."""
import os
from pathlib import Path
import pickle
import json
import shutil
import subprocess

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def check_readiness(root=ROOT, emit=print):
    root = Path(root)
    load_dotenv(root / ".env")
    ready = True
    for name in ("OPENAI_API_KEY", "PEXELS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        value = os.getenv(name, "").strip()
        configured = bool(value) and not value.lower().startswith(("your_", "your-", "changeme"))
        emit(f"{name}: configured" if configured else f"{name}: missing; set {name} in the root .env")
        ready = ready and configured

    token_path = root / "token.pickle"
    token_ready = False
    if token_path.is_file():
        try:
            # Same trusted local credential file used by the existing uploader.
            with token_path.open("rb") as stream:
                credentials = pickle.load(stream)
            token_ready = bool(credentials.refresh_token and (credentials.valid or credentials.expired))
            emit("YouTube token.pickle: reusable token configured" if token_ready else
                 "YouTube token.pickle: authorization required for unattended uploads")
        except Exception:
            emit("YouTube token.pickle: unreadable/invalid; authorization required")
    else:
        emit("YouTube token.pickle: missing; run the authorization command once")
    client_path = root / "client_secret.json"
    client_ready = False
    if client_path.is_file():
        try:
            client = json.loads(client_path.read_text(encoding="utf-8"))["installed"]
            client_ready = all(isinstance(client.get(key), str) and client[key].strip()
                               for key in ("client_id", "client_secret", "auth_uri", "token_uri"))
        except Exception:
            pass
    emit("YouTube client_secret.json: configured (Desktop OAuth)" if client_ready else
         "YouTube client_secret.json: missing/invalid; expected Desktop OAuth credentials in project root")
    emit("YouTube OAuth: configured locally; remote validity not checked" if token_ready else
         "YouTube OAuth: not ready; authorize using python -m engine.uploader.youtube_uploader --authorize")
    if (root / "token.json").exists():
        emit("YouTube token.json: not supported by the existing uploader; use token.pickle")
    ready = ready and token_ready
    for relative in ("data", "output/videos", "temp/audio", "temp/clips", "temp/subtitles",
                     "assets/music", "assets/clips_library"):
        path = root / relative
        present = path.is_dir()
        emit(f"Directory {relative}: available" if present else f"Directory {relative}: missing (settings creates it at application startup)")
        ready = ready and present

    try:
        binary = os.getenv("FFMPEG_BINARY", "ffmpeg-imageio")
        if binary == "ffmpeg-imageio":
            import imageio_ffmpeg
            binary = imageio_ffmpeg.get_ffmpeg_exe()
        elif binary == "auto-detect":
            binary = shutil.which("ffmpeg")
        if not binary:
            raise RuntimeError("FFmpeg unavailable")
        completed = subprocess.run([binary, "-version"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ffmpeg_ready = completed.returncode == 0
    except Exception:
        ffmpeg_ready = False
    emit("FFmpeg: available" if ffmpeg_ready else "FFmpeg: unavailable; install project dependencies or configure FFMPEG_BINARY")
    ready = ready and ffmpeg_ready
    emit("Readiness: ready for a manual private test (API balance and remote access not checked)" if ready else
         "Readiness: not ready; resolve missing configuration above")
    return ready


if __name__ == "__main__":
    raise SystemExit(0 if check_readiness() else 1)
