"""Guided channel setup; only add and explicit authorize perform local writes."""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path
import tempfile
from filelock import FileLock
from config.channels import CHANNEL_DATA_FILE, load_channel_configs, parse_channel_configs, RuntimePaths, resolve_channel_config


PROJECT_ROOT = CHANNEL_DATA_FILE.parent.parent


def _local_status(channel, configs):
    from engine.uploader.youtube_uploader import YouTubeCredentialContext
    from config.content_policy import GLOBAL_GUARDRAIL_INSTRUCTIONS
    paths = RuntimePaths(channel, PROJECT_ROOT)
    token = YouTubeCredentialContext(channel=channel).token_file
    missing = []
    try:
        runtime_matches = resolve_channel_config(channel.channel_key) == channel
    except ValueError:
        runtime_matches = False
    if not runtime_matches:
        missing.append("Make the selected config available through the production runtime registry")
    for config in configs:
        RuntimePaths(config, PROJECT_ROOT)  # Existing scope/link validation, no mkdir.
    token_exists = token.is_file() and not token.is_symlink()
    if not token_exists:
        missing.append("Run explicit authorize for this channel to create its local OAuth token")
    workflows = {}
    for filename in ("production.yml", "test-video-generation.yml"):
        file = PROJECT_ROOT / ".github" / "workflows" / filename
        text = file.read_text() if file.is_file() else ""
        supported = bool(re.search(r"(?m)^      channel_key:", text)) and "resolve_channel_config" in text and "workflow_dispatch:" in text
        if filename == "test-video-generation.yml":
            supported = supported and "upload_private:" in text
        workflows[filename] = supported
        if not supported:
            missing.append("Restore channel-aware workflow inputs: " + filename)
    pipeline = PROJECT_ROOT / "engine/pipeline/video_pipeline.py"
    policy_applies = bool(GLOBAL_GUARDRAIL_INSTRUCTIONS) and pipeline.is_file() and "validate_content" in pipeline.read_text()
    if not policy_applies:
        missing.append("Restore the shared global content policy in the pipeline")
    receipt_status = "NOT_VERIFIED"
    receipt = PROJECT_ROOT / "single-video-test.json"
    if receipt.is_file():
        try:
            evidence = json.loads(receipt.read_text())
            if (evidence.get("channel_key") == channel.channel_key
                    and evidence.get("expected_channel_id") == channel.youtube_channel_id
                    and evidence.get("upload_status") == "uploaded" and evidence.get("upload_verified") is True
                    and isinstance(evidence.get("youtube_video_id"), str) and evidence["youtube_video_id"].strip()):
                receipt_status = "LOCAL_RECEIPT_MATCHES_NOT_LIVE_VERIFIED"
        except (ValueError, AttributeError):
            receipt_status = "INVALID_LOCAL_RECEIPT"
    return {"channel_key": channel.channel_key, "config_exists": True,
            "youtube_channel_id": channel.youtube_channel_id, "credential_reference": channel.credential_reference,
            "expected_github_secret": channel.credential_reference, "github_secret_status": "NOT_VERIFIED",
            "token_path": str(token), "local_token_exists": token_exists,
            "token_validity": "NOT_VERIFIED_NO_CREDENTIAL_LOAD", "runtime_resolution_matches": runtime_matches,
            "paths_namespaces_valid": True, "global_content_policy_applies": policy_applies,
            "workflow_support": workflows, "private_validation_status": receipt_status,
            "cron_template_ready": workflows["production.yml"],
            "readiness": "NOT_READY" if missing else "READY_FOR_PRIVATE_TEST",
            "missing_steps": missing,
            "manual_checks_required": ["Ensure the reviewed channel config exists on the selected GitHub workflow branch",
                "Manually install the channel's GitHub Actions token secret",
                "Run the private single-video workflow and confirm channel ID and private privacy",
                "Enable cron only after explicit review of the successful private test"],
            "scope": "LOCAL_PREREQUISITES_ONLY; no remote secret, token validity or live publication verification"}


def _repository_name(explicit=None):
    value = explicit or os.environ.get("GITHUB_REPOSITORY")
    if not value:
        remote = subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=PROJECT_ROOT,
                                         text=True, stderr=subprocess.DEVNULL).strip()
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", remote)
        if not match:
            raise ValueError("Provide --repository OWNER/REPO for a GitHub destination")
        value = match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("Invalid repository identifier")
    return value


def _guided_command(args, configs):
    try:
        channel = resolve_channel_config(args.channel_key, configs=configs)
    except ValueError:
        print(json.dumps({"readiness": "NOT_READY", "config_exists": False,
                          "missing_steps": ["Register the requested channel key first"]}))
        return 1
    if args.command in ("status", "readiness"):
        result = _local_status(channel, configs)
        print(json.dumps(result, indent=2))
        return 1 if args.command == "readiness" and result["readiness"] == "NOT_READY" else 0
    if args.command == "authorize":
        if resolve_channel_config(channel.channel_key) != channel:
            raise ValueError("Authorization requires the matching production runtime config")
        from engine.uploader.youtube_uploader import main as authorize_main
        return authorize_main(["--authorize", "--channel-key", channel.channel_key])
    if args.command == "secret-info":
        from engine.uploader.youtube_uploader import YouTubeCredentialContext
        token = YouTubeCredentialContext(channel=channel).token_file
        quoted = str(token).replace("'", "''")
        print(json.dumps({"token_path": str(token), "github_secret_name": channel.credential_reference,
            "powershell_command": "[Convert]::ToBase64String([IO.File]::ReadAllBytes('" + quoted + "')) | Set-Clipboard",
            "instructions": "Run the command locally, then manually paste the clipboard into GitHub repository Settings > Secrets and variables > Actions under this exact secret name. No value is printed or saved by this CLI."}, indent=2))
        return 0
    if args.command == "private-test-info":
        print(json.dumps({"workflow": ".github/workflows/test-video-generation.yml",
            "workflow_display_name": "YouTube Single-Video Test", "ref": "main",
            "inputs": {"channel_key": channel.channel_key, "upload_private": True},
            "instructions": "Use Run workflow on the branch containing this registered config and code. Run once; confirm the returned video ID, configured channel ID, PRIVATE privacy and no publishAt. Do not blindly rerun an uncertain upload."}, indent=2))
        return 0
    repo = _repository_name(args.repository)
    print(json.dumps({"url": "https://api.github.com/repos/" + repo + "/actions/workflows/production.yml/dispatches",
        "method": "POST", "headers": {"Accept": "application/vnd.github+json",
            "Authorization": "Bearer <YOUR_GITHUB_PAT>", "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"},
        "body": {"ref": "main", "inputs": {"channel_key": channel.channel_key, "operation": "prepare"}},
        "timezone": channel.timezone, "preparation_opportunities": channel.preparation_times,
        "offset_minutes": 0,
        "instructions": "Template only. Configure cron-job.org manually after private validation. Every opportunity targets tomorrow and resumes the same batch. Existing schedules are unchanged; no additional offset is assumed."}, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CHANNEL_DATA_FILE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("validate")
    for name in ("status", "authorize", "secret-info", "private-test-info", "cron-template", "readiness"):
        command = commands.add_parser(name)
        command.add_argument("--channel-key", required=True)
        if name == "cron-template":
            command.add_argument("--repository", help="Optional GitHub OWNER/REPO; otherwise use local origin")
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
            if args.command not in ("list", "validate"):
                return _guided_command(args, configs)
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
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # Validation/IO diagnostics may contain pasted credentials: print types only.
        if args.command in ("status", "readiness"):
            print(json.dumps({"readiness": "NOT_READY", "missing_steps": [
                "Repair registry or local path validation (" + type(exc).__name__ + "); run validate"]}))
        else:
            print(f"Channel onboarding failed ({type(exc).__name__}); no automatic retry")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
