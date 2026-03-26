import math
import random
from typing import List, Optional

from moviepy import (
    VideoFileClip,
    AudioFileClip,
    TextClip,
    CompositeVideoClip,
    CompositeAudioClip,
    concatenate_videoclips,
    afx,
)


def _safe_subclip(clip, start: float, end: float):
    end = min(end, clip.duration)
    start = min(start, max(0, end - 0.1))
    return clip.subclipped(start, end)


def _make_fast_cut_clips(
    clip_paths: List[str],
    target_duration: float,
    width: int,
    height: int,
) -> List[VideoFileClip]:
    if not clip_paths:
        raise ValueError("No clip paths provided.")

    segments = []
    total = 0.0
    idx = 0

    pool = clip_paths[:]
    random.shuffle(pool)

    while total < target_duration:
        clip_path = pool[idx % len(pool)]
        idx += 1

        clip = VideoFileClip(clip_path)

        cut_duration = random.uniform(0.8, 1.5)
        remaining = target_duration - total
        cut_duration = min(cut_duration, remaining)

        if clip.duration > cut_duration + 0.2:
            max_start = max(0, clip.duration - cut_duration)
            start = random.uniform(0, max_start)
            clip = _safe_subclip(clip, start, start + cut_duration)
        else:
            clip = _safe_subclip(clip, 0, min(cut_duration, clip.duration))

        clip = clip.resized(height=height)
        if clip.w < width:
            clip = clip.resized(width=width)

        clip = clip.cropped(
            x_center=clip.w / 2,
            y_center=clip.h / 2,
            width=width,
            height=height,
        )

        zoom_factor = random.uniform(1.03, 1.10)
        clip = clip.resized(
            lambda t: 1 + (zoom_factor - 1) * (t / max(clip.duration, 0.1))
        )

        segments.append(clip)
        total += clip.duration

    return segments


def _build_subtitle_clips(
    subtitles: List[str],
    total_duration: float,
    width: int,
    height: int,
    font_size: int = 65,
):
    if not subtitles:
        return []

    per_sub_duration = total_duration / len(subtitles)
    subtitle_clips = []
    current_start = 0.0

    for line in subtitles:
        txt = TextClip(
            text=line,
            font_size=font_size,
            color="white",
            stroke_color="black",
            stroke_width=4,
            method="caption",
            text_align="center",
            size=(int(width * 0.9), int(height * 0.2)),
        )

        txt = txt.with_start(current_start).with_duration(per_sub_duration)
        txt = txt.with_position(("center", int(height * 0.75)))
        subtitle_clips.append(txt)
        current_start += per_sub_duration

    return subtitle_clips


def build_short_video(
    clips: List[str],
    audio_path: str,
    output_path: str,
    width: int = 1080,
    height: int = 1920,
    subtitles: Optional[List[str]] = None,
    music_path: Optional[str] = None,
):
    if not clips:
        raise ValueError("No clips provided.")
    if not audio_path:
        raise ValueError("No audio path provided.")

    voice_audio = AudioFileClip(audio_path)
    voice_duration = voice_audio.duration

    visual_segments = _make_fast_cut_clips(
        clip_paths=clips,
        target_duration=voice_duration,
        width=width,
        height=height,
    )

    base_video = concatenate_videoclips(visual_segments, method="compose")

    if base_video.duration > voice_duration:
        base_video = base_video.subclipped(0, voice_duration)

    audio_layers = [voice_audio]

    music_audio = None
    if music_path:
        music_audio = AudioFileClip(music_path)

        if music_audio.duration < voice_duration:
            loops = math.ceil(voice_duration / music_audio.duration)
            music_audio = afx.AudioLoop(duration=voice_duration).apply(music_audio)
        else:
            music_audio = music_audio.subclipped(0, voice_duration)

        music_audio = music_audio.with_volume_scaled(0.12)
        audio_layers.append(music_audio)

    final_audio = CompositeAudioClip(audio_layers)

    subtitle_layers = _build_subtitle_clips(
        subtitles=subtitles or [],
        total_duration=voice_duration,
        width=width,
        height=height,
    )

    final_video = CompositeVideoClip([base_video, *subtitle_layers], size=(width, height))
    final_video = final_video.with_audio(final_audio)

    final_video.write_videofile(
        output_path,
        fps=24,
        codec="libx264",
        audio_codec="aac",
        preset="fast",
        bitrate="800k",
    )

    final_audio.close()
    voice_audio.close()

    if music_audio:
        music_audio.close()

    final_video.close()
    base_video.close()

    for clip in visual_segments:
        clip.close()

    return output_path