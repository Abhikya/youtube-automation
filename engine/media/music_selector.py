import os
import random


def select_background_music():

    music_folder = "assets/music"

    files = [
        f for f in os.listdir(music_folder)
        if f.endswith(".mp3")
    ]

    if not files:
        return None

    music_file = random.choice(files)

    return os.path.join(music_folder, music_file)

def select_longform_music(output_path):
    """Reuse an existing bed; otherwise make a quiet original ambient bed locally."""
    existing = select_background_music()
    if existing:
        return existing
    import wave
    import numpy as np
    rate, seconds = 22050, 60
    time = np.arange(rate * seconds) / rate
    samples = np.zeros_like(time)
    for index, frequency in enumerate((110.0, 164.81, 220.0, 329.63)):
        envelope = 0.6 + 0.4 * np.sin(2 * np.pi * time / (17 + index * 7) + index)
        samples += envelope * np.sin(2 * np.pi * frequency * time) / (index + 2)
    samples *= np.minimum(time / 3, 1) * np.minimum((seconds - time) / 3, 1)
    samples = (samples / max(np.max(np.abs(samples)), 1) * 8000).astype('<i2')
    with wave.open(str(output_path), 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(rate)
        stream.writeframes(samples.tobytes())
    return str(output_path)
