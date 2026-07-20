import unittest

from utils import aysa_knowledge
from cogs import aysa_chat


class ChunkTextTests(unittest.TestCase):
    def test_empty_text_returns_no_chunks(self):
        self.assertEqual(aysa_knowledge._chunk_text(""), [])
        self.assertEqual(aysa_knowledge._chunk_text("   \n\n   "), [])

    def test_short_text_is_a_single_chunk(self):
        chunks = aysa_knowledge._chunk_text("Just one short paragraph.")
        self.assertEqual(chunks, ["Just one short paragraph."])

    def test_paragraphs_are_packed_up_to_the_target(self):
        para = "x" * 500
        text = "\n\n".join([para] * 3)  # 1500 chars, under CHUNK_TARGET_CHARS (1800)
        chunks = aysa_knowledge._chunk_text(text)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].count(para), 3)

    def test_overflow_starts_a_new_chunk(self):
        para = "x" * 1000
        text = "\n\n".join([para] * 3)  # 3000+ chars, must overflow past one chunk
        chunks = aysa_knowledge._chunk_text(text)
        self.assertGreaterEqual(len(chunks), 2)
        for c in chunks:
            self.assertLessEqual(len(c), aysa_knowledge.CHUNK_TARGET_CHARS)

    def test_oversized_single_paragraph_is_hard_split(self):
        para = "y" * 5000
        chunks = aysa_knowledge._chunk_text(para)
        self.assertGreater(len(chunks), 1)
        # Reassembling (accounting for overlap) should still cover the source.
        self.assertTrue(all(c for c in chunks))

    def test_vector_literal_format(self):
        literal = aysa_knowledge._vector_literal([0.1, -0.2, 3.0])
        self.assertTrue(literal.startswith("[") and literal.endswith("]"))
        self.assertIn("0.1", literal)
        self.assertIn("-0.2", literal)


class CrisisLanguageDetectorTests(unittest.TestCase):
    def test_flags_common_crisis_phrasing(self):
        positives = [
            "I want to kill myself",
            "sometimes I think about suicide",
            "I just want to end my life",
            "I've been thinking about hurting myself",
            "I don't want to live anymore",
            "everyone would be better off without me",
        ]
        for text in positives:
            with self.subTest(text=text):
                self.assertTrue(aysa_chat._contains_crisis_language(text))

    def test_does_not_flag_ordinary_venting(self):
        negatives = [
            "work has been killing me lately, so stressful",
            "I want to talk about my anxiety",
            "this deadline is going to be the death of me lol",
            "how do I deal with a difficult coworker",
        ]
        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(aysa_chat._contains_crisis_language(text))

    def test_crisis_resources_text_present_and_reasonable(self):
        self.assertIn("988", aysa_chat.CRISIS_RESOURCES_TEXT)
        self.assertIn("findahelpline.com", aysa_chat.CRISIS_RESOURCES_TEXT)


if __name__ == "__main__":
    unittest.main()
