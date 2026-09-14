import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def generate_script(idea):

    prompt = f"""
Write a 40 second Mystery Shorts script about this unsolved mystery:

{idea}

Rules:
- Strong hook in first 3 seconds
- Maximum curiosity
- Around 80 words
- Simple storytelling
- Clearly distinguish known facts from theories and unresolved questions
- Label speculation and folklore explicitly; never present them as confirmed fact
- Do not invent evidence, quotes, or a definitive solution
- Return only the spoken narration, without stage directions or headings
"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "user", "content": prompt}
        ]
    )

    script = response.choices[0].message.content

    return script