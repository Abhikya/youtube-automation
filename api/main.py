from fastapi import FastAPI
from pydantic import BaseModel

from engine.pipeline.video_pipeline import generate_video
from engine.generators.idea_generator import generate_video_ideas
from engine.approval.telegram_bot import send_video_for_approval

app = FastAPI()


class GenerateRequest(BaseModel):
    topic: str
    count: int = 1


@app.get("/")
def home():
    return {"message": "AI Video Engine API Running"}


@app.post("/generate-videos")
async def generate_videos(req: GenerateRequest):

    ideas = generate_video_ideas(req.topic)

    results = []

    for idea in ideas[:req.count]:
        result = generate_video(req.topic, idea=idea)
        results.append(result)

        # 🔥 AUTO SEND TO TELEGRAM
        await send_video_for_approval(result)

    return {"videos": results}