import os
import re
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def clean_script_for_voice(script: str):

    script = re.sub(r"\[.*?\]", "", script)
    script = script.replace("Voiceover:", "")
    script = script.replace("Opening shot:", "")
    script = script.replace("Closing shot:", "")

    script = re.sub(r"\s+", " ", script)

    return script.strip()


def generate_voice(script, output_file):

    cleaned_script = clean_script_for_voice(script)

    response = client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="nova",
        input=cleaned_script
    )

    with open(output_file, "wb") as f:
        f.write(response.content)

    return output_file

def generate_longform_voice(script, output_file):
    """Bounded TTS chunks; measured chunk boundaries limit cumulative caption drift."""
    from pathlib import Path
    from moviepy import AudioFileClip, concatenate_audioclips
    cleaned = clean_script_for_voice(script)
    sentences = re.split(r'(?<=[.!?])\s+', cleaned)
    chunks, pending = [], []
    for sentence in sentences:
        words = sentence.split()
        for offset in range(0, len(words), 65):
            part = words[offset:offset + 65]
            if pending and len(pending) + len(part) > 65:
                chunks.append(' '.join(pending)); pending = []
            pending.extend(part)
    if pending:
        chunks.append(' '.join(pending))
    if not chunks:
        raise ValueError('Empty narration')
    audio, captions, cursor = [], [], 0.0
    try:
        for index, text in enumerate(chunks):
            path = str(Path(output_file).with_name(f'voice-part-{index:03d}.mp3'))
            generate_voice(text, path)
            clip = AudioFileClip(path)
            audio.append(clip)
            words = text.split()
            for start in range(0, len(words), 8):
                end = min(start + 8, len(words))
                captions.append({'text': ' '.join(words[start:end]),
                    'start': cursor + clip.duration * start / len(words),
                    'end': cursor + clip.duration * end / len(words)})
            cursor += clip.duration
        joined = concatenate_audioclips(audio)
        try:
            joined.write_audiofile(output_file, codec='libmp3lame', logger=None)
        finally:
            joined.close()
    finally:
        for clip in audio:
            clip.close()
    if not 240 <= cursor <= 420:
        raise ValueError(f'Long-form TTS duration {cursor:.1f}s is outside 4-7 minutes; do not pad or upload')
    return {'duration': cursor, 'subtitles': captions, 'chunks': len(chunks)}
