import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def generate_scenes(script):

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

    return scenes[:5]