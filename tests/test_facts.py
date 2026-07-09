import asyncio
import unittest
from unittest import mock

from utils import facts


class FactsClientTests(unittest.TestCase):
    def test_get_weather_empty_location_returns_message_not_error(self):
        result = asyncio.run(facts.get_weather(""))
        self.assertIn("No location", result)

    def test_search_fact_empty_query_returns_message_not_error(self):
        result = asyncio.run(facts.search_fact("   "))
        self.assertIn("No question", result)

    def test_get_weather_network_failure_degrades_gracefully(self):
        # Regression-style test matching the OpenRouterClientTests pattern:
        # a network failure should come back as a short honest string, never
        # an unhandled exception bubbling up into the chat pipeline.
        with mock.patch.object(facts.aiohttp, "ClientSession", side_effect=RuntimeError("boom")):
            result = asyncio.run(facts.get_weather("Jaipur"))
        self.assertIn("Couldn't check the weather", result)

    def test_search_fact_network_failure_degrades_gracefully(self):
        with mock.patch.object(facts.aiohttp, "ClientSession", side_effect=RuntimeError("boom")):
            result = asyncio.run(facts.search_fact("FIFA World Cup 2026"))
        self.assertIn("Couldn't look that up", result)

    def test_describe_weather_code_known(self):
        self.assertEqual(facts._describe_weather_code(0), "clear sky")
        self.assertEqual(facts._describe_weather_code(61), "light rain")

    def test_describe_weather_code_unknown_or_missing(self):
        self.assertEqual(facts._describe_weather_code(None), "conditions unclear")
        self.assertEqual(facts._describe_weather_code(12345), "conditions unclear")


if __name__ == "__main__":
    unittest.main()