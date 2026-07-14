import unittest

from cogs.ai_chat import format_member_lookup


class MemberLookupFormatterTests(unittest.TestCase):
    def test_bot_account_short_circuits(self):
        # Regression-guard: the bot check has to come first and win over
        # everything else, or a bot could get formatted as if it were a
        # real member with roles/notes — easy to get backwards by accident.
        result = format_member_lookup({
            "display_name": "MEE6",
            "username": "mee6",
            "is_bot": True,
            "joined": "January 01, 2024",
            "created": "January 01, 2020",
            "roles": "Bots",
            "notes": "should never appear",
        })
        self.assertIn("bot account", result)
        self.assertNotIn("should never appear", result)

    def test_normal_member_includes_roles_and_join_date(self):
        result = format_member_lookup({
            "display_name": "Nox",
            "username": "nox",
            "is_bot": False,
            "joined": "March 03, 2023",
            "created": "June 06, 2019",
            "roles": "Owner, Admin",
        })
        self.assertIn("Nox", result)
        self.assertIn("March 03, 2023", result)
        self.assertIn("Owner, Admin", result)

    def test_notes_appended_when_present(self):
        result = format_member_lookup({
            "display_name": "Sam",
            "is_bot": False,
            "roles": "none",
            "notes": "Plays a lot of Valorant, prefers Hinglish replies.",
        })
        self.assertIn("Known notes: Plays a lot of Valorant", result)

    def test_notes_omitted_when_absent(self):
        result = format_member_lookup({
            "display_name": "Sam",
            "is_bot": False,
            "roles": "none",
        })
        self.assertNotIn("Known notes", result)

    def test_missing_optional_fields_fall_back_to_unknown(self):
        # A member with no joined_at (shouldn't really happen, but joined_at
        # can legitimately be None for some cache states) shouldn't crash
        # the formatter or produce a blank field.
        result = format_member_lookup({"display_name": "Ghost", "is_bot": False})
        self.assertIn("unknown", result)
        self.assertIn("none", result)

    def test_omits_status_when_not_provided(self):
        # Max Awareness Phase 2: no status key at all (member offline,
        # invisible, or Discord just hasn't reported yet) — formatter must
        # say nothing about it, not "unknown". This replaces the old
        # test_never_mentions_status_or_activity, which was a Phase 1
        # regression-guard against status being invented from nothing; now
        # that lookup_member can legitimately carry real status/activity,
        # the guard is scoped to "omitted key means silence", not "the word
        # can never appear at all".
        result = format_member_lookup({
            "display_name": "Ghost",
            "is_bot": False,
            "joined": "2024",
            "created": "2018",
            "roles": "none",
        })
        lowered = result.lower()
        for banned in ("status", "activity", "online", "offline", "right now"):
            self.assertNotIn(banned, lowered)

    def test_includes_status_and_activity_when_provided(self):
        result = format_member_lookup({
            "display_name": "Nox",
            "username": "nox",
            "is_bot": False,
            "joined": "January 2024",
            "created": "2018",
            "roles": "Owner",
            "status": "online",
            "activity": "playing Balatro",
        })
        self.assertIn("online", result.lower())
        self.assertIn("playing Balatro", result)

    def test_includes_status_without_activity(self):
        # Presence can report a status with no activity attached (just
        # sitting idle with nothing running) — shouldn't produce a dangling
        # comma or an empty activity clause.
        result = format_member_lookup({
            "display_name": "Nox",
            "is_bot": False,
            "joined": "2024",
            "created": "2018",
            "roles": "none",
            "status": "idle",
        })
        self.assertIn("idle", result.lower())
        self.assertNotIn(",.", result)

    def test_bot_short_circuit_still_wins_over_status(self):
        # Bot check must still short-circuit first even if status somehow
        # ended up in the dict for a bot (shouldn't happen given the
        # `if not target.bot` guard in _execute_tool_call, but the
        # formatter itself should stay defensive regardless of caller).
        result = format_member_lookup({
            "display_name": "MEE6",
            "is_bot": True,
            "status": "online",
        })
        self.assertIn("bot account", result)
        self.assertNotIn("online", result.lower())


if __name__ == "__main__":
    unittest.main()