import os
import re
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def clean_script_for_voice(script: str):

    script = re.sub(r"\[.*?\]", "", script)
    script = script.replace("Voiceover:", "")
    script = script.replace("Opening shot:", "")
    script = script.replace("Closing shot:", "")

    script = re.sub(r"\s+", " ", script)

    return script.strip()


def generate_voice(script, output_file):

    cleaned_script = clean_script_for_voice(script)

    response = client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="nova",
        input=cleaned_script
    )

    with open(output_file, "wb") as f:
        f.write(response.content)

    return output_file