import os
import re
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def generate_metadata(script, *, channel_config=None):
    prompt = f"""
Generate curiosity-driven Mystery Shorts metadata for this script.
Keep the title and description faithful to the script.
Distinguish known facts from theories and label folklore where applicable.
Do not turn speculation into confirmed facts or claim the mystery is solved.

Return ONLY in this exact format:

TITLE: ...
DESCRIPTION: ...
HASHTAGS: #shorts #topic1 #topic2 #topic3
Include #shorts and 2-4 additional hashtags strongly relevant to this script.
Use lowercase, unique hashtags. Avoid #viral, #fyp and #trending.
Do not invent unrelated categories or put hashtags in the title or description.

Script:
{script}
"""

    if channel_config is not None:
        prompt = prompt.replace("Mystery Shorts", "YouTube Shorts")
        prompt += f"\nChannel: {channel_config.channel_name}\nTopic: {channel_config.topic_theme}\nContent instructions: {channel_config.content_instructions}"

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

    # Reuse generated topic tags; never invent categories when they are missing.
    normalized = ["#shorts"]
    seen = {"#shorts", "#viral", "#fyp", "#trending"}
    for tag in hashtags:
        tag = tag.lower()
        if not re.fullmatch(r"#\w+", tag) or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
        if len(normalized) == 5:
            break

    return {
        "Title": title,
        "Description": description,
        "Hashtags": normalized
    }
