"""Validated channel configuration and shared durable ownership identity."""
import json
from pathlib import Path
from collections.abc import Sequence
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, field_validator


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


CHANNEL_DATA_FILE = Path(__file__).with_name("channels.json")
# Validation limits, not a new scheduler or per-channel production configuration.
SUPPORTED_SCHEDULE = {"timezone": "Asia/Kolkata",
    "publication_slots": ("10:00", "14:00", "19:00"),
    "preparation_times": ("05:05", "09:05", "13:05", "17:05", "21:05")}
LEGACY_NAMESPACES = {"state_namespace": ".actions-work",
                     "artifact_namespace": "mystery", "output_namespace": "output/videos"}


def _validate_supported_schedule(values):
    if any(values.get(field) != expected for field, expected in SUPPORTED_SCHEDULE.items()):
        raise ValueError("Unsupported production timezone or schedule")


class ChannelDefaults(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    timezone: str
    publication_slots: tuple[str, ...]
    preparation_times: tuple[str, ...]


class ChannelDefinition(BaseModel):
    """Editable inputs only; resolved ChannelConfig remains the runtime contract."""
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    channel_key: str
    channel_name: str
    youtube_channel_id: str
    credential_reference: str
    topic_theme: str
    content_instructions: str
    timezone: str | None = None
    publication_slots: tuple[str, ...] | None = None
    preparation_times: tuple[str, ...] | None = None
    telegram_chat_reference: str | None = None
    telegram_credential_reference: str | None = None
    legacy_namespaces: dict[str, str] | None = None


class ChannelRegistryData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    schema_version: int = Field(alias="schema")
    defaults: ChannelDefaults
    channels: tuple[ChannelDefinition, ...]


def _channel_index(registry):
    index, youtube_ids, credential_references = {}, set(), set()
    namespaces = {field: set() for field in ("state_namespace", "artifact_namespace", "output_namespace")}
    for config in registry:
        if not isinstance(config, ChannelConfig):
            raise ValueError("Registry entries must be validated ChannelConfig records")
        if config.channel_key in index:
            raise ValueError("Duplicate channel key")
        index[config.channel_key] = config
        for field, existing in namespaces.items():
            value = getattr(config, field).casefold()
            if any(value == old or value.startswith(old + "/") or old.startswith(value + "/") for old in existing):
                raise ValueError("Duplicate or overlapping " + field)
            existing.add(value)
        if config.youtube_channel_id in youtube_ids:
            raise ValueError("Duplicate YouTube channel ID")
        youtube_ids.add(config.youtube_channel_id)
        if config.credential_reference in credential_references:
            raise ValueError("Duplicate credential reference")
        credential_references.add(config.credential_reference)
    return index


def parse_channel_configs(text):
    # JSON duplicate object members must not silently replace earlier evidence.
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    json.loads(text, object_pairs_hook=unique_object)
    document = ChannelRegistryData.model_validate_json(text)
    if document.schema_version != 1 or not document.channels:
        raise ValueError("Unsupported or empty channel registry")
    _validate_supported_schedule(document.defaults.model_dump())
    configs = []
    for definition in document.channels:
        values = definition.model_dump(exclude_none=True)
        legacy = values.pop("legacy_namespaces", None)
        key = values["channel_key"]
        # Validate keys before deriving any namespace (never touch the filesystem).
        ChannelConfig.valid_key(key)
        slug = key.replace("_", "-")
        values = {**document.defaults.model_dump(), **values}
        _validate_supported_schedule(values)
        values.update(state_namespace=".actions-work-" + slug,
                      artifact_namespace=slug, output_namespace="output/" + key)
        if key == "minute_mysteries":
            if legacy != LEGACY_NAMESPACES:
                raise ValueError("Minute Mysteries requires exact legacy namespace compatibility")
            values.update(legacy)
        elif legacy is not None:
            raise ValueError("Legacy namespace overrides are restricted to the default channel")
        values.setdefault("telegram_chat_reference", "TELEGRAM_CHAT_" + key.upper() + "_ID")
        values.setdefault("telegram_credential_reference", "TELEGRAM_BOT_" + key.upper() + "_TOKEN")
        configs.append(ChannelConfig.model_validate(values))
    _channel_index(configs)
    return tuple(configs)


def load_channel_configs(path=CHANNEL_DATA_FILE):
    return parse_channel_configs(Path(path).read_text(encoding="utf-8"))


def resolve_channel_config(channel_key: str, *, configs: Sequence[ChannelConfig] | None = None) -> ChannelConfig:
    """Validate the whole registry; registration never enables automatic scheduling."""
    index = _channel_index(CHANNEL_CONFIGS if configs is None else configs)
    if channel_key not in index:
        raise ValueError("Unknown channel key")
    return index[channel_key]


CHANNEL_CONFIGS = load_channel_configs()
# Public compatibility exports; new channels need no new Python constant.
MINUTE_MYSTERIES = resolve_channel_config("minute_mysteries")
SPACE_SCIENCE = resolve_channel_config("space_science")
HIDDEN_HISTORY = resolve_channel_config("hidden_history")


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
