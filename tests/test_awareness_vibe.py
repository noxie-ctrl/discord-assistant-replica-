import asyncio
import unittest
from unittest import mock

from utils import awareness


class ServerVibeDigestTests(unittest.TestCase):
    def setUp(self):
        # Isolate the module-level cache between tests.
        awareness._cached_vibe = {}
        awareness._cached_vibe_at = {}

    def test_returns_empty_when_groq_not_configured(self):
        # Unlike the news digest, there's no raw-message fallback here on
        # purpose — no Groq means no digest, not a leak of raw chat content.
        with mock.patch.object(awareness.groq_client, "is_configured", return_value=False):
            result = asyncio.run(
                awareness.refresh_server_vibe(123, ["hey", "lol same"], force=True)
            )
        self.assertEqual(result, "")

    def test_caches_per_guild_independently(self):
        async def fake_call_groq(*args, **kwargs):
            return "Chill and meme-heavy, lots of gaming banter."

        with mock.patch.object(awareness.groq_client, "is_configured", return_value=True), \
             mock.patch.object(awareness.groq_client, "call_groq", side_effect=fake_call_groq):
            asyncio.run(awareness.refresh_server_vibe(1, ["hi"], force=True))
            asyncio.run(awareness.refresh_server_vibe(2, ["yo"], force=True))

        self.assertEqual(
            awareness.get_cached_server_vibe(1), "Chill and meme-heavy, lots of gaming banter."
        )
        self.assertEqual(
            awareness.get_cached_server_vibe(2), "Chill and meme-heavy, lots of gaming banter."
        )
        # A guild that was never refreshed should read back empty, not crash.
        self.assertEqual(awareness.get_cached_server_vibe(999), "")

    def test_none_response_does_not_clobber_existing_cache(self):
        # Regression: a thin/mixed sample (model says NONE) or a transient
        # failure should keep whatever vibe was cached before, not blank it.
        awareness._cached_vibe[42] = "existing vibe"
        awareness._cached_vibe_at[42] = 0.0

        async def fake_call_groq(*args, **kwargs):
            return "NONE"

        with mock.patch.object(awareness.groq_client, "is_configured", return_value=True), \
             mock.patch.object(awareness.groq_client, "call_groq", side_effect=fake_call_groq):
            result = asyncio.run(awareness.refresh_server_vibe(42, ["ok"], force=True))

        self.assertEqual(result, "existing vibe")

    def test_respects_refresh_interval_unless_forced(self):
        calls = {"count": 0}

        async def fake_call_groq(*args, **kwargs):
            calls["count"] += 1
            return "some vibe"

        with mock.patch.object(awareness.groq_client, "is_configured", return_value=True), \
             mock.patch.object(awareness.groq_client, "call_groq", side_effect=fake_call_groq):
            asyncio.run(awareness.refresh_server_vibe(7, ["hi"], force=True))
            asyncio.run(awareness.refresh_server_vibe(7, ["hi again"], force=False))

        self.assertEqual(calls["count"], 1)


if __name__ == "__main__":
    unittest.main()