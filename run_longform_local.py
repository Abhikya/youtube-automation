"""Generate exactly one inspectable local long-form video. No upload operation exists here."""
import argparse
import json
import logging
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel-key', default='minute_mysteries',
                        choices=['minute_mysteries', 'space_science', 'hidden_history'])
    parser.add_argument("--resume", action="store_true", help="Resume the saved local story without regenerating narration")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    from config.channels import resolve_channel_config, RuntimePaths
    from config.formats import longform_channel
    from config.settings import BASE_DIR
    from engine.pipeline.video_pipeline import generate_video
    channel = longform_channel(resolve_channel_config(args.channel_key))
    result = generate_video(publish=False, runtime_paths=RuntimePaths(channel, Path(BASE_DIR)), resume=args.resume)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
