"""Edit channel data only; never authorize accounts, secrets, cron or production."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from filelock import FileLock
from config.channels import CHANNEL_DATA_FILE, load_channel_configs, parse_channel_configs, RuntimePaths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CHANNEL_DATA_FILE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("validate")
    add = commands.add_parser("add")
    required = ("channel_key", "channel_name", "youtube_channel_id", "topic_theme",
                "content_instructions", "credential_reference")
    for field in required:
        add.add_argument("--" + field.replace("_", "-"), required=True)
    add.add_argument("--timezone")
    add.add_argument("--publication-slots", nargs="+")
    add.add_argument("--preparation-times", nargs="+")
    args = parser.parse_args(argv)
    try:
        path = args.config.absolute()
        if path.is_symlink():
            raise ValueError("Linked channel config file rejected")
        if args.command != "add":
            configs = load_channel_configs(path)
            if args.command == "validate":
                print(f"VALID: {len(configs)} channels")
            else:
                for channel in configs:
                    print(json.dumps(channel.model_dump(mode="json")))
            return 0
        # Serialize registry edits and atomically replace the validated JSON only.
        with FileLock(str(path) + ".lock", timeout=0):
            load_channel_configs(path)
            document = json.loads(path.read_text(encoding="utf-8"))
            record = {field: getattr(args, field) for field in required}
            for field in ("timezone", "publication_slots", "preparation_times"):
                if getattr(args, field) is not None:
                    record[field] = getattr(args, field)
            document["channels"].append(record)
            proposed = json.dumps(document, indent=2) + "\n"
            configs = parse_channel_configs(proposed)
            channel = configs[-1]
            paths = RuntimePaths(channel, CHANNEL_DATA_FILE.parent.parent)
            print(json.dumps({**channel.model_dump(mode="json"),
                              "temp_path": str(paths.temp), "idea_memory_path": str(paths.memory),
                              "local_credential_path": ".credentials/" + channel.channel_key + "/token.pickle"}, indent=2))
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                                 prefix=".channel-onboarding-", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(proposed)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
            print("REGISTERED: config only; OAuth, secrets and cron require separate explicit setup")
        return 0
    except (ValueError, OSError) as exc:
        # Validation/IO diagnostics may contain pasted credentials: print types only.
        print(f"Channel onboarding failed ({type(exc).__name__}); no automatic retry")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
