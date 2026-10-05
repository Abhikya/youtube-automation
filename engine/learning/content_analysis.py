"""Read-only descriptive joins of existing channel-scoped learning records."""
import argparse
import json
from datetime import timezone
from typing import Literal
from pydantic import Field
from engine.learning.schema import Schema, Identifier, PublishedVideo, MetricSnapshot, ContentFeatures, LearningObservation


class ObservationPreview(Schema):
    snapshot: MetricSnapshot
    like_to_view_ratio: float | None
    comment_to_view_ratio: float | None
    missing_metrics: list[str]
    observation: LearningObservation


class VideoPreview(Schema):
    publication: PublishedVideo
    features: list[ContentFeatures]
    observations: list[ObservationPreview]
    missing_data: list[str]


class AnalysisPreview(Schema):
    channel_context_id: Identifier
    mode: Literal["READ_ONLY_PREVIEW"] = "READ_ONLY_PREVIEW"
    video_count: int = Field(ge=0)
    observation_count: int = Field(ge=0)
    videos: list[VideoPreview]
    issues: list[str]
    comparative_evidence: str
    limitations: list[str]


def analyze_content(channel_context_id, publications, snapshots, features):
    """Pure deterministic preview. No persistence, rankings, or causal inference."""
    issues = []
    def validate(records, model, identity):
        found = {}
        blocked = set()
        for raw in records:
            try:
                item = model.model_validate(raw.model_dump() if isinstance(raw, model) else raw).require_channel(channel_context_id)
                key = getattr(item, identity)
                if key in blocked:continue
                if key in found and item != found[key]:
                    del found[key]
                    blocked.add(key)
                    issues.append(model.__name__+":ConflictingIdentity")
                else:found[key] = item
            except Exception:
                issues.append(model.__name__+":InvalidOrWrongChannel")
        return found
    pubs = validate(publications, PublishedVideo, "published_video_id")
    metrics = validate(snapshots, MetricSnapshot, "snapshot_id")
    content = validate(features, ContentFeatures, "feature_record_id")
    groups = {}
    for pub in pubs.values():groups.setdefault(pub.youtube_video_id, []).append(pub)
    valid = {video:group[0] for video,group in groups.items() if len(group)==1}
    for group in groups.values():
        if len(group)>1:issues.append("PublishedVideo:DuplicateYouTubeIdentity")
    for metric in metrics.values():
        if metric.youtube_video_id not in valid:issues.append("MetricSnapshot:MissingOrAmbiguousPublication")
    linked = {p.published_video_id:[] for p in valid.values()}
    for feature in content.values():
        matched = False
        for pub in valid.values():
            if feature.video_reference:
                if feature.video_reference.record_id != pub.published_video_id:continue
                if feature.generation_reference and (pub.generation_reference is None or feature.generation_reference.record_id != pub.generation_reference.record_id):
                    issues.append("ContentFeatures:GenerationLinkMismatch")
                    continue
            elif not (feature.generation_reference and pub.generation_reference and feature.generation_reference.record_id==pub.generation_reference.record_id):continue
            linked[pub.published_video_id].append(feature)
            matched = True
        if not matched:issues.append("ContentFeatures:MissingOrInvalidPublicationLink")
    videos = []
    for identifier,pub in sorted(valid.items()):
        observations = []
        history = sorted((m for m in metrics.values() if m.youtube_video_id==identifier), key=lambda m:(m.collected_at,m.snapshot_id))
        for metric in history:
            values = metric.metrics or {}
            views,likes,comments = (values.get(k) for k in ("views","likes","comments"))
            usable = metric.collection_status in {"success","partial"}
            like_ratio = likes/views if usable and views is not None and views>0 and likes is not None else None
            comment_ratio = comments/views if usable and views is not None and views>0 and comments is not None else None
            note = "Recorded observation at "+metric.collected_at.astimezone(timezone.utc).isoformat()+": "+", ".join(k+"="+("unknown" if values.get(k) is None else str(values[k])) for k in ("views","likes","comments"))+"."
            if like_ratio is not None:note += " Observed like-to-view ratio="+str(like_ratio)+"."
            observations.append(ObservationPreview(snapshot=metric,like_to_view_ratio=like_ratio,comment_to_view_ratio=comment_ratio,
                missing_metrics=[k for k in ("views","likes","comments") if values.get(k) is None],
                observation=LearningObservation(notes=note,sample_count=1,
                    evidence=[{"channel_context_id":channel_context_id,"record_type":"metric_snapshot","record_id":metric.snapshot_id}],
                    uncertainty_notes="One observation of one video; counters may lag. No content effect is established.")))
        absent=[]
        if not linked[pub.published_video_id]:absent.append("content_features")
        if not history:absent.append("metric_snapshots")
        if pub.published_at is None:absent.append("publication_timestamp")
        videos.append(VideoPreview(publication=pub,features=sorted(linked[pub.published_video_id],key=lambda f:f.feature_record_id),observations=observations,missing_data=absent))
    return AnalysisPreview(channel_context_id=channel_context_id,video_count=len(videos),observation_count=sum(len(v.observations) for v in videos),
        videos=videos,issues=sorted(issues),comparative_evidence="insufficient evidence for comparative pattern",
        limitations=["Descriptive only: no causal inference or ranking.",
                     "Repeated snapshots are not independent video samples; cumulative counters are not summed.",
                     "Different ages, sources and observation windows are not assumed comparable.",
                     "Feature versions remain separate; no winning version is selected.",
                     "Missing/zero views and failed/unavailable observations have no engagement ratio."])


def preview_analysis(repository, *, page_size=100):
    """Stable read-only inventory using existing paginated repository reads."""
    if type(page_size) is not int or not 1<=page_size<=1000:raise ValueError("Invalid page size")
    connection=getattr(repository,"_connection",None)
    if connection is not None:
        from psycopg.pq import TransactionStatus
        if connection.info.transaction_status != TransactionStatus.IDLE:raise ValueError("Idle connection required")
    def pages(method):
        result=[]
        offset=0
        while True:
            page=method(limit=page_size,offset=offset)
            if not isinstance(page,list):raise ValueError("Malformed inventory page")
            result.extend(page)
            if len(page)<page_size:return result
            offset+=len(page)
    with repository.transaction():
        if connection is not None:connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return analyze_content(repository.channel_context_id,pages(repository.list_published_videos),pages(repository.list_snapshots),pages(repository.list_features))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-context-id",required=True)
    args=parser.parse_args(argv)
    try:
        from engine.learning.database import connect_database
        from engine.learning.repositories import LearningRepository
        with connect_database() as connection:result=preview_analysis(LearningRepository(connection,args.channel_context_id))
        print(result.model_dump_json(indent=2))
        return 1 if result.issues else 0
    except Exception:
        print(json.dumps({"mode":"READ_ONLY_PREVIEW","error_type":"AnalysisReadFailed"}))
        return 1


if __name__=="__main__":raise SystemExit(main())
