from config.channels import RuntimePaths, MINUTE_MYSTERIES
from config.settings import BASE_DIR

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
    VIDEO_WIDTH,
    VIDEO_HEIGHT,
)


def generate_video(topic=None, idea=None, *, publish=True, runtime_paths=None):
    channel = runtime_paths.channel if runtime_paths is not None else MINUTE_MYSTERIES
    topic = channel.topic_theme if topic is None else topic
    if topic != channel.topic_theme:
        raise ValueError("Topic does not match channel configuration")
    content_args = {} if channel == MINUTE_MYSTERIES else {"channel_config": channel}

    paths = runtime_paths or RuntimePaths(MINUTE_MYSTERIES, BASE_DIR)
    if publish and paths.channel != MINUTE_MYSTERIES:
        raise ValueError("Additional-channel publishing is not enabled")
    paths.prepare()

    if idea is None:
        ideas = generate_video_ideas(topic, memory_file=paths.memory, **content_args)
        if not ideas:
            raise ValueError("No mystery ideas were returned; no media was generated.")
        idea = ideas[0]

    script = generate_script(idea, **content_args)

    audio_path = str(paths.audio / "voice.mp3")

    generate_voice(script, audio_path)

    scenes = generate_scenes(script)

    all_clips = []

    for scene in scenes:
        clips = download_pexels_clips(scene, str(paths.clips), max_clips=3)
        all_clips.extend(clips)

    subtitles = generate_subtitles(script)

    music_path = select_background_music()

    safe_topic = topic.replace(" ", "_").lower()
    
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    filename = f"{safe_topic}_{timestamp}.mp4"

    output_video_path = str(paths.output / filename)

    build_short_video(
        clips=all_clips,
        audio_path=audio_path,
        output_path=output_video_path,
        width=VIDEO_WIDTH,
        height=VIDEO_HEIGHT,
        subtitles=subtitles,
        music_path=music_path,
        temp_audiofile=str(paths.audio / (filename + ".render.m4a")),
    )

    metadata = generate_metadata(script, **content_args)

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
