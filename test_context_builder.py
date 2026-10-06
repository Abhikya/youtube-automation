"""Context construction and real repository SQL paths using offline state only."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import re
import unittest
from unittest.mock import patch
import psycopg
from engine.learning.context_builder import build_context, export_context, persist_context, prepare_context, ContextBuildError, VERSION
from engine.learning.content_analysis import analyze_content
from engine.learning.content_features import extract_features
from engine.learning.repositories import LearningRepository, RepositoryConflict, RepositoryError
from engine.learning.schema import PublishedVideo,MetricSnapshot,ContentFeatures
from engine.learning.mapping import to_columns
from test_content_features import Repository,NOW
from test_content_analysis import snapshot


class OfflineConnection:
    def __init__(self):
        self.headers={};self.observations=[];self.evidence=[];self.calls=[];self.fail=None;self.rollbacks=0
    @contextmanager
    def transaction(self):
        saved=deepcopy((self.headers,self.observations,self.evidence))
        try:yield
        except Exception:
            self.headers,self.observations,self.evidence=saved
            self.rollbacks+=1
            raise
    @contextmanager
    def cursor(self,**kwargs):yield OfflineCursor(self)


class OfflineCursor:
    def __init__(self,connection):self.connection=connection;self.rows=[]
    def execute(self,query,params):
        text=query.as_string();c=self.connection;c.calls.append((text,params))
        table=next((t for t in ('learning_contexts','learning_observations','learning_evidence') if '"'+t+'"' in text),None)
        if text.startswith('INSERT'):
            if c.fail==table:raise psycopg.OperationalError('secret fixture error')
            names=re.search(r'\((.*?)\) VALUES',text)[1]
            columns=[n.strip().strip('"') for n in names.split(',')]
            row={key: value.obj if hasattr(value,'obj') else value for key,value in zip(columns,params)}
            if table=='learning_contexts':
                if row['context_id'] in c.headers:raise psycopg.errors.UniqueViolation('duplicate fixture')
                row['sealed']=False;c.headers[row['context_id']]=row
            elif table=='learning_observations':c.observations.append(row)
            else:c.evidence.append(row)
        elif text.startswith('UPDATE'):
            c.headers[params[1]]['sealed']=True
        elif text.startswith('SELECT'):
            collection=c.observations if table=='learning_observations' else c.evidence
            self.rows=[deepcopy(r) for r in collection if r['channel_context_id']==params[0] and r['context_id']==params[1]]
            if table=='learning_observations':self.rows.sort(key=lambda r:r['observation_position'])
            else:self.rows.sort(key=lambda r:(r['role'],-1 if r['observation_position'] is None else r['observation_position'],r['evidence_position']))
        return self
    def fetchall(self):return self.rows


class OfflineRepository(LearningRepository):
    def __init__(self,records):
        super().__init__(OfflineConnection(),'channel-a')
        self.records=records
    def get_context(self,key):
        row=self._connection.headers.get(key)
        return self._read_context(deepcopy(row)) if row else None
    def get_published_video(self,key):return self.records.get(('published_video',key))
    def get_snapshot(self,key):return self.records.get(('metric_snapshot',key))
    def get_features(self,key):return self.records.get(('content_features',key))


class ContextBuilderTests(unittest.TestCase):
    def setUp(self):
        repo=Repository();self.pub=repo.publication
        self.feature=extract_features('channel-a',generation=repo.generation,publication=self.pub,extracted_at=NOW)
        self.metric=snapshot()
        self.parameters=dict(context_id='context-build-1',built_at=NOW+timedelta(hours=1),start_at=NOW-timedelta(days=1),end_at=NOW+timedelta(minutes=30),max_publications=5,max_observations=50,max_evidence_links=200,max_export_bytes=100000)
    def analysis(self,metrics=None,features=None,pubs=None):
        return analyze_content('channel-a',[self.pub] if pubs is None else pubs,[self.metric] if metrics is None else metrics,[self.feature] if features is None else features)
    def build(self,analysis=None,**changes):return build_context(analysis or self.analysis(),**dict(self.parameters,**changes))
    def repository(self):
        records={('published_video',self.pub.published_video_id):self.pub,('metric_snapshot',self.metric.snapshot_id):self.metric,('content_features',self.feature.feature_record_id):self.feature}
        return OfflineRepository(records)
    def test_construct_existing_model(self):
        context=self.build();self.assertEqual(context.sample_count,1);self.assertTrue(context.observations);self.assertTrue(context.examples)
    def test_version_not_chronological_id(self):
        a=self.build();b=self.build(context_id='other');self.assertNotEqual(a.context_id,b.context_id);self.assertEqual(a.context_version,b.context_version);self.assertEqual(a.context_version,VERSION)
    def test_distinct_publications_snapshots_and_versions(self):
        later=NOW+timedelta(minutes=10)
        m=snapshot('second',collected_at=later,observation_window={'kind':'cumulative','end_at':later})
        f=self.feature.model_copy(update={'feature_record_id':'old','extraction_version':'old'})
        context=self.build(self.analysis(metrics=[self.metric,m],features=[self.feature,f]))
        self.assertEqual(context.sample_count,1);self.assertTrue(all(o.sample_count==1 for o in context.observations))
    def test_insufficient_evidence(self):self.assertIn('insufficient evidence for comparative pattern',self.build().uncertainty_notes)
    def test_observation_order(self):
        c=self.build();self.assertIn('views=93',c.observations[0].notes);self.assertEqual(c.observations[-1].notes,'insufficient evidence for comparative pattern')
    def test_evidence_order(self):
        c=self.build();self.assertEqual([r.record_type for r in c.observations[0].evidence],['published_video','metric_snapshot']);self.assertEqual(c.examples[0].record_type,'published_video')
    def test_provenance(self):
        c=self.build();self.assertIn('"source_analysis"',c.provenance.notes);self.assertIn('fixture',c.provenance.notes);self.assertEqual(c.provenance.captured_at,c.built_at)
    def test_freshness(self):self.assertEqual(self.build().data_through_at,NOW)
    def test_unknown_publication_time(self):self.assertIn('"published_at":null',self.build().provenance.notes)
    def test_no_metrics_freshness_unknown(self):self.assertIsNone(self.build(self.analysis(metrics=[])).data_through_at)
    def test_deterministic(self):self.assertEqual(self.build(),self.build())
    def test_channel_isolation(self):
        foreign=self.feature.model_copy(update={'channel_context_id':'foreign'})
        with self.assertRaises(ContextBuildError):self.build(self.analysis(features=[foreign]))
    def test_empty_rejected(self):
        with self.assertRaises(ContextBuildError):self.build(self.analysis(pubs=[],metrics=[],features=[]))
    def test_explicit_bounds_and_overflow(self):
        for change in ({'max_publications':0},{'max_observations':1},{'max_evidence_links':1},{'max_export_bytes':1}):
            with self.subTest(change=change),self.assertRaises(ContextBuildError):self.build(**change)
    def test_window_validation(self):
        with self.assertRaises(ContextBuildError):self.build(end_at=NOW+timedelta(hours=2))
    def test_future_evidence_rejected(self):
        with self.assertRaises(ContextBuildError):self.build(built_at=NOW-timedelta(minutes=1),end_at=NOW-timedelta(minutes=2))
    def test_window_excludes_snapshot(self):
        c=self.build(start_at=NOW+timedelta(minutes=1));self.assertIsNone(c.data_through_at);self.assertNotIn('views=93',str(c.observations))
    def test_export_bounded(self):
        c=self.build();self.assertIn('context-build-1',export_context(c,max_export_bytes=100000))
        with self.assertRaises(ContextBuildError):export_context(c,max_export_bytes=1)
    def test_preview_zero_writes(self):
        repo=self.repository()
        with patch('engine.learning.context_builder.preview_analysis',return_value=self.analysis()):result=prepare_context(repo,**self.parameters)
        self.assertEqual(result['status'],'PREVIEW');self.assertEqual(repo._connection.calls,[])
    def test_persist_reconstruct_ordered_children(self):
        repo=self.repository();c=self.build();result=persist_context(repo,c)
        self.assertEqual(result['status'],'INSERTED');self.assertEqual(repo.get_context(c.context_id),c)
        self.assertEqual(len(repo._connection.observations),len(c.observations))
        self.assertEqual(len(repo._connection.evidence),sum(len(o.evidence) for o in c.observations)+len(c.examples))
        self.assertTrue(repo._connection.headers[c.context_id]['sealed'])
    def test_repeated_skip_identical(self):
        repo=self.repository();c=self.build();persist_context(repo,c);before=deepcopy((repo._connection.headers,repo._connection.observations,repo._connection.evidence))
        self.assertEqual(persist_context(repo,c)['status'],'SKIPPED_IDENTICAL');self.assertEqual(before,(repo._connection.headers,repo._connection.observations,repo._connection.evidence))
    def test_immutable_conflict(self):
        repo=self.repository();c=self.build();persist_context(repo,c)
        with self.assertRaises(RepositoryConflict):persist_context(repo,self.build(context_version='different'))
        self.assertEqual(repo.get_context(c.context_id),c)
    def test_changed_source_blocks_insert(self):
        repo=self.repository();repo.records[('metric_snapshot',self.metric.snapshot_id)]=self.metric.model_copy(update={'metrics':{'views':999}})
        with self.assertRaises(RepositoryConflict):persist_context(repo,self.build())
        self.assertEqual(repo._connection.headers,{})
    def test_rollback_observation_failure(self):
        repo=self.repository();repo._connection.fail='learning_observations'
        with self.assertRaises(RepositoryError):persist_context(repo,self.build())
        self.assertEqual(repo._connection.headers,{});self.assertEqual(repo._connection.observations,[]);self.assertGreater(repo._connection.rollbacks,0)
    def test_rollback_evidence_failure(self):
        repo=self.repository();repo._connection.fail='learning_evidence'
        with self.assertRaises(RepositoryError):persist_context(repo,self.build())
        self.assertEqual(repo._connection.headers,{});self.assertEqual(repo._connection.observations,[]);self.assertEqual(repo._connection.evidence,[])
    def test_required_evidence_revalidated(self):
        c=self.build();c.observations[0].evidence.clear()
        with self.assertRaises(ValueError):self.repository().create_context(c)
    def test_missing_same_channel_reference(self):
        repo=self.repository();c=self.build();repo.records.clear()
        with self.assertRaises(RepositoryConflict):repo.create_context(c)
        self.assertEqual(repo._connection.headers,{})
    def test_cross_channel_reference(self):
        c=self.build();c.observations[0].evidence[0].channel_context_id='foreign'
        with self.assertRaises(ValueError):self.repository().create_context(c)
    def test_child_ordering_corruption_rejected(self):
        repo=self.repository();c=self.build();persist_context(repo,c);repo._connection.observations[0]['observation_position']=10
        with self.assertRaises(RepositoryError):repo.get_context(c.context_id)
    def test_evidence_ordering_corruption_rejected(self):
        repo=self.repository();c=self.build();persist_context(repo,c);repo._connection.evidence[0]['evidence_position']=10
        with self.assertRaises(RepositoryError):repo.get_context(c.context_id)
    def test_wrong_channel_child_read_rejected(self):
        repo=self.repository();c=self.build();persist_context(repo,c);repo._connection.evidence[0]['record_id']='missing'
        # FK existence is enforced by PostgreSQL; offline validator also resolves targets.
        with self.assertRaises(RepositoryConflict):repo.validate_context_evidence(repo.get_context(c.context_id))
    def test_publication_limit_selects_deterministically(self):
        second=self.pub.model_copy(update={'published_video_id':'second','youtube_video_id':'zzzzzzzzzzz'})
        later=NOW+timedelta(minutes=10)
        metric=snapshot('second',video='zzzzzzzzzzz',collected_at=later,observation_window={'kind':'cumulative','end_at':later})
        a=self.analysis(pubs=[second,self.pub],metrics=[metric,self.metric])
        b=self.analysis(pubs=[self.pub,second],metrics=[self.metric,metric])
        c=self.build(a,max_publications=1)
        self.assertEqual(c,self.build(b,max_publications=1))
        self.assertEqual(c.sample_count,1)
        self.assertEqual(c.observations[0].evidence[0].record_id,'second')
        self.assertEqual(c.data_through_at,later)
    def test_missing_counters_not_zero(self):
        c=self.build(self.analysis(metrics=[snapshot(metrics={'views':None,'likes':0})]))
        self.assertIn('views=unknown',c.observations[0].notes)
        self.assertNotIn('views=0',c.observations[0].notes)
    def test_unsealed_read_rejected(self):
        repo=self.repository();c=self.build();persist_context(repo,c)
        repo._connection.headers[c.context_id]['sealed']=False
        with self.assertRaises(RepositoryError):repo.get_context(c.context_id)
    def test_malformed_context_rolls_back_without_overwrite(self):
        repo=self.repository();c=self.build();persist_context(repo,c)
        existing=repo.get_context(c.context_id)
        altered=c.model_copy(deep=True)
        altered.observations[0].notes='Invented altered observation'
        with self.assertRaises(ContextBuildError):persist_context(repo,altered)
        self.assertEqual(repo.get_context(c.context_id),existing)
    def test_migration_has_constraints_and_immutability(self):
        from engine.learning.migrations import discover_migrations
        migration=discover_migrations()[1].content
        for value in ('CREATE TABLE learning_observations','CREATE TABLE learning_evidence','FOREIGN KEY (channel_context_id, snapshot_id)','DEFERRABLE INITIALLY DEFERRED','Contexts are immutable','WITH ORDINALITY','SET search_path FROM CURRENT'):
            self.assertIn(value,migration)


if __name__=='__main__':unittest.main()
