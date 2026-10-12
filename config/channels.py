"""Validated channel configuration and shared durable ownership identity."""
from collections.abc import Sequence
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, field_validator


class ChannelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    channel_key: str
    channel_name: str
    youtube_channel_id: str
    credential_reference: str
    topic_theme: str
    content_instructions: str
    timezone: str
    publication_slots: tuple[str, ...]
    preparation_times: tuple[str, ...]
    state_namespace: str
    artifact_namespace: str
    output_namespace: str
    telegram_chat_reference: str
    telegram_credential_reference: str

    @field_validator("channel_key")
    @classmethod
    def valid_key(cls, value):
        if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", value):
            raise ValueError("Invalid channel key")
        return value

    @field_validator("youtube_channel_id")
    @classmethod
    def valid_channel_id(cls, value):
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", value):
            raise ValueError("Invalid YouTube channel ID")
        return value

    @field_validator("credential_reference", "telegram_chat_reference", "telegram_credential_reference")
    @classmethod
    def valid_reference(cls, value):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
            raise ValueError("Use a secret/variable name reference, not credential contents")
        return value

    @field_validator("channel_name", "topic_theme", "content_instructions")
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or value != value.strip():
            raise ValueError("Nonempty text without surrounding whitespace required")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Invalid timezone") from None
        return value

    @field_validator("publication_slots", "preparation_times")
    @classmethod
    def valid_times(cls, values):
        if not values or any(not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", v) for v in values):
            raise ValueError("Nonempty HH:MM times required")
        if tuple(sorted(set(values))) != values:
            raise ValueError("Times must be unique and ordered")
        return values

    @field_validator("state_namespace", "artifact_namespace", "output_namespace")
    @classmethod
    def valid_namespace(cls, value):
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value)
                or any(part in {".", ".."} for part in value.split("/"))):
            raise ValueError("Invalid relative namespace")
        return value


MINUTE_MYSTERIES = ChannelConfig(
    channel_key="minute_mysteries",
    channel_name="Minute Mysteries",
    youtube_channel_id="UCR2L8UE-pc1Hd7U3AurrECQ",
    credential_reference="YOUTUBE_TOKEN_PICKLE_B64",
    topic_theme="unsolved mysteries",
    content_instructions=(
        "Documented unsolved mysteries; curiosity-driven Shorts. Strong hook in first "
        "3 seconds; around 80 words and 40 seconds; simple storytelling. Distinguish "
        "known facts from theories and unresolved questions; label speculation and "
        "folklore explicitly. Never invent evidence, quotes or a definitive solution. "
        "Keep titles and descriptions faithful to the script."
    ),
    timezone="Asia/Kolkata",
    publication_slots=("10:00", "14:00", "19:00"),
    preparation_times=("05:05", "09:05", "13:05", "17:05", "21:05"),
    state_namespace=".actions-work",
    artifact_namespace="mystery",
    output_namespace="output/videos",
    telegram_chat_reference="TELEGRAM_CHAT_ID",
    telegram_credential_reference="TELEGRAM_BOT_TOKEN",
)
# Registered for explicit manual testing only; no automatic trigger is added.
SPACE_SCIENCE = ChannelConfig(
    channel_key="space_science",
    channel_name="Space and Strange Science",
    youtube_channel_id="UCfs8yJirS4w54IEiwSlM9xA",
    credential_reference="YOUTUBE_TOKEN_SPACE_SCIENCE_B64",
    topic_theme="astronomy, space mysteries, strange science",
    content_instructions="Factual, curiosity-driven, science-focused storytelling.",
    timezone="Asia/Kolkata",
    publication_slots=("10:00", "14:00", "19:00"),
    preparation_times=MINUTE_MYSTERIES.preparation_times,
    state_namespace=".actions-work-space-science",
    artifact_namespace="space-science",
    output_namespace="output/space_science",
    telegram_chat_reference="TELEGRAM_CHAT_SPACE_SCIENCE_ID",
    telegram_credential_reference="TELEGRAM_BOT_SPACE_SCIENCE_TOKEN",
)
# Registration only; Hidden History cron is not enabled by this config.
HIDDEN_HISTORY = ChannelConfig(
    channel_key="hidden_history",
    channel_name="hiddenhistory",
    youtube_channel_id="UCWsMUjiZ7q07Bb-MKhXojDw",
    credential_reference="YOUTUBE_TOKEN_HIDDEN_HISTORY_B64",
    topic_theme="forgotten history, unusual historical incidents, strange moments from history",
    content_instructions=(
        "Factual, concise, curiosity-driven historical storytelling. Focus on lesser-known "
        "real historical events and unusual incidents. Avoid sensational claims that are "
        "not historically supportable."
    ),
    timezone="Asia/Kolkata",
    publication_slots=("10:00", "14:00", "19:00"),
    preparation_times=MINUTE_MYSTERIES.preparation_times,
    state_namespace=".actions-work-hidden-history",
    artifact_namespace="hidden-history",
    output_namespace="output/hidden_history",
    telegram_chat_reference="TELEGRAM_CHAT_HIDDEN_HISTORY_ID",
    telegram_credential_reference="TELEGRAM_BOT_HIDDEN_HISTORY_TOKEN",
)
CHANNEL_CONFIGS = (MINUTE_MYSTERIES, SPACE_SCIENCE, HIDDEN_HISTORY)


def resolve_channel_config(channel_key: str, *, configs: Sequence[ChannelConfig] | None = None) -> ChannelConfig:
    """Validate the entire registry before lookup; never load secrets or run code.

    Optional configs supports offline validation/future registries. Registration
    does not configure automatic scheduling; callers select a channel explicitly.
    """
    registry = CHANNEL_CONFIGS if configs is None else configs
    keys = set()
    namespaces = {field: set() for field in ("state_namespace", "artifact_namespace", "output_namespace")}
    found = None
    for config in registry:
        if not isinstance(config, ChannelConfig):
            raise ValueError("Registry entries must be validated ChannelConfig records")
        if config.channel_key in keys:
            raise ValueError("Duplicate channel key")
        keys.add(config.channel_key)
        for field, existing in namespaces.items():
            value = getattr(config, field).casefold()
            if any(value == old or value.startswith(old + "/") or old.startswith(value + "/") for old in existing):
                raise ValueError("Duplicate or overlapping " + field)
            existing.add(value)
        if config.channel_key == channel_key:
            found = config
    if found is None:
        raise ValueError("Unknown channel key")
    return found


def validate_channel_identity(record, channel, *, allow_legacy=False):
    """Never infer ownership for non-default channels or partially tagged state."""
    fields = ("channel_key", "youtube_channel_id", "state_namespace")
    expected = {field: getattr(channel, field) for field in fields}
    if not any(field in record for field in fields):
        if not allow_legacy or channel != MINUTE_MYSTERIES:
            raise ValueError("Legacy state adoption is restricted to Minute Mysteries")
    elif any(record.get(field) != value for field, value in expected.items()):
        raise ValueError("Channel state identity mismatch")
    preparation = record.get("preparation") or record.get("queue", {}).get("preparation")
    if preparation and preparation.get("channel_id") != channel.youtube_channel_id:
        raise ValueError("Preparation channel identity mismatch")


def channel_identity(channel):
    return {field: getattr(channel, field) for field in ("channel_key", "youtube_channel_id", "state_namespace")}


def slot_owner(channel, day, slot):
    return f"{channel.channel_key}:{day.isoformat()}:{slot}"


def valid_slot_owner(owner, channel, day, slot):
    return owner == slot_owner(channel, day, slot) or (channel == MINUTE_MYSTERIES and owner == f"{day.isoformat()}:{slot}")


def validate_channel_scope(channel):
    """Reserve legacy namespaces even for programmatically supplied configs."""
    if channel == MINUTE_MYSTERIES:
        return
    if channel.channel_key == MINUTE_MYSTERIES.channel_key:
        raise ValueError("Default channel configuration mismatch")
    for field in ("state_namespace", "artifact_namespace"):
        value, legacy = getattr(channel, field).casefold(), getattr(MINUTE_MYSTERIES, field).casefold()
        if value == legacy or value.startswith(legacy + "/") or legacy.startswith(value + "/"):
            raise ValueError("Legacy namespace belongs only to Minute Mysteries")


from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved file ownership boundary; legacy output/memory belong only to default."""
    channel: ChannelConfig
    root: Path

    def __post_init__(self):
        validate_channel_scope(self.channel)
        object.__setattr__(self, "root", Path(self.root).resolve())
        if self.channel != MINUTE_MYSTERIES:
            value = self.channel.output_namespace.casefold()
            legacy = MINUTE_MYSTERIES.output_namespace.casefold()
            if value == legacy or legacy.startswith(value + "/"):
                raise ValueError("Legacy output root belongs only to Minute Mysteries")
        for path in (self.temp, self.audio, self.clips, self.subtitles, self.output, self.memory.parent):
            if not path.resolve().is_relative_to(self.root) or path.resolve() != path:
                raise ValueError("Runtime namespace escapes root or uses linked paths")

    @property
    def temp(self):
        return self.root / "temp" / self.channel.channel_key

    @property
    def audio(self):
        return self.temp / "audio"

    @property
    def clips(self):
        return self.temp / "clips"

    @property
    def subtitles(self):
        return self.temp / "subtitles"

    @property
    def output(self):
        return self.root / self.channel.output_namespace

    @property
    def memory(self):
        if self.channel == MINUTE_MYSTERIES:
            return self.root / "data" / "idea_memory.json"
        return self.root / "data" / "channels" / self.channel.channel_key / "idea_memory.json"

    def prepare(self):
        self.__post_init__()
        for path in (self.audio, self.clips, self.subtitles, self.output, self.memory.parent):
            path.mkdir(parents=True, exist_ok=True)



def rendered_media_filename(channel, timestamp):
    """Existing pipeline filename contract, including legacy topic punctuation."""
    from datetime import datetime
    if not isinstance(timestamp, str) or not re.fullmatch(r"[0-9]{14}", timestamp):
        raise ValueError("Invalid media filename timestamp")
    try:
        datetime.strptime(timestamp, "%Y%m%d%H%M%S")
    except ValueError:
        raise ValueError("Invalid media filename timestamp") from None
    prefix = channel.topic_theme.replace(" ", "_").lower()
    if "/" in prefix or "\\" in prefix:
        raise ValueError("Invalid media filename topic")
    return f"{prefix}_{timestamp}.mp4"


def validate_rendered_media_filename(name, channel):
    """Reject paths, arbitrary names and invalid dates; never establish ownership."""
    if not isinstance(name, str) or "/" in name or "\\" in name:
        raise ValueError("Invalid media filename")
    match = re.fullmatch(r".+_([0-9]{14})\.mp4", name)
    if match is None or name != rendered_media_filename(channel, match.group(1)):
        raise ValueError("Invalid media filename")
