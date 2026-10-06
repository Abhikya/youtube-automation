"""Explicitly bounded immutable learning context builds; isolated from production."""
import argparse
import json
from datetime import datetime, timezone
from uuid import uuid4
from pydantic import AwareDatetime, TypeAdapter
from engine.learning.schema import LearningContext, PublishedVideo, MetricSnapshot, ContentFeatures
from engine.learning.content_analysis import analyze_content, preview_analysis
from engine.learning.learning_observations import build_observations
from engine.learning.repositories import RepositoryConflict

VERSION = "descriptive-context-v1"
_TIME = TypeAdapter(AwareDatetime)


class ContextBuildError(ValueError):
    """Sanitized build failure; no raw driver or source payload."""


def build_context(analysis, *, context_id, built_at, start_at, end_at,
                  max_publications, max_observations, max_evidence_links, max_export_bytes,
                  context_version=VERSION, expires_at=None):
    """Explicit bounds are mandatory: no undocumented numeric defaults.

    Window [start_at,end_at) applies to metric collection times, not unknown
    publication dates. Publications without window metrics retain content evidence.
    Publication bound keeps latest observed videos, breaking ties by YouTube ID.
    Other bounds reject overflow rather than silently dropping evidence/limitations.
    """
    try:
        build = _TIME.validate_python(built_at)
        start = _TIME.validate_python(start_at)
        end = _TIME.validate_python(end_at)
        if not start < end <= build:
            raise ValueError("Invalid analysis window")
        for limit in (max_publications,max_observations,max_evidence_links,max_export_bytes):
            if type(limit) is not int or limit < 1:
                raise ValueError("Explicit positive bounds required")
        checked = build_observations(analysis).analysis
        if not checked.videos:
            raise ValueError("Empty context")
        for video in checked.videos:
            if video.publication.created_at > build or video.publication.updated_at > build or any(f.created_at > build for f in video.features):
                raise ValueError("Future source evidence")
            if any(o.snapshot.collected_at > build for o in video.observations):
                raise ValueError("Future snapshot")
        def in_window(video):
            return [o.snapshot for o in video.observations if start <= o.snapshot.collected_at < end]
        ordered = sorted(checked.videos,key=lambda v: (
            -max((m.collected_at.timestamp() for m in in_window(v)),default=float('-inf')),
            v.publication.youtube_video_id))
        selected = ordered[:max_publications]
        canonical = analyze_content(checked.channel_context_id,[v.publication for v in selected],
            [m for v in selected for m in in_window(v)],[f for v in selected for f in v.features])
        observations = build_observations(canonical)
        examples = []
        seen = set()
        for observation in observations.observations:
            for ref in observation.evidence:
                key=(ref.record_type,ref.record_id)
                if key not in seen:
                    examples.append(ref)
                    seen.add(key)
        count=sum(len(o.evidence) for o in observations.observations)+len(examples)
        if len(observations.observations)>max_observations or count>max_evidence_links:
            raise ValueError("Context bound exceeded")
        times=[o.snapshot.collected_at for v in canonical.videos for o in v.observations]
        provenance={"builder_version":VERSION,"evidence_digest":observations.evidence_digest,
            "source_analysis":canonical.model_dump(mode="json"),
            "bounds":{"max_publications":max_publications,"max_observations":max_observations,
                      "max_evidence_links":max_evidence_links,"max_export_bytes":max_export_bytes},
            "publications_available":checked.video_count,"publications_selected":len(selected)}
        context=LearningContext(channel_context_id=canonical.channel_context_id,context_id=context_id,
            context_version=context_version,analysis_period={"kind":"interval","start_at":start,"end_at":end},
            observations=observations.observations,examples=examples,sample_count=len(selected),built_at=build,
            data_through_at=max(times) if times else None,expires_at=expires_at,
            uncertainty_notes="insufficient evidence for comparative pattern; sample_count is distinct selected publications. Metric freshness covers only selected observations; unknown publication timestamps remain unknown.",
            provenance={"source":"bounded-learning-context-builder","source_record_id":context_id,"captured_at":build,
                        "notes":json.dumps(provenance,sort_keys=True,separators=(",",":"))})
        export_context(context,max_export_bytes=max_export_bytes)
        return context
    except Exception:
        raise ContextBuildError("InvalidEmptyOrOverBoundContext") from None


def export_context(context, *, max_export_bytes):
    """Validated bounded JSON export, with no file I/O."""
    value=LearningContext.model_validate(context.model_dump())
    if type(max_export_bytes) is not int or max_export_bytes < 1:
        raise ContextBuildError("InvalidExportBound")
    payload=value.model_dump_json()
    if len(payload.encode('utf-8'))>max_export_bytes:
        raise ContextBuildError("ExportBoundExceeded")
    return payload


def persist_context(repository, context):
    """Explicit transaction: validate unchanged evidence, insert children or skip.

    Same ID and identical content skips. Same ID and changed content conflicts.
    Another build ID creates a separate immutable snapshot, never updates one.
    """
    context=LearningContext.model_validate(context.model_dump()).require_channel(repository.channel_context_id)
    try:
        provenance=json.loads(context.provenance.notes)
        if context.provenance.source != "bounded-learning-context-builder":
            raise ValueError("Unknown build provenance")
        source=provenance["source_analysis"]
        reconstructed=build_context(source,context_id=context.context_id,built_at=context.built_at,
            start_at=context.analysis_period.start_at,end_at=context.analysis_period.end_at,
            context_version=context.context_version,expires_at=context.expires_at,**provenance["bounds"])
        # available count is metadata from the full inventory, which is not persisted.
        reconstructed_provenance=json.loads(reconstructed.provenance.notes)
        reconstructed_provenance["publications_available"]=provenance["publications_available"]
        reconstructed.provenance.notes=json.dumps(reconstructed_provenance,sort_keys=True,separators=(",",":"))
        if reconstructed != context:
            raise ValueError("Altered build")
    except Exception:
        raise ContextBuildError("InvalidContextBuild") from None
    with repository.transaction():
        existing=repository.get_context(context.context_id)
        if existing is not None:
            existing.require_channel(repository.channel_context_id)
            if existing != context:
                raise RepositoryConflict("Context identity already contains different evidence.")
            return {"status":"SKIPPED_IDENTICAL","context":existing}
        methods={PublishedVideo:repository.get_published_video,MetricSnapshot:repository.get_snapshot,
                 ContentFeatures:repository.get_features}
        from engine.learning.content_analysis import AnalysisPreview
        analysis=AnalysisPreview.model_validate(source)
        for video in analysis.videos:
            records=[video.publication]+video.features+[o.snapshot for o in video.observations]
            for record in records:
                identifier=(record.published_video_id if isinstance(record,PublishedVideo) else
                            record.snapshot_id if isinstance(record,MetricSnapshot) else record.feature_record_id)
                current=methods[type(record)](identifier)
                if current is None or current != record:
                    raise RepositoryConflict("Context source evidence is missing or changed.")
        result=repository.create_context(context)
        readback=repository.get_context(context.context_id)
        if readback != context:
            raise RepositoryConflict("Context reconstruction mismatch; transaction rolled back.")
        return {"status":"INSERTED","context":result}


def prepare_context(repository, *, persist=False, **parameters):
    context=build_context(preview_analysis(repository),**parameters)
    if persist:
        return persist_context(repository,context)
    return {"status":"PREVIEW","context":context}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel-context-id',required=True)
    parser.add_argument('--start-at',required=True)
    parser.add_argument('--end-at',required=True)
    parser.add_argument('--context-id')
    parser.add_argument('--context-version',default=VERSION)
    for name in ('max-publications','max-observations','max-evidence-links','max-export-bytes'):
        parser.add_argument('--'+name,type=int,required=True)
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--preview',action='store_true')
    modes.add_argument('--persist',action='store_true')
    args=parser.parse_args(argv)
    try:
        from engine.learning.database import connect_database
        from engine.learning.repositories import LearningRepository
        with connect_database() as connection:
            result=prepare_context(LearningRepository(connection,args.channel_context_id),persist=args.persist,
                context_id=args.context_id or str(uuid4()),built_at=datetime.now(timezone.utc),start_at=args.start_at,
                end_at=args.end_at,context_version=args.context_version,max_publications=args.max_publications,
                max_observations=args.max_observations,max_evidence_links=args.max_evidence_links,max_export_bytes=args.max_export_bytes)
        print(json.dumps({"status":result["status"],"context":result["context"].model_dump(mode='json')}))
        return 0
    except Exception:
        print(json.dumps({"status":"FAILED","error_type":"ContextBuildOrPersistenceFailed"}))
        return 1


if __name__ == '__main__':raise SystemExit(main())
