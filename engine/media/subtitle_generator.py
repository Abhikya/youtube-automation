import re


def clean_script(script: str):
    """
    Remove stage directions and formatting like:
    [Opening shot:], Voiceover:, emojis etc.
    """

    # remove bracketed directions
    script = re.sub(r"\[.*?\]", "", script)

    # remove Voiceover tags
    script = script.replace("Voiceover:", "")

    # remove emojis
    script = re.sub(r"[^\w\s.,!?']", "", script)

    # normalize whitespace
    script = re.sub(r"\s+", " ", script)

    return script.strip()


def split_into_chunks(words, chunk_size=4):
    """
    Split words into groups of 3–5 words for subtitles
    """

    chunks = []

    for i in range(0, len(words), chunk_size):
        part = words[i:i + chunk_size]
        line = " ".join(part).upper()
        chunks.append(line)

    return chunks


def generate_subtitles(script):
    """
    Convert narration script into subtitle lines
    """

    cleaned = clean_script(script)

    words = cleaned.split()

    subtitles = split_into_chunks(words, chunk_size=4)

    return subtitles