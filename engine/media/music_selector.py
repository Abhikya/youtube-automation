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