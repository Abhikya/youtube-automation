"""Narrow YouTube read capabilities; no persistence or resource mutations."""
from datetime import date, timedelta, datetime, timezone
import math
from googleapiclient.errors import HttpError
from engine.learning.metric_collector import YouTubeStatisticsProvider, video_id

BASIC = ('views', 'estimatedMinutesWatched', 'averageViewDuration',
         'averageViewPercentage', 'subscribersGained', 'subscribersLost')
COUNTS = {'views', 'subscribersGained', 'subscribersLost'}


def unavailable(status):
    return {'status': status, 'values': None}


def number(value, *, count=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('InvalidMetric')
    if count and int(value) != value:
        raise ValueError('InvalidCount')
    return int(value) if count else value


def rows(response, columns):
    if not isinstance(response, dict):
        raise ValueError('MalformedReport')
    headers = response.get('columnHeaders')
    if not isinstance(headers, list):
        raise ValueError('MissingColumns')
    names = [h.get('name') if isinstance(h, dict) else None for h in headers]
    if len(names) != len(set(names)) or set(names) != set(columns):
        raise ValueError('UnexpectedColumns')
    raw = response.get('rows', [])
    if not isinstance(raw, list):
        raise ValueError('MalformedRows')
    result = []
    for row in raw:
        if not isinstance(row, list) or len(row) != len(names):
            raise ValueError('MalformedRow')
        result.append(dict(zip(names, row)))
    return result


class AnalyticsReader:
    """Only channels.list, videos.list and Analytics reports.query are reachable."""
    def __init__(self, data_service, analytics_service, *, expected_channel, max_retries=3, sleep=None):
        if not isinstance(expected_channel, str) or not expected_channel.strip():
            raise ValueError('ExpectedChannelRequired')
        self._data = data_service
        self._analytics = analytics_service
        self.expected_channel = expected_channel
        self._policy = YouTubeStatisticsProvider(data_service, max_retries=max_retries, sleep=sleep)
        self._verified = False

    def _read(self, make_request):
        for attempt in range(self._policy.max_retries + 1):
            try:
                return make_request().execute(num_retries=0), None
            except Exception as exc:
                transient, delay = self._policy._retry_policy(exc, attempt)
                if transient:
                    if attempt == self._policy.max_retries or delay > 60:
                        return None, 'RETRYABLE'
                    self._policy.sleep(delay)
                    continue
                if isinstance(exc, HttpError):
                    if exc.resp.status in {401, 403}:return None, 'AUTHORIZATION_FAILED'
                    if exc.resp.status == 400:return None, 'UNSUPPORTED'
                return None, 'API_ERROR'

    def verify_channel(self):
        self._verified = False
        response, error = self._read(lambda:self._data.channels().list(part='id', mine=True))
        if error:return {'status':error}
        if (not isinstance(response, dict) or response.get('nextPageToken') or
            not isinstance(response.get('items'), list) or len(response['items']) != 1 or
            not isinstance(response['items'][0], dict) or response['items'][0].get('id') != self.expected_channel):
            return {'status':'CHANNEL_MISMATCH'}
        self._verified = True
        return {'status':'MATCH', 'channel_id':self.expected_channel}

    def video(self, identifier):
        video_id(identifier)
        if not self._verified:raise ValueError('ChannelPreflightRequired')
        response, error = self._read(lambda:self._data.videos().list(part='snippet,status,statistics', id=identifier))
        if error:return {'status':error}
        try:
            from engine.learning.metric_collector import parse_response
            stats = parse_response(response, identifier)
            if stats['youtube_channel_id'] != self.expected_channel:return {'status':'CHANNEL_MISMATCH'}
            item=response['items'][0]
            snippet=item['snippet']
            privacy=item['status']['privacyStatus']
            published=snippet['publishedAt']
            from pydantic import TypeAdapter, AwareDatetime
            stamp=TypeAdapter(AwareDatetime).validate_python(published)
            if privacy not in {'public','private','unlisted'}:raise ValueError('Privacy')
            return {'status':'AVAILABLE','video_id':identifier,'channel_id':stats['youtube_channel_id'],
                    'title':snippet.get('title'), 'published_at':stamp.isoformat(), 'privacy':privacy,
                    'cumulative_metrics':stats['metrics'],
                    'observed_at':datetime.now(timezone.utc).isoformat()}
        except Exception:return {'status':'INVALID_RESPONSE'}

    def _query(self, identifier, start, end, metrics, dimensions):
        if not self._verified:raise ValueError('ChannelPreflightRequired')
        return self._read(lambda:self._analytics.reports().query(
            ids='channel==MINE', startDate=start.isoformat(), endDate=(end-timedelta(days=1)).isoformat(),
            metrics=','.join(metrics), dimensions=dimensions, filters='video=='+identifier))

    def collect(self, identifier, start, end):
        video_id(identifier)
        if type(start) is not date or type(end) is not date or start >= end:raise ValueError('InvalidWindow')
        window={'start_date':start.isoformat(),'end_date_exclusive':end.isoformat(),
                'timezone':'America/Los_Angeles','source':'youtube-analytics-v2/reports.query'}
        result={'window':window}
        response,error=self._query(identifier,start,end,BASIC,'video')
        basic=unavailable(error or 'INVALID_RESPONSE')
        if not error:
            try:
                parsed=rows(response,('video',*BASIC))
                if not parsed:basic=unavailable('NO_DATA')
                elif len(parsed)!=1 or parsed[0]['video']!=identifier:raise ValueError('WrongVideo')
                else:basic={'status':'AVAILABLE','values':{k:number(parsed[0][k],count=k in COUNTS) for k in BASIC}}
            except Exception:basic=unavailable('INVALID_RESPONSE')
        result['basic']=basic
        # Missing daily rows are unknown, not zero. Daily coverage is a conservative
        # comparability gate, not a promise that YouTube will never revise a report.
        response,error=self._query(identifier,start,end,BASIC,'day')
        coverage={'status':error or 'INVALID_RESPONSE','complete':False,'available_dates':[]}
        if not error:
            try:
                daily=rows(response,('day',*BASIC));days=[]
                for row in daily:
                    day=date.fromisoformat(row['day'])
                    if not start<=day<end or day.isoformat() in days:raise ValueError('InvalidDay')
                    for metric in BASIC:number(row[metric],count=metric in COUNTS)
                    days.append(day.isoformat())
                expected=[(start+timedelta(days=i)).isoformat() for i in range((end-start).days)]
                coverage={'status':'AVAILABLE' if daily else 'NO_DATA','complete':sorted(days)==expected,
                          'available_dates':sorted(days)}
            except Exception:pass
        result['daily_coverage']=coverage
        response,error=self._query(identifier,start,end,('views','estimatedMinutesWatched'),'insightTrafficSourceType')
        traffic=unavailable(error or 'INVALID_RESPONSE')
        if not error:
            try:
                parsed=rows(response,('insightTrafficSourceType','views','estimatedMinutesWatched'))
                seen=set()
                for row in parsed:
                    key=row['insightTrafficSourceType']
                    if not isinstance(key,str) or not key.strip() or key in seen:raise ValueError('InvalidTrafficSource')
                    seen.add(key)
                    row['views']=number(row['views'],count=True)
                    row['estimatedMinutesWatched']=number(row['estimatedMinutesWatched'])
                traffic={'status':'AVAILABLE','values':sorted(parsed,key=lambda r:r['insightTrafficSourceType'])} if parsed else unavailable('NO_DATA')
            except Exception:pass
        result['traffic_sources']=traffic
        response,error=self._query(identifier,start,end,('audienceWatchRatio',),'elapsedVideoTimeRatio')
        retention=unavailable(error or 'INVALID_RESPONSE')
        if not error:
            try:
                parsed=rows(response,('elapsedVideoTimeRatio','audienceWatchRatio'));seen=set()
                for row in parsed:
                    point=number(row['elapsedVideoTimeRatio']);ratio=number(row['audienceWatchRatio'])
                    if point>1 or point in seen:raise ValueError('InvalidRetentionPoint')
                    seen.add(point);row.update(elapsedVideoTimeRatio=point,audienceWatchRatio=ratio)
                retention={'status':'AVAILABLE','values':sorted(parsed,key=lambda r:r['elapsedVideoTimeRatio'])} if parsed else unavailable('NO_DATA')
            except Exception:pass
        result['retention']=retention
        return result

    def close(self):
        for service in (self._analytics,self._data):
            try:service.close()
            except Exception:pass


def authenticated_reader(expected_channel):
    """Phase 2 authentication only: refresh in memory, never save or consent.

    Existing local credentials/scopes are retained. Services expose only the
    reader's read calls; production authentication is not imported or changed.
    """
    import pickle
    from config.settings import YOUTUBE_TOKEN_FILE
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    data = None
    try:
        with open(YOUTUBE_TOKEN_FILE, 'rb') as token:
            credentials = pickle.load(token)
        if not credentials.valid:
            if not credentials.expired or not credentials.refresh_token:
                raise ValueError('CredentialsUnavailable')
            credentials.refresh(Request())
        if not credentials.valid:
            raise ValueError('CredentialsUnavailable')
        data = build('youtube', 'v3', credentials=credentials, cache_discovery=False)
        analytics = build('youtubeAnalytics', 'v2', credentials=credentials, cache_discovery=False)
        return AnalyticsReader(data, analytics, expected_channel=expected_channel)
    except Exception:
        if data is not None:
            try:data.close()
            except Exception:pass
        raise ValueError('AnalyticsAuthenticationFailed') from None
