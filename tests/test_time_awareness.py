import unittest
from datetime import datetime, timezone
from unittest import mock

from utils import nim_client


class TimeAwarenessTests(unittest.TestCase):
    """Regression coverage for the AM/PM misread bug: build_system_prompt()
    used to hand the model a bare 24-hour string like "11:48" with no AM/PM
    cue, and it would sometimes misread that as PM (confidently saying
    "it's 11:50 PM" at 11:48 AM). The fix states the 12-hour clock, the
    24-hour clock, and a plain-English day-part label, all pointing the
    same direction — this locks that in so it can't quietly regress."""

    def _build_prompt(self, fixed_utc: datetime) -> str:
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_utc

        with mock.patch.object(nim_client, "datetime", FrozenDatetime):
            return nim_client.build_system_prompt(
                personality={}, guild_name="NERV-HQ", owner_name="Nox",
            )

    def test_late_morning_states_am_explicitly(self):
        # 11:48 IST == 06:18 UTC — the exact case caught live in #bot-commands.
        fixed_utc = datetime(2026, 7, 8, 6, 18, tzinfo=timezone.utc)
        prompt = self._build_prompt(fixed_utc)
        self.assertIn("11:48 AM IST", prompt)
        self.assertIn("morning", prompt)
        self.assertNotIn("11:48 PM", prompt)

    def test_late_night_states_pm_explicitly(self):
        # 23:50 IST == 18:20 UTC — the time Lucy actually (wrongly) said out loud.
        fixed_utc = datetime(2026, 7, 8, 18, 20, tzinfo=timezone.utc)
        prompt = self._build_prompt(fixed_utc)
        self.assertIn("11:50 PM IST", prompt)
        self.assertIn("night", prompt)
        self.assertNotIn("11:50 AM", prompt)


if __name__ == "__main__":
    unittest.main()