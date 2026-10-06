"""Evidence-backed LearningObservation previews; no persistence or generation hooks."""
import argparse
import hashlib
import json
from engine.learning.schema import Schema, LearningObservation
from engine.learning.content_analysis import AnalysisPreview, analyze_content, preview_analysis

VERSION = "descriptive-observations-v1"


class ObservationBuildError(ValueError):
    """Sanitized invalid, conflicting or incompletely joined analysis."""


class ObservationPreview(Schema):
    # Envelope retains original evidence/provenance; observations use the existing model.
    analysis: AnalysisPreview
    observations: list[LearningObservation]
    evidence_digest: str
    builder_version: str
    comparative_evidence: str


def build_observations(analysis):
    """Revalidate source evidence; do not trust caller-provided ratios or notes.

    No current schema/repository stores standalone observations. This immutable
    preview does not create LearningContext, IDs, timestamps or database records.
    """
    try:
        supplied = AnalysisPreview.model_validate(analysis.model_dump() if isinstance(analysis, AnalysisPreview) else analysis)
        if supplied.issues:
            raise ValueError("Incomplete analysis")
        canonical = analyze_content(supplied.channel_context_id,
            [v.publication for v in supplied.videos],
            [o.snapshot for v in supplied.videos for o in v.observations],
            [f for v in supplied.videos for f in v.features])
        if canonical.issues:
            raise ValueError("Conflicting evidence")
        # Reject altered associations, counts, duplicate representations or derived output.
        if canonical != supplied:
            raise ValueError("Noncanonical analysis")
    except Exception:
        raise ObservationBuildError("InvalidOrConflictingAnalysis") from None
    channel = canonical.channel_context_id
    def reference(kind, identifier):
        return dict(channel_context_id=channel, record_type=kind, record_id=identifier)
    observations = []
    for video in canonical.videos:
        pub = reference("published_video", video.publication.published_video_id)
        for item in video.observations:
            observations.append(LearningObservation(
                notes=item.observation.notes,
                evidence=[pub, reference("metric_snapshot", item.snapshot.snapshot_id)],
                sample_count=1, uncertainty_notes=item.observation.uncertainty_notes))
        for feature in video.features:
            values = {field: getattr(feature, field) for field in
                      ("topics", "categories", "title_patterns", "hook_patterns", "keywords", "hashtags", "content_structure")}
            text = json.dumps({key: None if val is None else [v.model_dump(mode="json") for v in val]
                               for key,val in values.items()}, sort_keys=True, ensure_ascii=True)
            observations.append(LearningObservation(
                notes="Stored content characteristics for video "+video.publication.youtube_video_id+": "+text,
                evidence=[pub, reference("content_features", feature.feature_record_id)], sample_count=1,
                uncertainty_notes="One feature version of one publication; knowledge labels and source notes are preserved. No performance effect is established."))
        for before, after in zip(video.observations, video.observations[1:]):
            a, b = before.snapshot, after.snapshot
            comparable = (a.collection_status in {"success", "partial"} and b.collection_status in {"success", "partial"}
                          and a.metric_source == b.metric_source and a.collected_at < b.collected_at
                          and a.observation_window.kind == b.observation_window.kind == "cumulative"
                          and a.observation_window.start_at == b.observation_window.start_at
                          and a.observation_window.end_at == a.collected_at
                          and b.observation_window.end_at == b.collected_at)
            changes = {key: b.metrics[key]-a.metrics[key] for key in ("views", "likes", "comments")
                       if comparable and (a.metrics or {}).get(key) is not None and (b.metrics or {}).get(key) is not None}
            if changes:
                observations.append(LearningObservation(
                    notes="Recorded cumulative counter change from "+a.collected_at.isoformat()+" to "+b.collected_at.isoformat()+": "+json.dumps(changes, sort_keys=True),
                    evidence=[pub, reference("metric_snapshot",a.snapshot_id), reference("metric_snapshot",b.snapshot_id)],
                    sample_count=1, uncertainty_notes="Two temporal observations of the same publication, not two videos. Counter changes may include lag or corrections; no causal interpretation."))
        if video.missing_data:
            observations.append(LearningObservation(notes="Unavailable data for video "+video.publication.youtube_video_id+": "+", ".join(video.missing_data),
                evidence=[pub], sample_count=1, uncertainty_notes="Missing data remains unknown."))
    if canonical.video_count:
        observations.append(LearningObservation(notes="insufficient evidence for comparative pattern",
            evidence=[reference("published_video",v.publication.published_video_id) for v in canonical.videos],
            sample_count=canonical.video_count,
            uncertainty_notes="Counts represent distinct publications, not snapshots or feature versions. Comparable observation windows and a validated comparison design are not established."))
    payload = json.dumps(canonical.model_dump(mode="json"),sort_keys=True,separators=(",",":"),ensure_ascii=True)
    return ObservationPreview(analysis=canonical,observations=observations,
        evidence_digest=hashlib.sha256((VERSION+payload).encode()).hexdigest(),builder_version=VERSION,
        comparative_evidence="INSUFFICIENT EVIDENCE")


def preview_observations(repository, *, page_size=100):
    """Reuse the existing read-only, repeatable-read repository analysis path."""
    return build_observations(preview_analysis(repository,page_size=page_size))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-context-id",required=True)
    args=parser.parse_args(argv)
    try:
        from engine.learning.database import connect_database
        from engine.learning.repositories import LearningRepository
        with connect_database() as connection:
            result=preview_observations(LearningRepository(connection,args.channel_context_id))
        print(result.model_dump_json(indent=2))
        return 0
    except Exception:
        print(json.dumps({"error_type":"ObservationPreviewFailed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
