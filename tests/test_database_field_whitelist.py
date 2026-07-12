import unittest

from utils import database as db


class GuildSettingFieldWhitelistTests(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for hardening update_guild_setting against an
    unvalidated column name landing in its f-string SET clause. No call
    site in this codebase passes an untrusted field name today — every
    kwarg is a literal written in source (cogs/utility.py, cogs/github.py)
    — but there was no guard at all before this, unlike the sibling
    set_personality_field()/VALID_PERSONALITY_FIELDS pattern a little
    further down the same file. The whitelist check runs before any pool
    access, so it's testable without a live database connection."""

    async def test_unknown_field_raises_before_touching_the_db(self):
        with self.assertRaises(ValueError):
            await db.update_guild_setting(123, definitely_not_a_real_column="x")

    async def test_no_kwargs_is_a_silent_no_op(self):
        # Returns immediately (no pool access) rather than raising.
        result = await db.update_guild_setting(123)
        self.assertIsNone(result)

    def test_whitelist_covers_every_real_call_site_in_the_codebase(self):
        # If a future call site starts using a field not listed here, this
        # is the check that should catch the mismatch in review.
        known_call_site_fields = {
            "welcome_channel_id", "welcome_message", "log_channel_id",
            "chat_trigger_mode", "chat_channel_id", "channel_redirection_enabled",
            "idle_chatter_enabled", "server_vibe_enabled", "vent_channel_id",
            "github_digest_channel_id", "github_last_digest_at",
        }
        self.assertTrue(known_call_site_fields.issubset(db.VALID_GUILD_SETTING_FIELDS))


if __name__ == "__main__":
    unittest.main()