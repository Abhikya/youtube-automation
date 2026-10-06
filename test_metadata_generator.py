"""Offline regression tests for the existing metadata generation boundary."""
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

with patch('openai.OpenAI'):
    metadata_module = importlib.import_module('engine.generators.metadata_generator')


class MetadataTests(unittest.TestCase):
    def generate(self, tags=None):
        text = 'TITLE: A signal remains unexplained\nDESCRIPTION: Known facts and uncertainty.'
        if tags is not None:
            text += '\nHASHTAGS: ' + tags
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
        with patch.object(metadata_module, 'client', client):
            result = metadata_module.generate_metadata('A radio signal of unknown origin.')
        return result, client

    def test_shorts_always_present(self):
        result, _ = self.generate('#Space #Astronomy #Science')
        self.assertEqual(result['Hashtags'], ['#shorts', '#space', '#astronomy', '#science'])

    def test_two_to_four_relevant_additions_when_available(self):
        for tags in ('#space #astronomy', '#space #astronomy #science #radio'):
            result, _ = self.generate(tags)
            self.assertGreaterEqual(len(result['Hashtags']) - 1, 2)
            self.assertLessEqual(len(result['Hashtags']) - 1, 4)

    def test_case_insensitive_deduplication(self):
        result, _ = self.generate('#Shorts #SHORTS #Space #space #Astronomy #ASTRONOMY')
        self.assertEqual(result['Hashtags'], ['#shorts', '#space', '#astronomy'])

    def test_cap_and_spam_filter(self):
        result, _ = self.generate('#viral #FYP #trending #space #astronomy #science #radio #signal')
        self.assertEqual(result['Hashtags'], ['#shorts', '#space', '#astronomy', '#science', '#radio'])

    def test_existing_fields_and_api_behavior_unchanged(self):
        result, client = self.generate('#space #astronomy')
        self.assertEqual(result['Title'], 'A signal remains unexplained')
        self.assertEqual(result['Description'], 'Known facts and uncertainty.')
        self.assertEqual(set(result), {'Title', 'Description', 'Hashtags'})
        client.chat.completions.create.assert_called_once()
        self.assertEqual(client.chat.completions.create.call_args.kwargs['model'], 'gpt-4o-mini')
        prompt = client.chat.completions.create.call_args.kwargs['messages'][0]['content']
        self.assertIn('A radio signal of unknown origin.', prompt)
        self.assertIn('2-4 additional hashtags strongly relevant', prompt)

    def test_missing_or_unusable_tags_do_not_invent_topics(self):
        for tags in (None, '', '#viral #fyp #trending', '#bad! plain-text'):
            result, _ = self.generate(tags)
            self.assertEqual(result['Hashtags'], ['#shorts'])

    def test_existing_shorts_with_one_known_tag_preserved(self):
        result, _ = self.generate('#SHORTS #space')
        self.assertEqual(result['Hashtags'], ['#shorts', '#space'])


if __name__ == '__main__':
    unittest.main()
