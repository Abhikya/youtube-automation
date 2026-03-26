from engine.uploader.youtube_uploader import upload_video

video_path = "output/videos/quick_minds_20260320000043.mp4"

title = "Test Upload from AI Engine 🚀"
description = "Testing automated upload"
tags = ["AI", "test", "shorts"]

upload_video(video_path, title, description, tags)

print("Uploaded successfully!")