from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from config.settings import BASE_CONTENT_TOPIC
from engine.pipeline.video_pipeline import generate_video
from engine.generators.idea_generator import generate_video_ideas

app = FastAPI()


class GenerateRequest(BaseModel):
    topic: Literal[BASE_CONTENT_TOPIC] = BASE_CONTENT_TOPIC
    count: int = Field(default=1, ge=1, le=3)


@app.get("/")
def home():
    return {"message": "AI Video Engine API Running"}


@app.post("/generate-videos")
def generate_videos(req: GenerateRequest):
    ideas = generate_video_ideas(req.topic)
    results = []

    for idea in ideas[:req.count]:
        result = generate_video(req.topic, idea=idea)
        results.append(result)

    # Each result includes the automatic upload outcome and local video path.
    return {"videos": results}
