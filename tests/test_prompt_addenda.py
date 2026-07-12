import unittest

from utils.nim_client import build_system_prompt


BASE_PERSONALITY = {
    "name": "Lucy",
    "age": "21",
    "role": "server admin assistant & friend to everyone here",
    "traits": "warm, competent",
    "backstory": "grew up online",
    "speaking_style": "casual",
    "boundaries": "respectful",
}


class GenderConsistencyAddendumTests(unittest.TestCase):
    """Regression coverage for the Hinglish gender-consistency fix — Lucy
    was slipping into masculine Hindi verb conjugation (e.g. 'kar raha
    hoon') despite being female. The addendum should be present whenever
    pronouns are female, and absent if a deployment ever reconfigures her
    to something else."""

    def test_feminine_pronouns_include_hindi_gender_addendum(self):
        personality = {**BASE_PERSONALITY, "pronouns": "she/her"}
        prompt = build_system_prompt(personality, "NERV-HQ", "Nox")
        self.assertIn("kar rahi hoon", prompt)
        self.assertIn("feminine conjugation", prompt)

    def test_non_female_pronouns_skip_hindi_gender_addendum(self):
        personality = {**BASE_PERSONALITY, "pronouns": "he/him"}
        prompt = build_system_prompt(personality, "NERV-HQ", "Nox")
        self.assertNotIn("kar rahi hoon", prompt)

    def test_missing_pronouns_default_to_female_and_include_addendum(self):
        personality = {**BASE_PERSONALITY}  # no "pronouns" key at all
        prompt = build_system_prompt(personality, "NERV-HQ", "Nox")
        self.assertIn("kar rahi hoon", prompt)


class ToolActionHonestyAddendumTests(unittest.TestCase):
    """Regression coverage for the 'stop claiming actions happened without
    calling the tool' fix. Present regardless of can_use_tools since
    flag_for_owner (always available) is covered by the same rule."""

    def test_addendum_present_when_tools_enabled(self):
        personality = {**BASE_PERSONALITY, "pronouns": "she/her"}
        prompt = build_system_prompt(personality, "NERV-HQ", "Nox", can_use_tools=True)
        self.assertIn("you haven't done it, full stop", prompt)
        self.assertIn("call the tool once per target", prompt)

    def test_addendum_present_even_when_tools_disabled(self):
        personality = {**BASE_PERSONALITY, "pronouns": "she/her"}
        prompt = build_system_prompt(personality, "NERV-HQ", "Nox", can_use_tools=False)
        self.assertIn("you haven't done it, full stop", prompt)


class NoToolsAvailableAddendumTests(unittest.TestCase):
    """Regression coverage for the permission-gap fix: when can_use_tools is
    False, the prompt now says so outright instead of leaving it unstated —
    the gap that let Lucy improvise a fake 'done' for a genuine admin whose
    permissions were misread (ADMINISTRATOR-only role) rather than telling
    them plainly she couldn't do it."""

    def test_disabled_tools_state_limitation_plainly(self):
        personality = {**BASE_PERSONALITY, "pronouns": "she/her"}
        prompt = build_system_prompt(personality, "NERV-HQ", "Nox", can_use_tools=False)
        self.assertIn("do NOT have tools available", prompt)
        self.assertIn("never describe it as done", prompt)

    def test_enabled_tools_does_not_include_the_disabled_wording(self):
        personality = {**BASE_PERSONALITY, "pronouns": "she/her"}
        prompt = build_system_prompt(personality, "NERV-HQ", "Nox", can_use_tools=True)
        self.assertNotIn("do NOT have tools available", prompt)


if __name__ == "__main__":
    unittest.main()