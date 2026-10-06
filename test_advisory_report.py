"""Offline safety and Analytics fixtures; no network, database or generation."""
import copy
from contextlib import contextmanager
from datetime import date,datetime,timezone,timedelta
import io,json,unittest
from unittest.mock import Mock,MagicMock,patch
import httplib2
from googleapiclient.errors import HttpError
from engine.learning.analytics_reader import AnalyticsReader,BASIC
from engine.learning.advisory_report import build_report,compare_videos,recommendations,main,read_learning_metadata

START=date(2026,9,20);END=date(2026,9,22);NOW=datetime(2026,10,6,tzinfo=timezone.utc)
IDS=['abcdefghijk','bcdefghijkl','cdefghijklm']


def inventory(identifier=IDS[0],**changes):
    value=dict(youtube_video_id=identifier,channel_context_id='context',youtube_channel_id='platform',
        classification='PRODUCTION',accepted_production=True,evidence=['checkpoint:run-1:slot-10'],
        title='Stored title',script='In 1977, a signal appeared. Its source remains unknown.')
    value.update(changes);return value


def analytics(value=50):
    return {'window':{'start_date':START.isoformat(),'end_date_exclusive':END.isoformat(),
             'timezone':'America/Los_Angeles','source':'youtube-analytics-v2/reports.query'},
        'basic':{'status':'AVAILABLE','values':{'views':100,'estimatedMinutesWatched':50,
            'averageViewDuration':20,'averageViewPercentage':value,'subscribersGained':1,'subscribersLost':0}},
        'daily_coverage':{'status':'AVAILABLE','complete':True,'available_dates':['2026-09-20','2026-09-21']},
        'traffic_sources':{'status':'AVAILABLE','values':[{'insightTrafficSourceType':'SHORTS','views':100,'estimatedMinutesWatched':50}]},
        'retention':{'status':'AVAILABLE','values':[{'elapsedVideoTimeRatio':0.1,'audienceWatchRatio':0.8}]}}


class Reader:
    expected_channel='platform'
    def __init__(self):self.calls=[];self.analytics={};self.platform={}
    def verify_channel(self):return {'status':'MATCH'}
    def video(self,identifier):
        self.calls.append(('video',identifier))
        return self.platform.get(identifier,{'status':'AVAILABLE','video_id':identifier,'channel_id':'platform',
            'title':'Live title','privacy':'public','published_at':'2026-09-18T10:00:00+00:00',
            'observed_at':NOW.isoformat(),'cumulative_metrics':{'views':93,'likes':6,'comments':0}})
    def collect(self,identifier,start,end):
        self.calls.append(('analytics',identifier));return copy.deepcopy(self.analytics.get(identifier,analytics(50+IDS.index(identifier))))
    def close(self):pass


class ReportTests(unittest.TestCase):
    def report(self,items=None,reader=None,**kwargs):
        return build_report(items or [inventory()],reader or Reader(),channel_context_id='context',expected_channel='platform',
            start=START,end=END,generated_at=NOW,**kwargs)

    def test_one_video_insufficient_no_advice(self):
        result=self.report();self.assertEqual(result['evidence_status'],'INSUFFICIENT_EVIDENCE');self.assertEqual(result['recommendations'],[])
        self.assertEqual(result['coverage']['eligible_videos'],1)
        self.assertEqual(result['metrics'][IDS[0]]['cumulative_data_api']['values']['comments'],0)

    def test_three_distinct_comparable_low_confidence_manual_advice(self):
        result=self.report([inventory(v) for v in IDS]);self.assertTrue(result['comparative_findings']);self.assertTrue(result['recommendations'])
        for advice in result['recommendations']:
            self.assertEqual(advice['confidence'],'low');self.assertEqual(advice['action'],'MANUAL_REVIEW_REQUIRED');self.assertTrue(advice['evidence'])
        self.assertIn('content_characteristics',result['comparative_findings'][0])

    def test_two_videos_no_recommendation(self):
        result=self.report([inventory(v) for v in IDS[:2]]);self.assertTrue(result['comparative_findings']);self.assertEqual(result['recommendations'],[])

    def test_test_unknown_wrong_channel_unaccepted_excluded_before_api(self):
        items=[inventory(IDS[0],classification='TEST'),inventory(IDS[1],classification='UNKNOWN'),inventory(IDS[2],youtube_channel_id='other'),
            inventory('defghijklmn',accepted_production=False)]
        reader=Reader();result=self.report(items,reader);self.assertEqual(result['coverage']['excluded_videos'],4);self.assertEqual(reader.calls,[])

    def test_private_unlisted_future_unpublished_excluded(self):
        for changes in ({'privacy':'private'},{'privacy':'unlisted'},{'published_at':'2026-12-01T00:00:00Z'}):
            reader=Reader();platform=reader.video(IDS[0]);platform.update(changes);reader.platform[IDS[0]]=platform;reader.calls=[]
            result=self.report(reader=reader);self.assertEqual(result['coverage']['eligible_videos'],0)
            self.assertNotIn(('analytics',IDS[0]),reader.calls)

    def test_wrong_api_channel_rejected(self):
        reader=Reader();platform=reader.video(IDS[0]);platform['channel_id']='other';reader.platform[IDS[0]]=platform
        self.assertEqual(self.report(reader=reader)['coverage']['eligible_videos'],0)

    def test_channel_preflight_failure_stops(self):
        reader=Reader();reader.verify_channel=lambda:{'status':'CHANNEL_MISMATCH'}
        with self.assertRaises(ValueError):self.report(reader=reader)
        self.assertEqual(reader.calls,[])

    def test_no_data_stays_none_and_not_compared(self):
        reader=Reader();raw=analytics();raw['basic']={'status':'NO_DATA','values':None};reader.analytics[IDS[0]]=raw
        result=self.report([inventory(v) for v in IDS[:2]],reader)
        self.assertIsNone(result['metrics'][IDS[0]]['interval_analytics']['basic']['values']);self.assertEqual(result['comparative_findings'],[])

    def test_incomplete_daily_coverage_not_compared(self):
        reader=Reader();raw=analytics();raw['daily_coverage']['complete']=False;reader.analytics[IDS[0]]=raw
        self.assertEqual(self.report([inventory(v) for v in IDS[:2]],reader)['comparative_findings'],[])

    def test_different_windows_not_compared(self):
        result=self.report([inventory(v) for v in IDS[:2]])
        result['videos'][1]['analytics']['window']['end_date_exclusive']='2026-09-23'
        self.assertEqual(compare_videos(result['videos']),[])

    def test_different_sources_not_compared(self):
        result=self.report([inventory(v) for v in IDS[:2]])
        result['videos'][1]['analytics']['window']['source']='other'
        self.assertEqual(compare_videos(result['videos']),[])

    def test_publication_age_difference_and_partial_first_day(self):
        result=self.report([inventory(v) for v in IDS[:2]])
        for timestamp in ('2026-09-10T00:00:00+00:00','2026-09-20T20:00:00+00:00'):
            result['videos'][1]['published_at']=timestamp;self.assertEqual(compare_videos(result['videos']),[])

    def test_comparison_deterministic_no_input_mutation(self):
        result=self.report([inventory(v) for v in IDS]);original=copy.deepcopy(result['videos'])
        self.assertEqual(compare_videos(original),compare_videos(list(reversed(original))))
        self.assertEqual(original,result['videos'])

    def test_duplicate_same_video_not_independent(self):
        result=self.report([inventory(),inventory()]);self.assertEqual(result['coverage']['discovered_videos'],1)
        self.assertEqual(result['coverage']['duplicate_rows'],[IDS[0]]);self.assertEqual(result['recommendations'],[])

    def test_conflicting_duplicate_rejected(self):
        with self.assertRaises(ValueError):self.report([inventory(),inventory(title='Other')])

    def test_unknown_content_or_no_difference_no_advice(self):
        result=self.report([inventory(v,script=None) for v in IDS]);self.assertEqual(result['recommendations'],[])
        reader=Reader();reader.analytics={v:analytics(50) for v in IDS}
        result=self.report([inventory(v) for v in IDS],reader);self.assertEqual(result['recommendations'],[])

    def test_recommendations_without_evidence_rejected(self):
        result=self.report([inventory(v) for v in IDS]);findings=result['comparative_findings']
        for row in findings:row['evidence']=[]
        self.assertEqual(recommendations(findings),[])

    def test_missing_content_unknown_not_zero(self):
        result=self.report([inventory(script=None,title=None,description=None,hashtags=None)])
        content=result['content_features'][IDS[0]]
        for field in ('script_word_count','title_character_count','description_character_count','hashtag_count'):
            self.assertIsNone(content[field])
        self.assertEqual(content['hook']['knowledge'],'unknown')

    def test_retention_exact_points_only(self):
        result=self.report([inventory(v) for v in IDS[:2]])
        retention=[r for r in result['comparative_findings'] if r['metric']=='audienceWatchRatio']
        self.assertEqual([r['elapsed_video_time_ratio'] for r in retention],[0.1])

    def test_per_video_failure_does_not_stop_later_video(self):
        reader=Reader();reader.platform[IDS[0]]={'status':'API_ERROR'}
        result=self.report([inventory(v) for v in IDS[:2]],reader);self.assertEqual(result['coverage']['eligible_videos'],1)

    def test_no_generation_upload_or_phase1_write(self):
        import builtins,socket
        with patch.object(builtins,'open',side_effect=AssertionError('Filesystem access forbidden')),\
             patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden')):
            result=self.report([inventory(v) for v in IDS]);self.assertTrue(result['recommendations'])
        # Read-only reporting modules have no direct production execution imports.
        import ast
        from pathlib import Path
        for path in ('engine/learning/analytics_reader.py','engine/learning/advisory_report.py'):
            tree=ast.parse(Path(path).read_text());calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call)]
            prohibited={'generate_script','generate_video','generate_video_ideas','generate_metadata','upload_video','upload_prepared_video',
                'insert_snapshot','create_features','persist_context','execute_import','write_text','write_bytes','unlink','replace'}
            for call in calls:
                name=call.func.attr if isinstance(call.func,ast.Attribute) else call.func.id if isinstance(call.func,ast.Name) else ''
                self.assertNotIn(name,prohibited)

    def test_cli_requires_explicit_live_flag_without_auth(self):
        with patch('engine.learning.advisory_report.authenticated_reader') as factory,patch('sys.stdout',new_callable=io.StringIO):
            code=main(['--inventory','unused','--channel-context-id','context','--youtube-channel-id','platform',
                '--start-date','2026-09-20','--end-date-exclusive','2026-09-22'])
        self.assertEqual(code,1);factory.assert_not_called()

    def test_repository_boundary_readonly_no_persistence(self):
        from psycopg.pq import TransactionStatus
        repository=MagicMock();repository._connection.info.transaction_status=TransactionStatus.IDLE;repository.channel_context_id='context'
        repository.transaction.return_value.__enter__.return_value=repository
        repository.get_profile.return_value=Mock()
        repository.list_published_videos.return_value=[];repository.list_features.return_value=[];repository.list_generations.return_value=[]
        read_learning_metadata(repository)
        repository._connection.execute.assert_called_once_with('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        for name in ('create_generation','create_published_video','upsert_profile','insert_snapshot','create_features','create_context'):
            getattr(repository,name).assert_not_called()

    def test_invalid_age_tolerance_or_unfinished_day_rejected(self):
        with self.assertRaises(ValueError):compare_videos([],max_age_difference_hours=25)
        with self.assertRaises(ValueError):build_report([],Reader(),channel_context_id='context',expected_channel='platform',start=date(2026,10,6),end=date(2026,10,7),generated_at=NOW)


def api_rows(columns,data):
    return {'columnHeaders':[{'name':c} for c in columns],'rows':data}


def services():
    data=Mock();data.channels.return_value.list.return_value.execute.return_value={'items':[{'id':'platform'}]}
    data.videos.return_value.list.return_value.execute.return_value={'items':[{'id':IDS[0],'snippet':{'channelId':'platform','title':'Title','publishedAt':'2026-09-18T10:00:00Z'},'status':{'privacyStatus':'public'},'statistics':{'viewCount':'93','likeCount':'6','commentCount':'0'}}]}
    api=Mock()
    def query(**args):
        dim=args['dimensions'];req=Mock()
        if dim=='video':response=api_rows(['video',*BASIC],[[IDS[0],100,20,12,50,1,0]])
        elif dim=='day':response=api_rows(['day',*BASIC],[['2026-09-20',50,10,12,50,1,0],['2026-09-21',50,10,12,50,0,0]])
        elif dim=='insightTrafficSourceType':response=api_rows([dim,'views','estimatedMinutesWatched'],[['SHORTS',80,16]])
        else:response=api_rows([dim,'audienceWatchRatio'],[[0.1,1.2],[0.25,0.7]])
        req.execute.return_value=response;return req
    api.reports.return_value.query.side_effect=query
    return data,api


class ReaderTests(unittest.TestCase):
    def reader(self,**kwargs):
        data,api=services();reader=AnalyticsReader(data,api,expected_channel='platform',**kwargs);reader.verify_channel();return reader,data,api

    def test_only_read_calls_and_metrics_supported(self):
        reader,data,api=self.reader();video=reader.video(IDS[0]);result=reader.collect(IDS[0],START,END)
        self.assertEqual(video['cumulative_metrics']['comments'],0);self.assertEqual(result['basic']['values']['averageViewDuration'],12)
        self.assertTrue(result['daily_coverage']['complete']);self.assertEqual(result['traffic_sources']['values'][0]['insightTrafficSourceType'],'SHORTS')
        self.assertEqual(result['retention']['values'][0]['audienceWatchRatio'],1.2)
        for call in api.reports.return_value.query.call_args_list:
            self.assertEqual(call.kwargs['endDate'],'2026-09-21');self.assertEqual(call.kwargs['ids'],'channel==MINE')
        self.assertEqual({call[0] for call in data.mock_calls},{'channels','channels().list','channels().list().execute','videos','videos().list','videos().list().execute'})

    def test_channel_mismatch_blocks_query(self):
        data,api=services();data.channels.return_value.list.return_value.execute.return_value={'items':[{'id':'other'}]}
        reader=AnalyticsReader(data,api,expected_channel='platform');self.assertEqual(reader.verify_channel()['status'],'CHANNEL_MISMATCH')
        with self.assertRaises(ValueError):reader.collect(IDS[0],START,END)
        api.reports.assert_not_called()

    def test_empty_rows_no_data_not_zero(self):
        reader,data,api=self.reader();api.reports.return_value.query.side_effect=None
        api.reports.return_value.query.return_value.execute.return_value=api_rows(['video',*BASIC],[])
        result=reader.collect(IDS[0],START,END);self.assertEqual(result['basic'],{'status':'NO_DATA','values':None})

    def test_malformed_retention_and_traffic_not_included(self):
        reader,data,api=self.reader();original=api.reports.return_value.query.side_effect
        def query(**args):
            request=original(**args)
            if args['dimensions']=='elapsedVideoTimeRatio':request.execute.return_value=api_rows(['elapsedVideoTimeRatio','audienceWatchRatio'],[[1.1,0.8]])
            if args['dimensions']=='insightTrafficSourceType':request.execute.return_value=api_rows(['insightTrafficSourceType','views','estimatedMinutesWatched'],[['SHORTS',-1,2]])
            return request
        api.reports.return_value.query.side_effect=query;result=reader.collect(IDS[0],START,END)
        self.assertEqual(result['retention']['status'],'INVALID_RESPONSE');self.assertEqual(result['traffic_sources']['status'],'INVALID_RESPONSE')
        self.assertIsNone(result['retention']['values'])

    def test_duplicate_columns_and_nonfinite_values_rejected(self):
        reader,data,api=self.reader();original=api.reports.return_value.query.side_effect
        def query(**args):
            request=original(**args)
            if args['dimensions']=='video':request.execute.return_value=api_rows(['video',*BASIC],[[IDS[0],100,float('nan'),12,50,1,0]])
            return request
        api.reports.return_value.query.side_effect=query
        self.assertEqual(reader.collect(IDS[0],START,END)['basic']['status'],'INVALID_RESPONSE')

    def test_429_retry_after_and_bounded_exhaustion(self):
        sleep=Mock();reader,data,api=self.reader(sleep=sleep,max_retries=2)
        error=HttpError(httplib2.Response({'status':'429','retry-after':'3'}),b'{"error":{"message":"secret"}}')
        request=Mock();request.execute.side_effect=[error,{'items':[{'id':'platform'}]}]
        data.channels.return_value.list.return_value=request
        self.assertEqual(reader.verify_channel()['status'],'MATCH');sleep.assert_called_once_with(3)
        request.execute.side_effect=error;self.assertEqual(reader.verify_channel()['status'],'RETRYABLE')

    def test_permanent_authorization_error_not_retried_sanitized(self):
        reader,data,api=self.reader(sleep=Mock())
        error=HttpError(httplib2.Response({'status':'403'}),b'{"error":{"message":"private_token"}}')
        api.reports.return_value.query.side_effect=error
        result=reader.collect(IDS[0],START,END);self.assertEqual(result['basic']['status'],'AUTHORIZATION_FAILED')
        self.assertNotIn('private_token',json.dumps(result));reader._policy.sleep.assert_not_called()

    def test_long_retry_after_defers(self):
        reader,data,api=self.reader(sleep=Mock());api.reports.return_value.query.side_effect=HttpError(httplib2.Response({'status':'429','retry-after':'120'}),b'{}')
        self.assertEqual(reader.collect(IDS[0],START,END)['basic']['status'],'RETRYABLE');reader._policy.sleep.assert_not_called()

    def authenticate(self, credentials):
        import config.settings  # Load real configuration before mocking token-file reads.
        from engine.learning.analytics_reader import authenticated_reader
        with patch('builtins.open', unittest.mock.mock_open(read_data=b'local')), \
             patch('pickle.load', return_value=credentials), \
             patch('google.auth.transport.requests.Request') as request, \
             patch('googleapiclient.discovery.build') as build:
            reader=authenticated_reader('platform')
            self.assertEqual([c.args[:2] for c in build.call_args_list], [('youtube','v3'),('youtubeAnalytics','v2')])
            for call in build.call_args_list:self.assertIs(call.kwargs['credentials'],credentials)
            reader.close()
        return request

    def test_expired_token_refreshes_in_memory_no_save(self):
        credentials=Mock(valid=False,expired=True,refresh_token='fixture')
        credentials.refresh.side_effect=lambda request:setattr(credentials,'valid',True)
        request=self.authenticate(credentials)
        credentials.refresh.assert_called_once_with(request.return_value)

    def test_valid_token_not_refreshed(self):
        credentials=Mock(valid=True)
        self.authenticate(credentials);credentials.refresh.assert_not_called()

    def test_refresh_failure_closed_sanitized(self):
        from engine.learning.analytics_reader import authenticated_reader
        credentials=Mock(valid=False,expired=True,refresh_token='fixture')
        credentials.refresh.side_effect=RuntimeError('secret')
        with patch('builtins.open',unittest.mock.mock_open()),patch('pickle.load',return_value=credentials), \
             patch('google.auth.transport.requests.Request'),patch('googleapiclient.discovery.build') as build:
            with self.assertRaisesRegex(ValueError,'^AnalyticsAuthenticationFailed$'):authenticated_reader('platform')
            build.assert_not_called()

    def test_missing_refresh_token_closed(self):
        from engine.learning.analytics_reader import authenticated_reader
        credentials=Mock(valid=False,expired=True,refresh_token=None)
        with patch('builtins.open',unittest.mock.mock_open()),patch('pickle.load',return_value=credentials), \
             patch('googleapiclient.discovery.build') as build:
            with self.assertRaises(ValueError):authenticated_reader('platform')
            credentials.refresh.assert_not_called();build.assert_not_called()

    def test_checked_in_inventory_exact_accepted_ids_and_channel(self):
        from pathlib import Path
        from engine.learning.advisory_report import validate_inventory
        raw=json.loads(Path('phase2/reporting/accepted_production_inventory.json').read_text())
        items,duplicates=validate_inventory(raw)
        expected=set('ZZ3bkXE4OYM s7GoVM1KnoU 3Vz-T71XY94 Revzhi9P8Y8 n4M3fSL776k wYud-PDHqSk _11Kh8-88Mk T-II7Hvqpz0 zxZVerSkpr4 ZJxZghuCkY4 UHiGTtQm5ZU 4IbM6iAMmvk k9wbPTVzUPw RJ-An9Dj1JE'.split())
        self.assertEqual({i.youtube_video_id for i in items},expected);self.assertEqual(duplicates,[])
        for i in items:
            self.assertEqual(i.channel_context_id,'4491252f-3db8-5466-891b-a15a7d3a7271')
            self.assertEqual(i.youtube_channel_id,'UCR2L8UE-pc1Hd7U3AurrECQ')
            self.assertEqual(i.classification,'PRODUCTION');self.assertTrue(i.accepted_production);self.assertTrue(i.evidence)
        self.assertEqual(len(validate_inventory(raw+raw)[0]),14)



if __name__=='__main__':unittest.main()
