"""Offline story identities, generator integration and channel isolation."""
import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from config.channels import CHANNEL_CONFIGS, RuntimePaths
from config.content_policy import ContentGuardrailError
from engine.generators.story_identity import canonical_story_key, cross_channel_overlaps, main


class StoryIdentityTests(unittest.TestCase):
    def test_event_paraphrases(self):
        for key, titles in (
            ('flight_19', ['The Mystery of Flight 19', 'What Really Happened to Flight 19?',
                           'Why Flight 19 Never Returned', '1. FLIGHT 19!', 'Flight 19 in 1945']),
            ('uss_cyclops', ['USS Cyclops disappearance', 'Why the USS Cyclops Vanished', 'USS Cyclops']),
        ):
            for title in titles:
                with self.subTest(title=title):
                    self.assertEqual(canonical_story_key(title), key)

    def test_distinct_angles_events_dates_and_places(self):
        pairs = [
            ('How Black Holes Form', 'What Happens Inside a Black Hole?'),
            ('USS Cyclops', 'SS Waratah'), ('Wow! Signal', 'Lorimer burst'),
            ("Tabby's Star", 'HD 139139'), ('London flood 1928', 'London flood 1953'),
            ('Springfield Armory blaze', 'Springfield Three disappearance'),
            ('USS Cyclops disappearance', 'USS Cyclops construction'),
            ('Flight 19', 'Flight 19 rescue operation'), ('Flight 19 1945', 'Flight 19 2020'),
            ('Flight 19', 'Flight 190'), ('Mary Celeste', 'Mary Celeste cargo economics'),
            ('The Moon formation', 'The Moon exploration'),
        ]
        for a, b in pairs:
            with self.subTest(a=a, b=b):
                self.assertNotEqual(canonical_story_key(a), canonical_story_key(b))

    def test_normalized_unknown_titles(self):
        self.assertEqual(canonical_story_key('1. "A Forgotten Disaster!"'),
                         canonical_story_key('a forgotten disaster'))
        self.assertEqual(canonical_story_key('Tabby’s Star'), canonical_story_key("Tabby's Star"))
        self.assertIsNone(canonical_story_key('---'))

    def test_report_is_nonmutating_and_not_publication_evidence(self):
        memories = {'minute_mysteries': {'old': ['Flight 19', 'Why Flight 19 Never Returned']},
                    'hidden_history': {'history': ['The Mystery of Flight 19']},
                    'space_science': {'space': ['Lorimer burst']}}
        before = copy.deepcopy(memories)
        rows = cross_channel_overlaps(memories)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['canonical_story_key'], 'flight_19')
        self.assertEqual(set(rows[0]['channels']), {'minute_mysteries', 'hidden_history'})
        self.assertEqual(rows[0]['published_status'], 'unknown')
        self.assertEqual(memories, before)

    def test_cli_reads_explicit_files_without_modification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'memory.json'
            path.write_text(json.dumps({'topic': ['Flight 19']}), encoding='utf-8')
            before = path.read_bytes()
            output = io.StringIO()
            with redirect_stdout(output):
                main(['--channel-memory', 'a=' + str(path), '--channel-memory', 'b=' + str(path)])
            self.assertEqual(len(json.loads(output.getvalue())['overlaps']), 1)
            self.assertEqual(path.read_bytes(), before)


class IdeaGeneratorDuplicateTests(unittest.TestCase):
    def generate(self, text, memory):
        from engine.generators import idea_generator as generator
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
        with patch.object(generator, 'client', client), \
             patch.object(generator, 'load_memory', return_value=memory), \
             patch.object(generator, 'save_memory') as save:
            result = generator.generate_video_ideas()
        return result, save, client

    def test_legacy_history_across_topic_keys_and_candidate_batch(self):
        memory = {'old topic': ['The Mystery of Flight 19']}
        result, save, client = self.generate(
            'Why Flight 19 Never Returned\nUSS Cyclops disappearance\nWhy the USS Cyclops Vanished\nSS Waratah', memory)
        self.assertEqual(result, ['USS Cyclops disappearance', 'SS Waratah'])
        self.assertEqual(memory['old topic'], ['The Mystery of Flight 19'])
        self.assertEqual(memory['unsolved mysteries'], result)
        save.assert_called_once()
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_all_duplicates_use_existing_failure_without_write_or_internal_retry(self):
        from engine.generators import idea_generator as generator
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='Why Flight 19 Never Returned'))])
        memory = {'unsolved mysteries': ['Flight 19']}
        before = copy.deepcopy(memory)
        with patch.object(generator, 'client', client), patch.object(generator, 'load_memory', return_value=memory), \
             patch.object(generator, 'save_memory') as save:
            with self.assertRaises(ContentGuardrailError):
                generator.generate_video_ideas()
        self.assertEqual(memory, before)
        save.assert_not_called()
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_guardrails_remain_before_memory_write(self):
        result, save, _ = self.generate('A religious miracle\nFlight 19\nWhy Flight 19 Never Returned', {})
        self.assertEqual(result, ['Flight 19'])
        self.assertEqual(save.call_args.args[0], {'unsolved mysteries': ['Flight 19']})

    def test_preserves_all_proposals_and_three_result_limit(self):
        result, save, _ = self.generate('Flight 19\nUSS Cyclops\nSS Waratah\nWow! Signal\nLorimer burst', {})
        self.assertEqual(len(result), 3)
        self.assertEqual(len(save.call_args.args[0]['unsolved mysteries']), 5)

    def test_three_channel_files_are_independent_and_legacy_roundtrip(self):
        from engine.generators import idea_generator as generator
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='Flight 19'))])
        with tempfile.TemporaryDirectory() as directory, patch.object(generator, 'client', client):
            for channel in CHANNEL_CONFIGS:
                paths = RuntimePaths(channel, Path(directory))
                paths.memory.parent.mkdir(parents=True, exist_ok=True)
                self.assertEqual(generator.generate_video_ideas(channel.topic_theme,
                    memory_file=paths.memory, channel_config=channel), ['Flight 19'])
                self.assertEqual(json.loads(paths.memory.read_text()), {channel.topic_theme: ['Flight 19']})
                before = paths.memory.read_bytes()
                with self.assertRaises(ContentGuardrailError):
                    generator.generate_video_ideas(channel.topic_theme, memory_file=paths.memory, channel_config=channel)
                self.assertEqual(paths.memory.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
