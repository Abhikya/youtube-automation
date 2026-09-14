from engine.pipeline.video_pipeline import generate_video


print("Starting AI Video Engine...\n")

result = generate_video()

print("\nVIDEO GENERATED\n")

print("Topic:", result["topic"])
print("Idea:", result["idea"])
print("Video Path:", result["video_path"])
print("Metadata:", result["metadata"])
print("YouTube Upload:", result["upload"])

print("Telegram Notification:", result["telegram"])
