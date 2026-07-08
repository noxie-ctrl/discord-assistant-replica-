import unittest

from cogs.ai_chat import resolve_idle_chatter_channel_ids


class IdleChatterChannelResolutionTests(unittest.TestCase):
    """resolve_idle_chatter_channel_ids is the pure logic behind the
    idle-chatter multi-channel fix — pulled out so it's directly testable
    without mocking discord.Guild/TextChannel, same pattern as
    maybe_suggest_channel_redirection in tests/test_ai_chat_helpers.py."""

    def test_explicit_channels_win_over_legacy_fallback(self):
        # A guild that's used /addidlechatterchannel should use exactly
        # those channels — the old chat_channel_id default shouldn't sneak
        # back in alongside them.
        result = resolve_idle_chatter_channel_ids([111, 222], fallback_channel_id=999)
        self.assertEqual(result, [111, 222])

    def test_falls_back_to_legacy_single_channel_when_unconfigured(self):
        # Regression-guard: a guild that never touches the new commands
        # shouldn't lose idle chatter entirely on upgrade.
        result = resolve_idle_chatter_channel_ids([], fallback_channel_id=999)
        self.assertEqual(result, [999])

    def test_returns_empty_when_nothing_configured_at_all(self):
        result = resolve_idle_chatter_channel_ids([], fallback_channel_id=None)
        self.assertEqual(result, [])

    def test_preserves_configured_order(self):
        result = resolve_idle_chatter_channel_ids([333, 111, 222], fallback_channel_id=999)
        self.assertEqual(result, [333, 111, 222])


if __name__ == "__main__":
    unittest.main()