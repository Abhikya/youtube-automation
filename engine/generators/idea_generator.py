from config.content_policy import GLOBAL_GUARDRAIL_INSTRUCTIONS, ContentGuardrailError, validate_content
import json
import os
from openai import OpenAI
from dotenv import load_dotenv
from config.settings import BASE_CONTENT_TOPIC, IDEA_MEMORY_FILE

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def load_memory(memory_file=IDEA_MEMORY_FILE):
    if not os.path.exists(memory_file):
        return {}

    with open(memory_file, "r") as f:
        return json.load(f)


def save_memory(memory, memory_file=IDEA_MEMORY_FILE):
    with open(memory_file, "w") as f:
        json.dump(memory, f, indent=2)


def generate_video_ideas(topic=BASE_CONTENT_TOPIC, *, memory_file=None, channel_config=None):
    if topic != (channel_config.topic_theme if channel_config is not None else BASE_CONTENT_TOPIC):
        raise ValueError(f"This automation only supports {BASE_CONTENT_TOPIC!r}.")

    memory = load_memory() if memory_file is None else load_memory(memory_file)

    previous_ideas = memory.get(topic, [])

    prompt = f"""
Generate 5 Mystery Shorts ideas about {topic}.

Rules:
- DO NOT repeat these ideas:
{previous_ideas}

- Keep each idea under 12 words
- Make them curiosity-driven and suitable for YouTube Shorts
- Focus on documented unsolved mysteries
- Distinguish known facts from theories; label folklore as folklore
- Do not invent evidence or present speculation as confirmed fact
"""

    if channel_config is not None:
        if memory_file is None:
            raise ValueError("Channel generation requires an isolated idea memory path")
        prompt = prompt.replace("Mystery Shorts", "YouTube Shorts").replace(
            "Focus on documented unsolved mysteries", "Focus on documented facts within the configured topic")
        prompt += f"\nChannel: {channel_config.channel_name}\nContent instructions: {channel_config.content_instructions}"

    prompt += "\n" + GLOBAL_GUARDRAIL_INSTRUCTIONS

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    ideas_text = response.choices[0].message.content.strip()

    ideas = [line.strip("- ").strip() for line in ideas_text.split("\n") if line.strip()]

    # Remove duplicates
    new_ideas = []
    for idea in ideas:
        try:
            validate_content(idea)
        except ContentGuardrailError:
            continue
        if idea not in previous_ideas and idea not in new_ideas:
            new_ideas.append(idea)
    if ideas and not new_ideas:
        raise ContentGuardrailError("No eligible new ideas; normal generation retry required")

    # Save updated memory
    memory.setdefault(topic, []).extend(new_ideas)
    save_memory(memory) if memory_file is None else save_memory(memory, memory_file)

    return new_ideas[:3]