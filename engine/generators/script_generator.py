import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def generate_script(idea):

    prompt = f"""
Write a 40 second YouTube Shorts script for this topic:

{idea}

Rules:
- Strong hook in first 3 seconds
- Maximum curiosity
- Around 80 words
- Simple storytelling
"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "user", "content": prompt}
        ]
    )

    script = response.choices[0].message.content

    return script