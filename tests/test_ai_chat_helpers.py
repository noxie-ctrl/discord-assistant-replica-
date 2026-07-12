import unittest
from types import SimpleNamespace

from cogs.ai_chat import (
    maybe_suggest_channel_redirection,
    classify_reply_depth,
    extract_image_urls_from_attachments,
    extract_image_urls_from_embeds,
    summarize_tool_results_for_fallback,
    member_has_any_permission,
    AIChat,
)


class ChannelAwarenessTests(unittest.TestCase):
    def test_detects_off_topic_vent_in_memes_channel(self):
        suggestion = maybe_suggest_channel_redirection(
            channel_topic="Memes and random fun",
            content="I feel like I am falling apart and need someone to talk to",
        )
        self.assertIsNotNone(suggestion)
        self.assertIn("vent", suggestion.lower())

    def test_ignores_on_topic_content(self):
        suggestion = maybe_suggest_channel_redirection(
            channel_topic="General chat and casual conversation",
            content="How was your day?",
        )
        self.assertIsNone(suggestion)


class ReplyDepthClassificationTests(unittest.TestCase):
    """Regression coverage for the 'stop sending paragraphs for casual chat'
    fix — classify_reply_depth() drives both max_tokens and the per-turn
    steering note in AIChat._handle_chat."""

    def test_short_casual_message_is_casual(self):
        self.assertEqual(classify_reply_depth("lol same"), "casual")

    def test_greeting_is_casual(self):
        self.assertEqual(classify_reply_depth("wassup"), "casual")

    def test_explicit_explain_request_is_deep(self):
        self.assertEqual(classify_reply_depth("then explain me abt the work"), "deep")

    def test_breakdown_request_is_deep(self):
        self.assertEqual(classify_reply_depth("can you give me a breakdown of this"), "deep")

    def test_long_message_is_deep_even_without_keywords(self):
        long_msg = "so today was a whole thing, basically everything that could go wrong did " * 2
        self.assertEqual(classify_reply_depth(long_msg), "deep")

    def test_empty_message_is_casual(self):
        self.assertEqual(classify_reply_depth(""), "casual")
        self.assertEqual(classify_reply_depth(None), "casual")


class ImageUrlExtractionTests(unittest.TestCase):
    """Regression coverage for the vision fix — these pure helpers feed
    AIChat._collect_image_urls, which now also checks the replied-to
    message and a short history fallback, not just the current message."""

    def test_extracts_image_by_content_type(self):
        att = SimpleNamespace(url="https://cdn.example.com/a.bin", content_type="image/png", filename="a.bin")
        self.assertEqual(extract_image_urls_from_attachments([att]), ["https://cdn.example.com/a.bin"])

    def test_extracts_image_by_extension_when_content_type_missing(self):
        att = SimpleNamespace(url="https://cdn.example.com/a.jpg", content_type=None, filename="a.jpg")
        self.assertEqual(extract_image_urls_from_attachments([att]), ["https://cdn.example.com/a.jpg"])

    def test_ignores_non_image_attachments(self):
        att = SimpleNamespace(url="https://cdn.example.com/a.pdf", content_type="application/pdf", filename="a.pdf")
        self.assertEqual(extract_image_urls_from_attachments([att]), [])

    def test_handles_no_attachments(self):
        self.assertEqual(extract_image_urls_from_attachments([]), [])
        self.assertEqual(extract_image_urls_from_attachments(None), [])

    def test_extracts_from_embed_image(self):
        embed = SimpleNamespace(image=SimpleNamespace(url="https://cdn.example.com/e.png"), thumbnail=None)
        self.assertEqual(extract_image_urls_from_embeds([embed]), ["https://cdn.example.com/e.png"])

    def test_extracts_from_embed_thumbnail_when_no_image(self):
        embed = SimpleNamespace(image=None, thumbnail=SimpleNamespace(url="https://cdn.example.com/t.png"))
        self.assertEqual(extract_image_urls_from_embeds([embed]), ["https://cdn.example.com/t.png"])

    def test_handles_no_embeds(self):
        self.assertEqual(extract_image_urls_from_embeds([]), [])
        self.assertEqual(extract_image_urls_from_embeds(None), [])


class ToolHonestyFallbackTests(unittest.TestCase):
    """Regression coverage for the 'stop saying Done. when nothing was
    confirmed' fix — this is what AIChat._handle_chat's tool loop falls
    back on when the model returns empty content after a round of real
    tool calls, instead of the old hardcoded 'Done.' string."""

    def test_no_tool_calls_gives_generic_apology_not_done(self):
        result = summarize_tool_results_for_fallback([])
        self.assertNotIn("Done.", result)
        self.assertIn("try that again", result.lower())

    def test_single_success_is_reflected_verbatim(self):
        result = summarize_tool_results_for_fallback(
            [("create_role", "Success: created role 'Raiders'.")]
        )
        self.assertIn("Success: created role 'Raiders'.", result)

    def test_mixed_success_and_failure_both_show_up(self):
        # The real-world case this exists for: "make a role and give it to
        # Alice, Bob, and Carol" where Bob's name doesn't resolve — the
        # fallback must not silently drop the failure and imply everyone
        # got it.
        result = summarize_tool_results_for_fallback([
            ("create_role", "Success: created role 'Raiders'."),
            ("assign_role", "Success: gave Alice the 'Raiders' role."),
            ("assign_role", "Error: no member named 'bobb' found."),
            ("assign_role", "Success: gave Carol the 'Raiders' role."),
        ])
        self.assertIn("Alice", result)
        self.assertIn("Error: no member named 'bobb' found.", result)
        self.assertIn("Carol", result)


class PermissionGapFixTests(unittest.TestCase):
    """Regression coverage for the real root cause behind 'she says done but
    nothing happened': can_use_tools (and the create_role/assign_role gates)
    used to check manage_roles/manage_guild/manage_channels directly, which
    misses members whose only grant is ADMINISTRATOR on a role — a very
    common single-admin-role server setup. ADMINISTRATOR does not imply
    those other bits are set in the raw permission bitfield, so a genuine
    admin could silently read as having none of them."""

    def test_administrator_alone_counts_as_permission(self):
        perms = SimpleNamespace(administrator=True, manage_roles=False, manage_guild=False)
        self.assertTrue(member_has_any_permission(perms, "manage_roles"))
        self.assertTrue(member_has_any_permission(perms, "manage_guild", "manage_channels"))

    def test_specific_permission_still_works_without_administrator(self):
        perms = SimpleNamespace(administrator=False, manage_roles=True, manage_guild=False)
        self.assertTrue(member_has_any_permission(perms, "manage_roles"))

    def test_neither_administrator_nor_named_permission_is_false(self):
        perms = SimpleNamespace(administrator=False, manage_roles=False, manage_guild=False)
        self.assertFalse(member_has_any_permission(perms, "manage_roles", "manage_guild"))

    def test_missing_attribute_defaults_to_false_not_a_crash(self):
        perms = SimpleNamespace(administrator=False)
        self.assertFalse(member_has_any_permission(perms, "manage_channels"))


class BoundedCacheWiringTests(unittest.TestCase):
    """Regression coverage for swapping the plain, unboundedly-growing
    tracking dicts (_recent_replies, _last_alert, _last_activity_at,
    _last_idle_chatter_at, _last_vent_check) for cachetools caches that
    clean themselves up over a long-running process, without changing how
    any call site reads/writes them."""

    def setUp(self):
        self.cog = AIChat(bot=SimpleNamespace())

    def test_recent_replies_is_bounded_lru(self):
        for i in range(600):
            self.cog._recent_replies[i] = (1, 2, 3, "snippet")
        self.assertLessEqual(len(self.cog._recent_replies), 500)
        # The most recently inserted entries should have survived eviction.
        self.assertIn(599, self.cog._recent_replies)

    def test_recent_replies_still_behaves_like_a_dict(self):
        self.cog._recent_replies[42] = (1, 2, 3, "hi")
        self.assertEqual(self.cog._recent_replies.get(42), (1, 2, 3, "hi"))
        self.assertIsNone(self.cog._recent_replies.get(999))

    def test_last_alert_and_activity_caches_support_dict_style_get(self):
        self.cog._last_alert[(1, 2)] = 100.0
        self.assertEqual(self.cog._last_alert.get((1, 2)), 100.0)
        self.assertIsNone(self.cog._last_alert.get((9, 9)))

        self.cog._last_activity_at[555] = 42.0
        self.assertEqual(self.cog._last_activity_at.get(555, 0.0), 42.0)
        self.assertEqual(self.cog._last_activity_at.get(999, 0.0), 0.0)


if __name__ == "__main__":
    unittest.main()