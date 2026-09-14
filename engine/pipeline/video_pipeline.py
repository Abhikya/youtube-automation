import os

from engine.uploader.youtube_uploader import upload_generated_video
from engine.approval.telegram_bot import notify_generated_video

from engine.generators.idea_generator import generate_video_ideas
from engine.generators.script_generator import generate_script
from engine.generators.metadata_generator import generate_metadata

from engine.media.voice_generator import generate_voice
from engine.media.subtitle_generator import generate_subtitles
from engine.media.music_selector import select_background_music

from engine.video.scene_generator import generate_scenes
from engine.video.pexels_fetcher import download_pexels_clips
from engine.video.video_editor import build_short_video
from datetime import datetime

from config.settings import (
    BASE_CONTENT_TOPIC,
    TEMP_AUDIO_DIR,
    TEMP_CLIPS_DIR,
    OUTPUT_VIDEO_DIR,
    VIDEO_WIDTH,
    VIDEO_HEIGHT,
)


def generate_video(topic: str = BASE_CONTENT_TOPIC, idea=None, *, publish=True):
    if topic != BASE_CONTENT_TOPIC:
        raise ValueError(f"This automation only supports {BASE_CONTENT_TOPIC!r}.")

    if idea is None:
        ideas = generate_video_ideas(topic)
        if not ideas:
            raise ValueError("No mystery ideas were returned; no media was generated.")
        idea = ideas[0]

    script = generate_script(idea)

    audio_path = os.path.join(TEMP_AUDIO_DIR, "voice.mp3")

    generate_voice(script, audio_path)

    scenes = generate_scenes(script)

    all_clips = []

    for scene in scenes:
        clips = download_pexels_clips(scene, TEMP_CLIPS_DIR, max_clips=3)
        all_clips.extend(clips)

    subtitles = generate_subtitles(script)

    music_path = select_background_music()

    safe_topic = topic.replace(" ", "_").lower()
    
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    filename = f"{safe_topic}_{timestamp}.mp4"

    output_video_path = os.path.join(OUTPUT_VIDEO_DIR, filename)

    build_short_video(
        clips=all_clips,
        audio_path=audio_path,
        output_path=output_video_path,
        width=VIDEO_WIDTH,
        height=VIDEO_HEIGHT,
        subtitles=subtitles,
        music_path=music_path,
    )

    metadata = generate_metadata(script)

    result = {
        "topic": topic,
        "idea": idea,
        "script": script,
        "video_path": output_video_path,
        "metadata": metadata,
    }

    if publish:
        result["upload"] = upload_generated_video(output_video_path, metadata)
        result["telegram"] = notify_generated_video(result)
    return result
