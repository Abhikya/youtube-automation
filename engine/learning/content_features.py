"""Deterministic channel-neutral feature extraction; no API or startup I/O."""
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid
from engine.learning.schema import ContentFeatures, GenerationRecord, PublishedVideo

VERSION = "deterministic-v1"


def feature(value, notes, *, derived=False):
    return {"value": None if value is None else str(value),
            "knowledge": "unknown" if value is None else "inferred" if derived else "known",
            "notes": notes}



def opening_sentence(script):
    """Conservative textual heuristic, not a verified spoken-hook detector."""
    if not script or not script.strip():
        return None
    first_line = script.strip().splitlines()[0]
    abbreviations = {"dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e"}
    for match in re.finditer(r"[.!?]", first_line):
        index = match.start()
        prefix = first_line[:index]
        following = first_line[index+1:]
        if following.startswith((".", "!", "?")):
            return None  # Ellipses/repeated punctuation are ambiguous.
        if match[0] == ".":
            token = re.search(r"([A-Za-z.]+)$", prefix)
            if token and token[1].lower() in abbreviations:
                continue
            if token and len(token[1]) == 1:
                return None  # Initials/acronyms are ambiguous.
            if following and not following[0].isspace():
                return None  # Decimal, acronym, or unsupported quote boundary.
        elif following and not following[0].isspace():
            return None
        sentence = first_line[:index+1]
        if len(sentence.split()) < 3:
            return None
        return sentence
    return None


def extract_features(channel_context_id, *, generation=None, publication=None, extracted_at):
    """Actual extraction time is mandatory. Never infer publication time or duration.

    Numeric measurements use labeled textual FeatureValues because the existing
    schema has no numeric feature columns. Character counts use Unicode codepoints;
    word counts use whitespace splitting. No categories/keywords are guessed.
    """
    if generation is None and publication is None:
        raise ValueError("A source record is required")
    if generation is not None:
        generation = GenerationRecord.model_validate(generation.model_dump()).require_channel(channel_context_id)
    if publication is not None:
        publication = PublishedVideo.model_validate(publication.model_dump()).require_channel(channel_context_id)
        if generation is not None and (publication.generation_reference is None or
                                      publication.generation_reference.record_id != generation.generation_id):
            raise ValueError("Publication generation linkage mismatch")
    metadata = (publication.upload_metadata or {}) if publication else {}
    def text(key):
        value = metadata.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("Malformed stored metadata")
        return value
    title = generation.title if generation else text("Title")
    description = generation.description if generation else text("Description")
    script = generation.script if generation else None
    tags = generation.hashtags if generation and generation.hashtags is not None else metadata.get("Hashtags")
    tag_source = "GenerationRecord.hashtags" if generation and generation.hashtags is not None else "PublishedVideo.upload_metadata.Hashtags"
    if tags is not None and (not isinstance(tags, list) or any(not isinstance(t,str) or not t.strip() for t in tags)):
        raise ValueError("Malformed stored hashtags")
    hook = generation.hook if generation else None
    hook_note = "Explicit GenerationRecord.hook" if hook else "No explicit hook or complete opening sentence available"
    derived_hook = False
    if hook is None and script:
        opening = opening_sentence(script)
        if opening:
            hook = opening
            hook_note = "Derived from textual sentence-boundary heuristics; not verified as an actual spoken or authored hook"
            derived_hook = True
    def measure(name, value, notes):
        return feature(None if value is None else name+"="+str(value), notes, derived=value is not None)
    source = {"generation": generation.model_dump(mode="json") if generation else None,
              "publication": publication.model_dump(mode="json") if publication else None}
    digest = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    gid = generation.generation_id if generation else None
    pid = publication.published_video_id if publication else None
    identity = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([VERSION,channel_context_id,gid,pid])))
    def reference(kind, identity):
        return {"channel_context_id":channel_context_id,"record_type":kind,"record_id":identity}
    return ContentFeatures(channel_context_id=channel_context_id, feature_record_id=identity,
        provenance={"source":"stored-learning-content", "source_record_id":pid or gid,
                    "captured_at":extracted_at,"notes":"input_sha256="+digest},
        generation_reference=reference("generation",gid) if gid else None,
        video_reference=reference("published_video",pid) if pid else None,
        topics=[feature(generation.topic if generation else None,"Explicit GenerationRecord.topic only")],
        categories=None, keywords=None,
        title_patterns=[measure("characters",len(title) if title is not None else None,"Unicode codepoints in stored title"),
                        measure("words",len(title.split()) if title is not None else None,"Whitespace-split stored title"),
                        measure("question_marks",title.count("?") if title is not None else None,"Literal title punctuation count")],
        hook_patterns=[feature(hook,hook_note,derived=derived_hook)],
        hashtags=None if tags is None else [feature(t,"Explicit stored hashtag from "+tag_source) for t in tags],
        content_structure=[measure("script_words",len(script.split()) if script else None,"Whitespace-split stored script"),
                           measure("script_characters",len(script) if script else None,"Unicode codepoints in stored script"),
                           measure("description_characters",len(description) if description is not None else None,"Unicode codepoints in stored description"),
                           measure("hashtag_count",len(tags) if tags is not None else None,"Count of explicitly stored hashtags"),
                           feature(None if generation is None or generation.measured_duration_seconds is None else
                                   "measured_duration_seconds="+str(generation.measured_duration_seconds),"Stored measured duration only; no speech-rate estimate")],
        extraction_method="deterministic-stored-content",extraction_version=VERSION,created_at=extracted_at)


def extract_stored(repository, *, generation_id=None, published_video_id=None, persist=False, clock=None):
    """Preview by default. Same source/version conflicts on changed evidence."""
    try:
        generation = repository.get_generation(generation_id) if generation_id else None
        publication = repository.get_published_video(published_video_id) if published_video_id else None
        if generation_id and generation is None or published_video_id and publication is None:
            raise ValueError("SourceMissing")
        if generation is not None and generation.generation_id != generation_id:
            raise ValueError("WrongSourceIdentity")
        if publication is not None:
            if publication.published_video_id != published_video_id:
                raise ValueError("WrongSourceIdentity")
            publication.require_channel(repository.channel_context_id)
            if generation is None and publication.generation_reference:
                generation = repository.get_generation(publication.generation_reference.record_id)
                if generation is None:raise ValueError("LinkedGenerationMissing")
        candidate = extract_features(repository.channel_context_id,generation=generation,publication=publication,
                                     extracted_at=(clock or (lambda:datetime.now(timezone.utc)))())
        with repository.transaction():
            prior = repository.get_features(candidate.feature_record_id)
            if prior is not None:
                prior.require_channel(repository.channel_context_id)
                # Preserve the actual first extraction time, never invent a source time.
                comparable = candidate.model_copy(update={"created_at":prior.created_at,
                    "provenance":candidate.provenance.model_copy(update={"captured_at":prior.provenance.captured_at})})
                if comparable != prior:
                    return {"status":"CONFLICT","feature_record_id":candidate.feature_record_id}
                return {"status":"SKIP_ALREADY_EXISTS","features":prior}
            if persist:
                repository.create_features(candidate)
        return {"status":"INSERT" if persist else "PREVIEW","features":candidate}
    except Exception:
        return {"status":"INVALID_OR_FAILED"}


def extract_publications(repository, *, persist=False, page_size=100, clock=None):
    """Stable inventory first, isolated extraction second; explicit partial reports.

    Real repository connections must be idle, allowing a top-level repeatable-read
    read-only transaction. Writes occur only after enumeration ends. Refuse an
    existing transaction rather than pretending its isolation is sufficient.
    """
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError("Invalid page size")
    publications = []
    results = []
    complete = True
    connection = getattr(repository, "_connection", None)
    try:
        if connection is not None:
            from psycopg.pq import TransactionStatus
            if connection.info.transaction_status != TransactionStatus.IDLE:
                raise ValueError("Idle connection required for stable enumeration")
        with repository.transaction():
            if connection is not None:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            offset = 0
            while True:
                page = repository.list_published_videos(limit=page_size, offset=offset)
                if not isinstance(page, list):
                    raise ValueError("Malformed page")
                publications.extend(page)
                if len(page) < page_size:
                    break
                offset += len(page)
    except Exception:
        complete = False
        results.append({"status":"FAILED", "error_type":"PublicationInventoryFailed"})
    seen = set()
    for publication in publications:
        try:
            identity = publication.published_video_id
            publication.require_channel(repository.channel_context_id)
            if identity in seen:
                result = {"status":"SKIP_DUPLICATE", "published_video_id":identity}
            else:
                seen.add(identity)
                result = extract_stored(repository,published_video_id=identity,persist=persist,clock=clock)
                result["published_video_id"] = identity
                if result["status"] == "INVALID_OR_FAILED":
                    result["error_type"] = "SourceExtractionFailed"
        except Exception:
            result = {"status":"FAILED", "error_type":"InvalidPublicationRow"}
        results.append(result)
    totals = {"collected":0, "skipped":0, "failed":0}
    for result in results:
        state = result["status"]
        key = "collected" if state in {"INSERT","PREVIEW"} else "skipped" if state in {"SKIP_ALREADY_EXISTS","SKIP_DUPLICATE"} else "failed"
        totals[key] += 1
    return {"complete":complete, "success":complete and totals["failed"]==0,
            "totals":totals, "results":results}
