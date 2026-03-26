from engine.pipeline.video_pipeline import generate_video


print("Starting AI Video Engine...\n")

topic = "space mysteries"

result = generate_video(topic)

print("\nVIDEO GENERATED\n")

print("Topic:", result["topic"])
print("Idea:", result["idea"])
print("Video Path:", result["video_path"])
print("Metadata:", result["metadata"])