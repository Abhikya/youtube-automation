import json
import os
from openai import OpenAI
from dotenv import load_dotenv
from config.settings import IDEA_MEMORY_FILE

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def load_memory():
    if not os.path.exists(IDEA_MEMORY_FILE):
        return {}

    with open(IDEA_MEMORY_FILE, "r") as f:
        return json.load(f)


def save_memory(memory):
    with open(IDEA_MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)


def generate_video_ideas(topic):

    memory = load_memory()

    previous_ideas = memory.get(topic, [])

    prompt = f"""
Generate 5 viral YouTube Shorts ideas about {topic}.

Rules:
- DO NOT repeat these ideas:
{previous_ideas}

- Keep each idea under 12 words
- Make them curiosity-driven
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    ideas_text = response.choices[0].message.content.strip()

    ideas = [line.strip("- ").strip() for line in ideas_text.split("\n") if line.strip()]

    # Remove duplicates
    new_ideas = [idea for idea in ideas if idea not in previous_ideas]

    # Save updated memory
    memory.setdefault(topic, []).extend(new_ideas)
    save_memory(memory)

    return new_ideas[:3]