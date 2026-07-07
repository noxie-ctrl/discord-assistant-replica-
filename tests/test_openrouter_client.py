import asyncio
import os
import unittest
from unittest import mock

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
            asyncio.run(openrouter_client.describe_images(["https://example.com/image.png"]))

    def test_timeout_is_handled_not_a_crash(self):
        # Regression test: call_openrouter's except clauses reference
        # asyncio.TimeoutError, but the module used to not import asyncio at
        # all — so a real timeout raised NameError instead of being handled
        # and moving on to the next key, crashing the caller outright.
        os.environ["OPENROUTER_API_KEY"] = "test-key-1"
        try:
            with mock.patch.object(
                openrouter_client, "_call_one", side_effect=asyncio.TimeoutError
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    asyncio.run(openrouter_client.call_openrouter([{"role": "user", "content": "hi"}]))
                self.assertNotIsInstance(ctx.exception, NameError)
        finally:
            os.environ.pop("OPENROUTER_API_KEY", None)


if __name__ == "__main__":
    unittest.main()