import unittest

from utils import persona_engine as pe


class ApplySignalTests(unittest.TestCase):
    def test_defaults_used_when_value_and_confidence_missing(self):
        value, confidence = pe.apply_signal(None, None, delta=10, weight=1.0, cap_gain=45.0)
        self.assertEqual(value, 60.0)
        self.assertGreater(confidence, 0.0)

    def test_value_clamped_to_0_100(self):
        value, _ = pe.apply_signal(95.0, 50.0, delta=30, weight=1.0, cap_gain=45.0)
        self.assertEqual(value, 100.0)
        value, _ = pe.apply_signal(5.0, 50.0, delta=-30, weight=1.0, cap_gain=45.0)
        self.assertEqual(value, 0.0)

    def test_confidence_gain_diminishes_as_confidence_rises(self):
        _, low_conf_gain = pe.apply_signal(50.0, 0.0, delta=5, weight=1.0, cap_gain=45.0)
        _, high_conf_gain = pe.apply_signal(50.0, 90.0, delta=5, weight=1.0, cap_gain=45.0)
        gain_from_low = low_conf_gain - 0.0
        gain_from_high = high_conf_gain - 90.0
        self.assertGreater(gain_from_low, gain_from_high)

    def test_confidence_clamped_to_100(self):
        _, confidence = pe.apply_signal(50.0, 99.0, delta=5, weight=1.0, cap_gain=45.0)
        self.assertLessEqual(confidence, 100.0)


class ApplyDeltasTiersTests(unittest.TestCase):
    def test_explicit_moves_value_more_than_inferred_or_heuristic(self):
        profile, confidence = {}, {}
        explicit_profile, explicit_conf = pe.apply_explicit_deltas(profile, confidence, {"directness": 35})
        inferred_profile, inferred_conf = pe.apply_inferred_deltas(profile, confidence, {"directness": 35})
        heuristic_profile, heuristic_conf = pe.apply_heuristic_deltas(profile, confidence, {"directness": 35})

        self.assertGreater(explicit_profile["directness"], inferred_profile["directness"])
        self.assertGreater(inferred_profile["directness"], heuristic_profile["directness"])
        self.assertGreater(explicit_conf["directness"], inferred_conf["directness"])
        self.assertGreater(inferred_conf["directness"], heuristic_conf["directness"])

    def test_unknown_axis_is_ignored(self):
        profile, confidence = pe.apply_explicit_deltas({}, {}, {"not_a_real_axis": 50})
        self.assertNotIn("not_a_real_axis", profile)
        self.assertEqual(profile, {})

    def test_zero_delta_is_a_noop(self):
        profile, confidence = pe.apply_explicit_deltas({"energy": 50.0}, {"energy": 10.0}, {"energy": 0})
        self.assertEqual(profile["energy"], 50.0)
        self.assertEqual(confidence["energy"], 10.0)

    def test_only_touches_axes_present_in_deltas(self):
        profile, confidence = pe.apply_explicit_deltas({}, {}, {"banter": 20})
        self.assertIn("banter", profile)
        self.assertNotIn("directness", profile)


class LoadProfileRowTests(unittest.TestCase):
    def test_parses_valid_json(self):
        row = {"style_profile": '{"directness": 70.5, "banter": 30}', "style_confidence": '{"directness": 50}'}
        profile, confidence = pe.load_profile_row(row)
        self.assertEqual(profile["directness"], 70.5)
        self.assertEqual(profile["banter"], 30.0)
        self.assertEqual(confidence["directness"], 50.0)

    def test_missing_columns_degrade_to_empty_dicts(self):
        profile, confidence = pe.load_profile_row({})
        self.assertEqual(profile, {})
        self.assertEqual(confidence, {})

    def test_malformed_json_degrades_to_empty_dict(self):
        profile, confidence = pe.load_profile_row({"style_profile": "not json", "style_confidence": "{}"})
        self.assertEqual(profile, {})

    def test_unknown_axis_keys_are_dropped(self):
        row = {"style_profile": '{"directness": 60, "made_up_axis": 99}', "style_confidence": "{}"}
        profile, _ = pe.load_profile_row(row)
        self.assertIn("directness", profile)
        self.assertNotIn("made_up_axis", profile)

    def test_none_row_does_not_raise(self):
        profile, confidence = pe.load_profile_row(None)
        self.assertEqual(profile, {})
        self.assertEqual(confidence, {})


class HeuristicSignalTests(unittest.TestCase):
    def test_empty_messages_returns_empty_dict(self):
        self.assertEqual(pe.heuristic_signal([]), {})
        self.assertEqual(pe.heuristic_signal(None), {})

    def test_exclamation_heavy_text_nudges_energy_up(self):
        deltas = pe.heuristic_signal(["this is so cool!!!", "amazing!!! love it!!!"])
        self.assertIn("energy", deltas)
        self.assertGreater(deltas["energy"], 0)

    def test_slang_nudges_banter_up(self):
        deltas = pe.heuristic_signal(["lol that's wild", "lmao no way bruh"])
        self.assertIn("banter", deltas)
        self.assertGreater(deltas["banter"], 0)

    def test_long_messages_nudge_depth_up(self):
        long_msg = "so I've been thinking about this a lot lately and honestly there's a lot going on " * 2
        deltas = pe.heuristic_signal([long_msg, long_msg])
        self.assertIn("depth", deltas)
        self.assertGreater(deltas["depth"], 0)

    def test_short_messages_nudge_depth_down(self):
        deltas = pe.heuristic_signal(["lol", "ok", "sure"])
        self.assertIn("depth", deltas)
        self.assertLess(deltas["depth"], 0)

    def test_deltas_stay_within_clamp_bounds(self):
        spammy = ["!!!!!!!!! LOL LMAO WOW AMAZING !!!!!!!!"] * 10
        deltas = pe.heuristic_signal(spammy)
        for value in deltas.values():
            self.assertLessEqual(abs(value), 6.0)


class ParseInferredDeltasTests(unittest.TestCase):
    def test_parses_plain_json(self):
        raw = '{"directness": 5, "banter": -3, "energy": 0, "depth": 0, "support_style": 0}'
        deltas = pe.parse_inferred_deltas(raw)
        self.assertEqual(deltas["directness"], 5.0)
        self.assertEqual(deltas["banter"], -3.0)

    def test_zero_values_are_dropped(self):
        raw = '{"directness": 0, "banter": 4}'
        deltas = pe.parse_inferred_deltas(raw)
        self.assertNotIn("directness", deltas)
        self.assertIn("banter", deltas)

    def test_strips_markdown_code_fences(self):
        raw = '```json\n{"directness": 5}\n```'
        deltas = pe.parse_inferred_deltas(raw)
        self.assertEqual(deltas["directness"], 5.0)

    def test_invalid_json_returns_none(self):
        self.assertIsNone(pe.parse_inferred_deltas("not json at all"))

    def test_empty_or_none_input_returns_none(self):
        self.assertIsNone(pe.parse_inferred_deltas(""))
        self.assertIsNone(pe.parse_inferred_deltas(None))

    def test_non_dict_json_returns_none(self):
        self.assertIsNone(pe.parse_inferred_deltas("[1, 2, 3]"))

    def test_values_clamped_to_ten(self):
        raw = '{"directness": 500, "banter": -500}'
        deltas = pe.parse_inferred_deltas(raw)
        self.assertEqual(deltas["directness"], 10.0)
        self.assertEqual(deltas["banter"], -10.0)

    def test_unknown_keys_ignored_all_zero_or_unknown_returns_none(self):
        raw = '{"not_a_real_axis": 8, "directness": 0}'
        self.assertIsNone(pe.parse_inferred_deltas(raw))


class RenderAdaptationLayerTests(unittest.TestCase):
    def test_returns_none_with_no_confidence(self):
        self.assertIsNone(pe.render_adaptation_layer({"directness": 90}, {"directness": 0}))

    def test_returns_none_with_empty_inputs(self):
        self.assertIsNone(pe.render_adaptation_layer({}, {}))
        self.assertIsNone(pe.render_adaptation_layer(None, None))

    def test_includes_high_directness_phrasing_once_confident(self):
        note = pe.render_adaptation_layer({"directness": 80}, {"directness": 90})
        self.assertIsNotNone(note)
        self.assertIn("blunt", note.lower())

    def test_includes_low_banter_phrasing_once_confident(self):
        note = pe.render_adaptation_layer({"banter": 10}, {"banter": 90})
        self.assertIn("sincere", note.lower())

    def test_mid_range_value_with_confidence_produces_no_phrase_for_that_axis(self):
        # 50 is neutral — shouldn't trigger either the high or low phrasing.
        note = pe.render_adaptation_layer({"directness": 50}, {"directness": 90})
        self.assertIsNone(note)

    def test_never_reveals_mechanics_of_adaptation(self):
        note = pe.render_adaptation_layer({"directness": 80}, {"directness": 90})
        self.assertNotIn("axis", note.lower())
        self.assertNotIn("confidence", note.lower())


class DescribeStyleForUserTests(unittest.TestCase):
    def test_default_message_when_no_signal(self):
        summary = pe.describe_style_for_user({}, {})
        self.assertIn("vibecheck", summary.lower())

    def test_summary_reflects_confident_axis(self):
        summary = pe.describe_style_for_user({"energy": 85}, {"energy": 90})
        self.assertIn("high energy", summary.lower())


class VibecheckQuestionsTests(unittest.TestCase):
    def test_every_question_has_two_options_with_valid_axis_deltas(self):
        for question in pe.VIBECHECK_QUESTIONS:
            self.assertEqual(len(question["options"]), 2)
            for option in question["options"]:
                self.assertTrue(option["label"])
                for axis in option["delta"]:
                    self.assertIn(axis, pe.AXES)

    def test_options_within_a_question_pull_opposite_directions(self):
        for question in pe.VIBECHECK_QUESTIONS:
            deltas = [sum(opt["delta"].values()) for opt in question["options"]]
            self.assertLess(deltas[0] * deltas[1], 0)


if __name__ == "__main__":
    unittest.main()