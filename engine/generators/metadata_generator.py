import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def generate_metadata(script):
    prompt = f"""
Generate curiosity-driven Mystery Shorts metadata for this script.
Keep the title and description faithful to the script.
Distinguish known facts from theories and label folklore where applicable.
Do not turn speculation into confirmed facts or claim the mystery is solved.

Return ONLY in this exact format:

TITLE: ...
DESCRIPTION: ...
HASHTAGS: #tag1 #tag2 #tag3 #tag4 #tag5 #tag6 #tag7 #tag8

Script:
{script}
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    text = response.choices[0].message.content.strip()

    title = ""
    description = ""
    hashtags = []

    for line in text.splitlines():
        line = line.strip()

        if line.upper().startswith("TITLE:"):
            title = line.split(":", 1)[1].strip()

        elif line.upper().startswith("DESCRIPTION:"):
            description = line.split(":", 1)[1].strip()

        elif line.upper().startswith("HASHTAGS:"):
            hashtags_text = line.split(":", 1)[1].strip()
            hashtags = [tag for tag in hashtags_text.split() if tag.startswith("#")]

    return {
        "Title": title,
        "Description": description,
        "Hashtags": hashtags
    }