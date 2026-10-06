"""Explicit read-only reporting; never a production control or persistence path."""
import argparse
from collections import Counter
from datetime import date,datetime,timezone
from itertools import combinations
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from pydantic import AwareDatetime,Field,StrictBool
from typing import Literal
from engine.learning.schema import Schema,Identifier,Text,ContentFeatures
from engine.learning.content_features import opening_sentence
from engine.learning.analytics_reader import AnalyticsReader,authenticated_reader,number


class ProductionInput(Schema):
    youtube_video_id: Text
    channel_context_id: Identifier
    youtube_channel_id: Identifier
    classification: Literal['PRODUCTION','TEST','UNKNOWN']
    accepted_production: StrictBool=False
    evidence: list[Text]=Field(default_factory=list)
    title: Text|None=None
    topic: Text|None=None
    script: Text|None=None
    description: Text|None=None
    hashtags: list[Text]|None=None
    measured_duration_seconds: float|None=Field(default=None,ge=0)


def validate_inventory(records):
    if not isinstance(records,list) or len(records)>1000:raise ValueError('InvalidInventory')
    # Conflicting same-ID evidence fails the report, not first-row-wins eligibility.
    unique={}
    duplicates=[]
    from engine.learning.metric_collector import video_id
    for raw in records:
        item=ProductionInput.model_validate(raw)
        video_id(item.youtube_video_id)
        key=item.youtube_video_id
        if key in unique:
            if item!=unique[key]:raise ValueError('ConflictingInventoryIdentity')
            duplicates.append(key)
        else:unique[key]=item
    return [unique[k] for k in sorted(unique)],sorted(duplicates)


def content_summary(item,stored_features):
    hook=opening_sentence(item.script)
    return {'input_evidence':item.evidence,'topic':item.topic,'script':item.script,
            'script_word_count':None if item.script is None else len(item.script.split()),
            'title_character_count':None if item.title is None else len(item.title),
            'description_character_count':None if item.description is None else len(item.description),
            'hashtags':item.hashtags,'hashtag_count':None if item.hashtags is None else len(item.hashtags),
            'measured_duration_seconds':item.measured_duration_seconds,
            'hook':{'value':hook,'knowledge':'inferred' if hook else 'unknown',
                    'provenance':'Existing opening_sentence textual heuristic; not a verified spoken hook'},
            'stored_feature_versions':stored_features}


def compare_videos(videos, *, max_age_difference_hours=24):
    """Pure descriptive comparisons. No pooling versions/snapshots or causal rules.

    The explicit age tolerance is a reporting policy, not a statistical guarantee.
    API windows are calendar days in Pacific time, not exact T+24h exposures.
    """
    if type(max_age_difference_hours) not in (int,float) or not 0<=max_age_difference_hours<=24:
        raise ValueError('InvalidAgeTolerance')
    findings=[];eligible=[]
    for video in sorted(videos,key=lambda v:v['video_id']):
        analytics=video['analytics'];window=analytics['window']
        if analytics['basic']['status']!='AVAILABLE' or not analytics['daily_coverage']['complete']:continue
        pub=datetime.fromisoformat(video['published_at'])
        start=datetime.combine(date.fromisoformat(window['start_date']),datetime.min.time(),ZoneInfo(window['timezone']))
        age=(start-pub).total_seconds()/3600
        if age<0:continue
        eligible.append((video,age))
    for (a,age_a),(b,age_b) in combinations(eligible,2):
        if a['video_id']==b['video_id']:continue
        if a['analytics']['window']!=b['analytics']['window']:continue
        if abs(age_a-age_b)>max_age_difference_hours:continue
        shared={'video_ids':[a['video_id'],b['video_id']], 'window':a['analytics']['window'],
                'publication_ages_at_window_start_hours':[age_a,age_b],
                'evidence':[f"analytics:{v['video_id']}:{v['analytics']['window']['start_date']}:{v['analytics']['window']['end_date_exclusive']}" for v in (a,b)],
                'content_characteristics':{v['video_id']:v['content_features'] for v in (a,b)},
                'confidence':'low','interpretation':'Descriptive association only; publication ages may differ within the stated tolerance.'}
        for metric in ('averageViewDuration','averageViewPercentage','estimatedMinutesWatched','subscribersGained'):
            left=a['analytics']['basic']['values'].get(metric);right=b['analytics']['basic']['values'].get(metric)
            if left is None or right is None:continue
            number(left);number(right)
            findings.append({**shared,'metric':metric,'values':[left,right],'difference_second_minus_first':right-left,
                'observation':f"Within the comparable observed window, {a['video_id']} recorded {metric}={left} and {b['video_id']} recorded {metric}={right}."})
        for point in (0.10,0.25,0.50,0.75,1.00):
            def exact(v):
                retention=v['analytics']['retention']
                if retention['status']!='AVAILABLE':return None
                return next((r['audienceWatchRatio'] for r in retention['values'] if r['elapsedVideoTimeRatio']==point),None)
            left,right=exact(a),exact(b)
            if left is not None and right is not None:
                findings.append({**shared,'metric':'audienceWatchRatio','elapsed_video_time_ratio':point,
                    'values':[left,right],'difference_second_minus_first':right-left,
                    'observation':f"Returned retention at {point}: {a['video_id']}={left}, {b['video_id']}={right}; no interpolation."})
    return findings


def recommendations(findings):
    """Exploratory human-review advice only; never confidence above low.

    At least three distinct mutually compared videos in the same source/window
    are required. This is a minimum review gate, not statistical significance.
    """
    groups={}
    for finding in findings:
        if not finding.get('evidence'):continue
        key=json.dumps(finding['window'],sort_keys=True)
        group=groups.setdefault(key,[]);group.append(finding)
    result=[]
    for key,group in sorted(groups.items()):
        ids=sorted({v for f in group for v in f['video_ids']})
        pairs={tuple(sorted(f['video_ids'])) for f in group}
        if len(ids)<3 or any(tuple(pair) not in pairs for pair in combinations(ids,2)):continue
        known_content={v for f in group for v,content in f.get('content_characteristics',{}).items()
                       if content.get('hook',{}).get('value') is not None or content.get('stored_feature_versions')}
        if not set(ids)<=known_content or not any(f['difference_second_minus_first']!=0 for f in group):continue
        evidence=sorted({ref for f in group for ref in f['evidence']})
        result.append({'observation':f'Descriptive performance differences are available for {len(ids)} mutually comparable production videos.',
            'evidence':'; '.join(evidence),'confidence':'low',
            'recommendation':'Manually review the reported hooks and content alongside these metrics. Treat differences as questions for review, not established generator rules.',
            'action':'MANUAL_REVIEW_REQUIRED'})
    return result


def read_learning_metadata(repository):
    """Optional existing metadata reads under an enforced read-only transaction."""
    connection=repository._connection
    from psycopg.pq import TransactionStatus
    if connection.info.transaction_status!=TransactionStatus.IDLE:raise ValueError('IdleConnectionRequired')
    def pages(method):
        result=[];offset=0
        while True:
            page=method(limit=100,offset=offset)
            if not isinstance(page,list):raise ValueError('InvalidInventoryPage')
            result.extend(page)
            if len(page)<100:return result
            offset+=len(page)
    with repository.transaction():
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        profile=repository.get_profile()
        if profile is None:raise ValueError('ProfileMissing')
        profile.require_channel(repository.channel_context_id)
        publications=pages(repository.list_published_videos)
        features=pages(repository.list_features)
        generations=pages(repository.list_generations)
        for row in publications+features+generations:row.require_channel(repository.channel_context_id)
        return {'profile':profile,'publications':publications,'features':features,'generations':generations}


def build_report(inventory, reader, *, channel_context_id, expected_channel, start, end,
                 generated_at, learning_metadata=None,max_age_difference_hours=24):
    from pydantic import TypeAdapter
    stamp=TypeAdapter(AwareDatetime).validate_python(generated_at)
    if type(start) is not date or type(end) is not date or start>=end:raise ValueError('InvalidAnalysisWindow')
    if end>stamp.astimezone(ZoneInfo('America/Los_Angeles')).date():raise ValueError('WindowIncludesUnfinishedDay')
    if reader.expected_channel!=expected_channel:raise ValueError('ReaderChannelMismatch')
    items,duplicates=validate_inventory(inventory)
    metadata=learning_metadata or {'publications':[],'features':[],'generations':[]}
    if learning_metadata:
        profile=metadata['profile'].require_channel(channel_context_id)
        if profile.youtube_channel_id!=expected_channel:raise ValueError('LearningChannelMismatch')
    for row in metadata['publications']+metadata['features']+metadata['generations']:row.require_channel(channel_context_id)
    # Reuse native feature records only with verified native publication/generation links.
    if len({p.youtube_video_id for p in metadata['publications']})!=len(metadata['publications']):raise ValueError('AmbiguousPublicationIdentity')
    pubs={p.youtube_video_id:p for p in metadata['publications']}
    generations={g.generation_id:g for g in metadata['generations']}
    excluded=[];accepted=[]
    for item in items:
        if item.channel_context_id!=channel_context_id or item.youtube_channel_id!=expected_channel:reason='WrongChannel'
        elif item.classification!='PRODUCTION':reason='NotAcceptedProduction:'+item.classification
        elif not item.accepted_production or not item.evidence:reason='ProductionEvidenceRequired'
        else:accepted.append(item);continue
        excluded.append({'video_id':item.youtube_video_id,'reason':reason})
    preflight=reader.verify_channel()
    if preflight['status']!='MATCH':raise ValueError('AuthenticatedChannelNotMatched')
    videos=[];features={};metrics={}
    for item in accepted:
        try:
            platform=reader.video(item.youtube_video_id)
            if platform['status']!='AVAILABLE':
                excluded.append({'video_id':item.youtube_video_id,'reason':platform['status']});continue
            pubstamp=TypeAdapter(AwareDatetime).validate_python(platform['published_at'])
            if platform['channel_id']!=expected_channel:
                excluded.append({'video_id':item.youtube_video_id,'reason':'WrongChannel'});continue
            if platform['privacy']!='public' or pubstamp>stamp:
                excluded.append({'video_id':item.youtube_video_id,'reason':'NotPublishedPublic'});continue
            source=item.model_copy(deep=True);stored=[]
            pub=pubs.get(item.youtube_video_id)
            if pub:
                gen=generations.get(pub.generation_reference.record_id) if pub.generation_reference else None
                if pub.generation_reference and gen is None:raise ValueError('MissingGenerationReference')
                if gen:
                    if source.script is not None and source.script!=gen.script:raise ValueError('ConflictingScriptEvidence')
                    source.script=gen.script
                    for field in ('title','description','hashtags','topic','measured_duration_seconds'):
                        if getattr(source,field) is None:setattr(source,field,getattr(gen,field))
                for f in metadata['features']:
                    if ((f.video_reference and f.video_reference.record_id==pub.published_video_id) or
                        (not f.video_reference and f.generation_reference and pub.generation_reference and f.generation_reference.record_id==pub.generation_reference.record_id)):
                        if f.generation_reference and (not pub.generation_reference or f.generation_reference.record_id!=pub.generation_reference.record_id):raise ValueError('InvalidFeatureLink')
                        stored.append(ContentFeatures.model_validate(f.model_dump()).model_dump(mode='json'))
            summary=content_summary(source,sorted(stored,key=lambda f:f['feature_record_id']))
            analytics=reader.collect(item.youtube_video_id,start,end)
            features[item.youtube_video_id]=summary
            counters=platform['cumulative_metrics'];views=counters.get('views')
            ratios={key:None if views is None or views<=0 or counters.get(name) is None else counters[name]/views
                    for key,name in (('like_to_view_ratio','likes'),('comment_to_view_ratio','comments'))}
            metrics[item.youtube_video_id]={'cumulative_data_api':{'source':'youtube-data-api-v3/videos.list',
                'observed_at':platform.get('observed_at'),'values':platform['cumulative_metrics'],'ratios':ratios},'interval_analytics':analytics}
            videos.append({'video_id':item.youtube_video_id,'title':platform['title'] or item.title,
                'published_at':platform['published_at'],'production_evidence':item.evidence,
                'analytics':analytics,'content_features':summary,
                'observations':[{'observation':'Current cumulative counters: '+json.dumps(platform['cumulative_metrics'],sort_keys=True),
                    'evidence':['youtube-data-api-v3/videos.list:'+item.youtube_video_id],
                    'sample_count':1,'uncertainty':'One video; no causal or comparative conclusion.'}]})
        except Exception:
            excluded.append({'video_id':item.youtube_video_id,'reason':'VideoAnalysisFailed'})
    findings=compare_videos(videos,max_age_difference_hours=max_age_difference_hours)
    advice=recommendations(findings)
    coverage=Counter(v['analytics']['basic']['status'] for v in videos)
    return {'report_metadata':{'generated_at':stamp.astimezone(timezone.utc).isoformat(),
                'channel':{'channel_context_id':channel_context_id,'youtube_channel_id':expected_channel},
                'analysis_window':{'start_date':start.isoformat(),'end_date_exclusive':end.isoformat(),'timezone':'America/Los_Angeles'},
                'mode':'READ_ONLY_ADVISORY','publication_age_tolerance_hours':max_age_difference_hours},
            'coverage':{'discovered_videos':len(items),'input_rows':len(inventory),'duplicate_rows':duplicates,
                'eligible_videos':len(videos),'excluded_videos':len(excluded),'exclusions':sorted(excluded,key=lambda r:r['video_id']),
                'analytics_coverage':dict(sorted(coverage.items())),
                'complete_daily_coverage_videos':sum(v['analytics']['daily_coverage']['complete'] for v in videos)},
            'videos':videos,'metrics':metrics,'content_features':features,'comparative_findings':findings,
            'evidence_status':'DESCRIPTIVE_COMPARISONS_AVAILABLE' if findings else 'INSUFFICIENT_EVIDENCE',
            'limitations':['Descriptive only; no causal claims or production changes.',
                'NO_DATA and missing metrics remain unknown, never zero.',
                'Cumulative Data API counters are separate from interval Analytics.',
                'Complete daily rows do not guarantee final or unrevisable Analytics.',
                'Calendar-day windows are not exact publication-relative 24h/48h windows.',
                'Publication-age tolerance is explicit; content/topic/distribution differences remain confounders.',
                'Multiple feature versions count as one video; absent source content remains unknown.',
                'Recommendations require at least three mutually comparable videos and remain low-confidence manual review.',
                'Impressions/CTR are not collected; no Reporting jobs are created.'],
            'recommendations':advice}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory',required=True,help='Explicit accepted production evidence JSON list; never automatic channel-wide eligibility')
    parser.add_argument('--channel-context-id',required=True)
    parser.add_argument('--youtube-channel-id',required=True)
    parser.add_argument('--start-date',required=True)
    parser.add_argument('--end-date-exclusive',required=True)
    parser.add_argument('--max-age-difference-hours',type=float,default=24)
    parser.add_argument('--read-learning',action='store_true',help='Optional enforced read-only learning metadata lookup')
    parser.add_argument('--live',action='store_true',help='Explicit authorization for read-only YouTube requests; no database persistence')
    args=parser.parse_args(argv)
    if not args.live:
        print(json.dumps({'mode':'READ_ONLY_ADVISORY','error_type':'ExplicitLiveReadRequired'}));return 1
    reader=None
    try:
        inventory=json.loads(Path(args.inventory).read_text(encoding='utf-8'))
        validate_inventory(inventory)
        start=date.fromisoformat(args.start_date);end=date.fromisoformat(args.end_date_exclusive)
        metadata=None
        if args.read_learning:
            from engine.learning.database import connect_database
            from engine.learning.repositories import LearningRepository
            with connect_database() as connection:
                metadata=read_learning_metadata(LearningRepository(connection,args.channel_context_id))
        reader=authenticated_reader(args.youtube_channel_id)
        result=build_report(inventory,reader,channel_context_id=args.channel_context_id,
            expected_channel=args.youtube_channel_id,start=start,end=end,generated_at=datetime.now(timezone.utc),
            learning_metadata=metadata,max_age_difference_hours=args.max_age_difference_hours)
        print(json.dumps(result,sort_keys=True,indent=2,allow_nan=False));return 0
    except Exception:
        print(json.dumps({'mode':'READ_ONLY_ADVISORY','error_type':'ReportFailed'}));return 1
    finally:
        if reader is not None:reader.close()


if __name__=='__main__':raise SystemExit(main())
