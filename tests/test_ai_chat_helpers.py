import unittest
from types import SimpleNamespace

from cogs.ai_chat import (
    maybe_suggest_channel_redirection,
    classify_reply_depth,
    extract_image_urls_from_attachments,
    extract_image_urls_from_embeds,
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


if __name__ == "__main__":
    unittest.main()