"""Offline prompt contracts, not a claim that model editorial choices are deterministic."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config.channels import resolve_channel_config
from config.content_policy import GLOBAL_GUARDRAIL_INSTRUCTIONS


class HiddenHistoryPromptTests(unittest.TestCase):
    def test_editorial_preferences_and_reselection_guidance(self):
        text = resolve_channel_config('hidden_history').content_instructions
        for guidance in (
            'forgotten disasters', 'bizarre documented historical incidents',
            'documented espionage operations', 'lost or abandoned places',
            'documented historical disappearances',
            'strange inventions and experiments with consequences',
            'generic historical overviews', 'broad biographies',
            'textbook-style war summaries', 'vague historical trivia',
            'Reject unsupported folklore framed as fact',
            'Internally discard and replace weak candidates',
            'at least three', 'EVENT + SURPRISE / CONSEQUENCE',
            'Do not manufacture mystery', 'Keep titles and descriptions faithful',
        ):
            with self.subTest(guidance=guidance):
                self.assertIn(guidance, text)

    def test_channel_instructions_reach_all_three_prompts_without_leaking(self):
        from engine.generators import idea_generator, script_generator, metadata_generator
        hidden = resolve_channel_config('hidden_history').content_instructions
        for key in ('hidden_history', 'minute_mysteries', 'space_science'):
            channel = resolve_channel_config(key)
            for stage, module, output in (
                ('ideas', idea_generator, 'A documented shipyard accident'),
                ('script', script_generator, 'The shipyard accident changed safety rules.'),
                ('metadata', metadata_generator, 'TITLE: The shipyard accident\nDESCRIPTION: Safety rules changed.\nHASHTAGS: #shorts #history #shipyard'),
            ):
                with self.subTest(channel=key, stage=stage):
                    client = Mock()
                    client.chat.completions.create.return_value = SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content=output))])
                    with patch.object(module, 'client', client), \
                         patch.object(idea_generator, 'load_memory', return_value={}), \
                         patch.object(idea_generator, 'save_memory'):
                        if stage == 'ideas':
                            result = module.generate_video_ideas(channel.topic_theme, memory_file='unused-memory.json', channel_config=channel)
                            self.assertEqual(result, [output])
                        elif stage == 'script':
                            self.assertEqual(module.generate_script('A shipyard accident', channel_config=channel), output)
                        else:
                            self.assertEqual(module.generate_metadata('The shipyard accident changed safety rules.', channel_config=channel)['Title'], 'The shipyard accident')
                    client.chat.completions.create.assert_called_once()
                    prompt = client.chat.completions.create.call_args.kwargs['messages'][0]['content']
                    self.assertIn(channel.content_instructions, prompt)
                    self.assertIn(GLOBAL_GUARDRAIL_INSTRUCTIONS, prompt)
                    self.assertEqual(hidden in prompt, key == 'hidden_history')
                    if stage == 'ideas':
                        self.assertIn('under 12 words', prompt)
                    elif stage == 'script':
                        self.assertIn('Around 80 words', prompt)
                    else:
                        self.assertIn('Return ONLY in this exact format', prompt)


if __name__ == '__main__':
    unittest.main()
