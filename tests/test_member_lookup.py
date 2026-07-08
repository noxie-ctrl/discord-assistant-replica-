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


if __name__ == "__main__":
    unittest.main()