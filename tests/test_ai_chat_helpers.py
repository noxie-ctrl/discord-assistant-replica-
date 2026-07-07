import unittest

from cogs.ai_chat import maybe_suggest_channel_redirection


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


if __name__ == "__main__":
    unittest.main()
