from pathlib import Path
import json
import logging
import requests
from config.content_policy import validate_content
from config.channels import RuntimePaths, MINUTE_MYSTERIES, rendered_media_filename
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


def generate_video(topic=None, idea=None, *, publish=True, runtime_paths=None, resume=False):
    channel = runtime_paths.channel if runtime_paths is not None else MINUTE_MYSTERIES
    longform = channel.content_format == "longform"
    if resume and (not longform or publish):
        raise ValueError("Resume is restricted to local long-form generation")
    if longform and publish:
        raise ValueError("Long-form must use generation-only or durable scheduled preparation")
    topic = channel.topic_theme if topic is None else topic
    if topic != channel.topic_theme:
        raise ValueError("Topic does not match channel configuration")
    content_args = {} if channel == MINUTE_MYSTERIES else {"channel_config": channel}

    paths = runtime_paths or RuntimePaths(MINUTE_MYSTERIES, BASE_DIR)
    if publish and paths.channel != MINUTE_MYSTERIES:
        raise ValueError("Additional-channel publishing is not enabled")
    paths.prepare()
    progress_path = paths.audio / "generation-progress.json"
    saved = {}
    if resume:
        saved = json.loads(progress_path.read_text(encoding="utf-8"))
        if saved.get("channel_key") != channel.channel_key or saved.get("content_format") != "longform" or saved.get("topic") != topic:
            raise ValueError("Local progress channel/format mismatch")
        if saved.get("video_path") and Path(saved["video_path"] + ".json").exists():
            raise ValueError("Local video already completed; inspect it instead of rendering another")
        idea = saved["idea"]

    if idea is None:
        ideas = generate_video_ideas(topic, memory_file=paths.memory, **content_args)
        if not ideas:
            raise ValueError("No mystery ideas were returned; no media was generated.")
        idea = ideas[0]

    validate_content(idea)
    script = saved["script"] if saved else generate_script(idea, **content_args)
    validate_content(script)
    metadata = saved["metadata"] if saved else generate_metadata(script, **content_args)
    validate_content(metadata.get("Title", ""), metadata.get("Description", ""),
                     " ".join(metadata.get("Hashtags", [])))

    if longform:
        progress = {**saved, "channel_key": channel.channel_key, "content_format": "longform",
            "topic": topic, "idea": idea, "script": script, "metadata": metadata}
        progress_path = paths.audio / "generation-progress.json"
        progress_path.write_text(json.dumps(progress, indent=2), encoding="utf-8")
        logging.getLogger(__name__).info("Long-form checkpoint saved: %s", sorted(progress))
    audio_path = str(paths.audio / "voice.mp3")

    voice_details = None
    if longform:
        from engine.media.voice_generator import generate_longform_voice
        if saved.get("voice_details") and Path(audio_path).is_file():
            voice_details = saved["voice_details"]
        else:
            voice_details = generate_longform_voice(script, audio_path)
        progress.update(audio_path=audio_path, voice_details=voice_details)
        progress_path.write_text(json.dumps(progress, indent=2), encoding="utf-8")
        logging.getLogger(__name__).info("Long-form checkpoint saved: %s", sorted(progress))
    else:
        generate_voice(script, audio_path)

    scenes = saved.get("scenes") or generate_scenes(script, **({"content_format": "longform"} if longform else {}))
    if longform:
        progress["scenes"] = scenes
        progress_path.write_text(json.dumps(progress, indent=2), encoding="utf-8")

    all_clips = []

    for scene in scenes:
        try:
            clips = download_pexels_clips(scene, str(paths.clips), max_clips=2 if longform else 3,
                **({"orientation": "landscape"} if longform else {}))
        except (requests.RequestException, TimeoutError) as exc:
            if not longform:
                raise
            logging.getLogger(__name__).warning("Skipping unavailable scene (%s)", type(exc).__name__)
            continue
        all_clips.extend(clips)
        if longform:
            progress["clips"] = all_clips
            progress_path.write_text(json.dumps(progress, indent=2), encoding="utf-8")
            logging.getLogger(__name__).info("Long-form media: %s clips downloaded", len(all_clips))

    if longform and len(set(all_clips)) < 12:
        raise ValueError("Insufficient landscape footage; require at least 12 distinct clips")
    subtitles = voice_details["subtitles"] if longform else generate_subtitles(script)

    if longform:
        from engine.media.music_selector import select_longform_music
        music_path = select_longform_music(paths.audio / "ambient-bed.wav")
    else:
        music_path = select_background_music()

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    filename = rendered_media_filename(channel, timestamp)

    output_video_path = str(paths.output / filename)

    if longform:
        progress.update(scenes=scenes, clips=all_clips, music_path=music_path, video_path=output_video_path)
        progress_path.write_text(json.dumps(progress, indent=2), encoding="utf-8")
        logging.getLogger(__name__).info("Long-form checkpoint saved: %s", sorted(progress))
    build_short_video(
        clips=all_clips,
        audio_path=audio_path,
        output_path=output_video_path,
        width=1920 if longform else VIDEO_WIDTH,
        height=1080 if longform else VIDEO_HEIGHT,
        subtitles=subtitles,
        music_path=music_path,
        temp_audiofile=str(paths.audio / (filename + ".render.m4a")),
        **({"content_format": "longform"} if longform else {}),
    )

    result = {
        "topic": topic,
        "idea": idea,
        "script": script,
        "video_path": output_video_path,
        "metadata": metadata,
    }

    if longform:
        from moviepy import VideoFileClip
        with VideoFileClip(output_video_path) as rendered:
            if tuple(rendered.size) != (1920, 1080) or not 240 <= rendered.duration <= 420:
                raise ValueError("Long-form render failed dimension/duration checks")
            result.update(content_format="longform", word_count=len(script.split()),
                audio_duration=voice_details["duration"], duration=rendered.duration,
                resolution=list(rendered.size), aspect_ratio="16:9", scenes=scenes,
                media_clip_count=len(set(all_clips)), tts_chunks=voice_details["chunks"],
                music_path=music_path,
                subtitle_timing="Measured TTS chunks; proportional timing within each chunk")
            thumbnail = str(paths.output / (filename + ".jpg"))
            try:
                rendered.save_frame(thumbnail, t=min(20, rendered.duration / 2))
                result["thumbnail_path"] = thumbnail
            except Exception:
                result["thumbnail_status"] = "optional_generation_failed"
        Path(output_video_path + ".json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if publish:
        result["upload"] = upload_generated_video(output_video_path, metadata)
        result["telegram"] = notify_generated_video(result)
    return result
