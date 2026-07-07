import os
import unittest

from utils import openrouter_client


class OpenRouterClientTests(unittest.TestCase):
    def test_is_configured_false_by_default(self):
        # Ensure no OPENROUTER_API_KEY env vars are set in this test
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("OPENROUTER_API_KEY_2", None)
        self.assertFalse(openrouter_client.is_configured())

    def test_describe_images_raises_when_not_configured(self):
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("OPENROUTER_API_KEY_2", None)
        with self.assertRaises(RuntimeError):
            # Should raise because no key configured
            import asyncio
            asyncio.get_event_loop().run_until_complete(openrouter_client.describe_images(["https://example.com/image.png"]))


if __name__ == "__main__":
    unittest.main()
