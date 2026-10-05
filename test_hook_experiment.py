from types import SimpleNamespace
from unittest.mock import patch
import unittest
from engine.generators import script_generator as sg


def response(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class HookExperimentTests(unittest.TestCase):
    def test_baseline_unchanged_prompt_and_call(self):
        with patch.object(sg.client.chat.completions, "create", return_value=response("Original.")) as call:
            assert sg.generate_script("idea") == "Original."
            prompt = call.call_args.kwargs["messages"][0]["content"]
            assert "Around 80 words" in prompt and "Strong hook in first 3 seconds" in prompt
            assert "hook_event_first" not in prompt
            assert call.call_args.kwargs["model"] == "gpt-4.1-mini"


    def test_only_opening_changes(self):
        baseline = "In 1977, a signal appeared. Its origin remains unknown. No evidence resolves it."
        with patch.object(sg.client.chat.completions, "create", return_value=response("A signal appeared in 1977.")):
            result = sg.generate_script("signal", hook_strategy="HOOK_EVENT_FIRST_V1", baseline_script=baseline)
        assert sg.split_opening(result)[1] == sg.split_opening(baseline)[1]


    def test_reject_extra_sentences(self):
        with patch.object(sg.client.chat.completions, "create", return_value=response("A signal appeared. Subscribe now.")):
            with self.assertRaises(ValueError): sg.rewrite_opening("signal", "In 1977, a signal appeared. It remains unknown.")


    def test_invalid_mode_never_calls_api(self):
        with patch.object(sg.client.chat.completions, "create") as call:
            with self.assertRaises(ValueError): sg.generate_script("idea", hook_strategy="INVALID")
            call.assert_not_called()


    def test_metadata_and_unknown_opening(self):
        assert sg.experiment_metadata("BASELINE")["experiment_variant"] == "baseline"
        assert sg.experiment_metadata("HOOK_EVENT_FIRST_V1")["experiment_variant"] == "hook_event_first_v1"
        with self.assertRaises(ValueError): sg.split_opening("no sentence terminator")

    def test_reorder_preserves_month_names_and_quantity(self):
        old="In March 1918, the USS Cyclops vanished between Barbados and Baltimore\u2014306 crew, gone."
        new="The USS Cyclops vanished between Barbados and Baltimore in March 1918\u2014306 crew, gone."
        result=sg.validate_opening_change(old,new)
        self.assertEqual(result["flags"], [])
        self.assertTrue(result["human_review_required"])
        self.assertEqual(result["semantic_equivalence"], "NOT_AUTOMATICALLY_VERIFIED")

    def test_removed_facts_and_new_content_are_flagged(self):
        result=sg.validate_opening_change("In March 1918, the USS Cyclops vanished near Barbados with 306 crew.",
                                          "The ship certainly vanished near Baltimore with 309 crew.")
        for flag in ["removed_date_detail","changed_numeric_fact_or_quantity",
                     "removed_named_entity_or_location_candidate","changed_certainty_language",
                     "introduced_content_requires_fact_review"]:
            self.assertIn(flag,result["flags"])

    def test_emotional_change_and_repeated_quantity(self):
        result=sg.validate_opening_change("A strange signal lasted 72 seconds.","A terrifying signal lasted 72 seconds for 72 seconds.")
        self.assertIn("changed_emotional_framing",result["flags"])
        self.assertIn("changed_numeric_fact_or_quantity",result["flags"])

    def test_generation_blocks_fact_removal(self):
        with patch.object(sg.client.chat.completions,"create",return_value=response("A signal appeared.")):
            with self.assertRaises(ValueError):sg.rewrite_opening("signal","In 1977, a signal appeared. Its origin remains unknown.")

    def test_validation_is_deterministic(self):
        old="In 1977, a signal appeared.";new="A signal appeared in 1977."
        self.assertEqual(sg.validate_opening_change(old,new),sg.validate_opening_change(old,new))
