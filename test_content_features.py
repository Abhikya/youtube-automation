"""Offline deterministic extraction tests; no service or database connections."""
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from engine.learning.schema import GenerationRecord, PublishedVideo, ContentFeatures
from engine.learning.content_features import extract_features, extract_stored, extract_publications

NOW = datetime(2026,10,5,tzinfo=timezone.utc)


def generation(**updates):
    values=dict(channel_context_id="channel-a",provenance={"source":"fixture"},generation_id="gen-1",
                idea="An idea",topic="An explicit topic",script="How did this happen? Here is the evidence.",
                title="An event?",description="Some evidence.",hashtags=["#Event"],generated_at=NOW)
    values.update(updates)
    return GenerationRecord(**values)


class Repository:
    channel_context_id="channel-a"
    def __init__(self):
        self.generation=generation()
        self.publication=PublishedVideo(channel_context_id="channel-a",provenance={"source":"fixture"},
            published_video_id="pub-1",youtube_video_id="abcdefghijk",created_at=NOW,updated_at=NOW,
            generation_reference={"channel_context_id":"channel-a","record_type":"generation","record_id":"gen-1"})
        self.features={}
    def get_generation(self, identifier):return self.generation if identifier=="gen-1" else None
    def get_published_video(self, identifier):return self.publication if identifier=="pub-1" else None
    def get_features(self, identifier):return self.features.get(identifier)
    def create_features(self, record):self.features[record.feature_record_id]=record
    def list_published_videos(self,limit,offset):return [self.publication][offset:offset+limit]
    @contextmanager
    def transaction(self):
        before=copy.deepcopy(self.features)
        try:yield
        except Exception:
            self.features=before
            raise


class ContentExtractionTests(unittest.TestCase):
    def extract(self, **updates):
        return extract_features("channel-a",generation=generation(**updates),extracted_at=NOW)
    def test_determinism_and_title_characteristics(self):
        a=self.extract()
        self.assertEqual(a,self.extract())
        self.assertEqual([f.value for f in a.title_patterns],["characters=9","words=2","question_marks=1"])
        self.assertTrue(all(f.knowledge=="inferred" for f in a.title_patterns))
    def test_script_description_hashtag_characteristics(self):
        values=[f.value for f in self.extract().content_structure]
        self.assertIn("script_words=8",values)
        self.assertIn("description_characters=14",values)
        self.assertIn("hashtag_count=1",values)
        self.assertEqual(self.extract().hashtags[0].knowledge,"known")
    def test_explicit_hook(self):
        hook=self.extract(hook="Explicit opening").hook_patterns[0]
        self.assertEqual(hook.value,"Explicit opening")
        self.assertEqual(hook.knowledge,"known")
    def test_derived_hook_has_provenance(self):
        hook=self.extract().hook_patterns[0]
        self.assertEqual(hook.value,"How did this happen?")
        self.assertEqual(hook.knowledge,"inferred")
        self.assertIn("Derived",hook.notes)
    def test_missing_hook_without_complete_opening(self):
        hook=self.extract(script="An unfinished opening").hook_patterns[0]
        self.assertIsNone(hook.value)
        self.assertEqual(hook.knowledge,"unknown")
    def test_missing_script_publication_only_preserves_unknown(self):
        pub=Repository().publication.model_copy(update={"generation_reference":None,"upload_metadata":None})
        result=extract_features("channel-a",publication=pub,extracted_at=NOW)
        self.assertTrue(all(f.value is None for f in result.title_patterns))
        self.assertIsNone(result.hook_patterns[0].value)
        self.assertIsNone(result.content_structure[0].value)
        self.assertIsNone(result.content_structure[3].value)
        self.assertIsNone(result.hashtags)
    def test_unknown_duration_and_topic_not_invented(self):
        result=self.extract(topic=None,hashtags=None)
        self.assertEqual(result.topics[0].knowledge,"unknown")
        self.assertIsNone(result.content_structure[-1].value)
        self.assertIsNone(result.categories)
        self.assertIsNone(result.keywords)
    def test_measured_duration_and_explicit_empty_hashtags(self):
        result=self.extract(measured_duration_seconds=33.12,hashtags=[])
        self.assertEqual(result.content_structure[-1].value,"measured_duration_seconds=33.12")
        self.assertEqual(result.content_structure[-1].knowledge,"known")
        self.assertEqual(result.content_structure[3].value,"hashtag_count=0")
    def test_channel_isolation_and_link_integrity(self):
        with self.assertRaises(ValueError):extract_features("other",generation=generation(),extracted_at=NOW)
        pub=Repository().publication
        with self.assertRaises(ValueError):
            extract_features("channel-a",generation=generation(generation_id="different"),publication=pub,extracted_at=NOW)
    def test_idempotency_preserves_first_extraction_time(self):
        repo=Repository()
        first=extract_stored(repo,published_video_id="pub-1",persist=True,clock=lambda:NOW)
        second=extract_stored(repo,published_video_id="pub-1",persist=True,clock=lambda:NOW+timedelta(days=1))
        self.assertEqual(second["status"],"SKIP_ALREADY_EXISTS")
        self.assertEqual(first["features"],second["features"])
        self.assertEqual(len(repo.features),1)
    def test_conflict_detected_without_overwrite(self):
        repo=Repository()
        extract_stored(repo,generation_id="gen-1",persist=True,clock=lambda:NOW)
        old=copy.deepcopy(repo.features)
        repo.generation=generation(title="Changed evidence")
        self.assertEqual(extract_stored(repo,generation_id="gen-1",persist=True,clock=lambda:NOW)["status"],"CONFLICT")
        self.assertEqual(repo.features,old)
    def test_preview_and_batch_do_not_persist(self):
        repo=Repository()
        results=extract_publications(repo,page_size=1,clock=lambda:NOW)
        self.assertEqual(results["results"][0]["status"],"PREVIEW")
        self.assertEqual(repo.features,{})
    def test_malformed_metadata_and_missing_source_safe(self):
        repo=Repository()
        repo.publication=repo.publication.model_copy(update={"generation_reference":None,"upload_metadata":{"Title":12}})
        self.assertEqual(extract_stored(repo,published_video_id="pub-1")["status"],"INVALID_OR_FAILED")
        self.assertEqual(extract_stored(repo,generation_id="missing")["status"],"INVALID_OR_FAILED")
        self.assertEqual(repo.features,{})
    def test_persistence_failure_rolls_back_and_sanitizes(self):
        repo=Repository()
        repo.create_features=Mock(side_effect=RuntimeError("secret-driver-password"))
        result=extract_stored(repo,generation_id="gen-1",persist=True,clock=lambda:NOW)
        self.assertEqual(result,{"status":"INVALID_OR_FAILED"})
        self.assertEqual(repo.features,{})
    def test_historical_preview_record_remains_distinct(self):
        plan=json.loads(Path("phase2_6_import_plan.json").read_text())
        prior=ContentFeatures.model_validate(plan["rows"][3]["record"])
        gen=GenerationRecord.model_validate(plan["rows"][1]["record"])
        result=extract_features(gen.channel_context_id,generation=gen,extracted_at=NOW)
        self.assertNotEqual(result.feature_record_id,prior.feature_record_id)
        self.assertEqual(prior.extraction_version,"preview-v1")
    def test_hashtag_priority_fallback_and_provenance(self):
        pub=Repository().publication.model_copy(update={"upload_metadata":{"Hashtags":["#Publication"]}})
        for tags, expected, source in ((["#Generation"],["#Generation"],"GenerationRecord"),
                                       (None,["#Publication"],"PublishedVideo"),([],[],None)):
            result=extract_features("channel-a",generation=generation(hashtags=tags),publication=pub,extracted_at=NOW)
            self.assertEqual([f.value for f in result.hashtags],expected)
            if source:self.assertIn(source,result.hashtags[0].notes)
        pub=pub.model_copy(update={"upload_metadata":None})
        self.assertIsNone(extract_features("channel-a",generation=generation(hashtags=None),publication=pub,extracted_at=NOW).hashtags)
        pub=pub.model_copy(update={"upload_metadata":{"Hashtags":[]}})
        self.assertEqual(extract_features("channel-a",generation=generation(hashtags=None),publication=pub,extracted_at=NOW).hashtags,[])

    def test_abbreviations_do_not_end_derived_hook(self):
        for prefix in ("Dr.","Mr.","Mrs.","Ms.","Prof.","Sr.","Jr."):
            script=prefix+" Smith examined the evidence. Another sentence follows."
            hook=self.extract(script=script).hook_patterns[0]
            self.assertEqual(hook.value,prefix+" Smith examined the evidence.")
            self.assertEqual(hook.knowledge,"inferred")
            self.assertIn("heuristics",hook.notes)
            self.assertIn("not verified",hook.notes)

    def test_ambiguous_hook_boundaries_remain_unknown(self):
        from engine.learning.content_features import opening_sentence
        for script in (None,""," ","Mr. Brown...", "Prof. ...", "Wait... What happened?",
                       "Was it real?!", "J. Smith found evidence.", "The value was 3.14 units."):
            self.assertIsNone(opening_sentence(script))
        self.assertEqual(opening_sentence("The evidence was examined. More text follows."),"The evidence was examined.")

    def batch_repo(self, rows):
        repo=Repository()
        valid={p.published_video_id:p for p in rows if hasattr(p,"published_video_id")}
        repo.get_published_video=lambda identifier:valid.get(identifier)
        repo.list_published_videos=lambda limit,offset:rows[offset:offset+limit]
        return repo

    def test_malformed_first_and_middle_rows_isolated(self):
        first=Repository().publication
        second=first.model_copy(update={"published_video_id":"pub-2"})
        for rows in ([None,first,second],[first,None,second]):
            report=extract_publications(self.batch_repo(rows),page_size=1,clock=lambda:NOW)
            self.assertEqual(report["totals"],{"collected":2,"skipped":0,"failed":1})
            self.assertTrue(report["complete"])
            self.assertFalse(report["success"])
            self.assertEqual(len(report["results"]),3)

    def test_page_failure_preserves_partial_results_and_reports_failure(self):
        repo=Repository()
        def page(limit,offset):
            if offset:raise RuntimeError("secret-page-password")
            return [repo.publication]
        repo.list_published_videos=page
        report=extract_publications(repo,page_size=1,clock=lambda:NOW)
        self.assertFalse(report["complete"])
        self.assertFalse(report["success"])
        self.assertEqual(report["totals"],{"collected":1,"skipped":0,"failed":1})
        self.assertNotIn("secret-page-password",str(report))
        self.assertEqual(repo.features,{})

    def test_repeatable_read_inventory_stable_under_delete_and_reorder(self):
        from psycopg.pq import TransactionStatus
        first=Repository().publication
        second=first.model_copy(update={"published_video_id":"pub-2"})
        third=first.model_copy(update={"published_video_id":"pub-3"})
        for mutate in (lambda rows:rows.pop(0),lambda rows:rows.reverse()):
            live=[first,second,third]
            repo=self.batch_repo(live)
            repo._connection=Mock()
            repo._connection.info.transaction_status=TransactionStatus.IDLE
            snapshot=[]
            def begin(statement):
                self.assertEqual(statement,"SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                snapshot.extend(live)
            repo._connection.execute.side_effect=begin
            def page(limit,offset):
                if offset==1:mutate(live)
                return snapshot[offset:offset+limit]
            repo.list_published_videos=page
            report=extract_publications(repo,page_size=1,clock=lambda:NOW)
            self.assertEqual(report["totals"]["collected"],3)
            self.assertEqual({r["published_video_id"] for r in report["results"]},{"pub-1","pub-2","pub-3"})
            repo._connection.execute.assert_called_once()

    def test_duplicate_inventory_does_not_process_twice(self):
        publication=Repository().publication
        report=extract_publications(self.batch_repo([publication,publication]),clock=lambda:NOW)
        self.assertEqual(report["totals"],{"collected":1,"skipped":1,"failed":0})

    def test_existing_transaction_fails_closed_without_enumeration(self):
        from psycopg.pq import TransactionStatus
        repo=Repository()
        repo._connection=Mock()
        repo._connection.info.transaction_status=TransactionStatus.INTRANS
        repo.list_published_videos=Mock()
        report=extract_publications(repo)
        self.assertFalse(report["complete"])
        self.assertEqual(report["totals"]["failed"],1)
        repo.list_published_videos.assert_not_called()

    def test_no_network_and_no_production_imports(self):
        with patch("socket.socket.connect",side_effect=AssertionError("network forbidden")):
            self.extract()


if __name__=="__main__":unittest.main()
