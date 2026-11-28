import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def generate_scenes(script, *, content_format="shortform"):

    prompt = f"""
Break this YouTube Shorts script into 5 visual scenes.

Each scene should be a short search keyword for stock footage.

Example output:

cat staring
cat ears twitching
cat looking around room
cat blinking slowly
cat sitting calmly

Script:
{script}
"""

    if content_format not in ("shortform", "longform"):
        raise ValueError("Unknown content format")
    if content_format == "longform":
        prompt = f"""Plan 18 ordered visual segments for this long-form narration.
Return 18 short landscape stock-footage search phrases, one per line, no numbering.
Use logical sections, not one query per sentence. Vary relevant environments, objects and actions.
Stock footage is illustrative, not evidence of the actual historical event.
Narration: {script}
"""
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "user", "content": prompt}
        ]
    )

    text = response.choices[0].message.content

    scenes = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            scenes.append(line)

    return scenes[:18] if content_format == "longform" else scenes[:5]