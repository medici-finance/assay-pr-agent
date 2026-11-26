import unittest
from unittest.mock import patch

from cursor_pr import cli, engine, source


TEXTS = ["def total(items):", "sum += item  # 🙂", "日本語のテキストです", "x = 1\n" * 50]


def count(name, text):
    from cursor_pr.vendor.tokens import encoding
    return len(encoding(name).encode(text, disallowed_special=()))


class EstimateTests(unittest.TestCase):
    def test_the_encoding_follows_the_model(self):
        for model in ("gpt-5-codex", "GPT-4.1-mini", "o3-mini", "o1-preview", "gpt-4o"):
            with self.subTest(model=model):
                self.assertEqual(engine.encodings(model), ("gpt-4o",))
        for model in ("gpt-4-turbo", "gpt-3.5-turbo"):
            with self.subTest(model=model):
                self.assertEqual(engine.encodings(model), ("gpt-4",))
        for model in ("claude-sonnet-4-5", "opus", "cursor-default", "gemini-2.5-pro", "", None):
            with self.subTest(model=model):
                self.assertEqual(engine.encodings(model), ("gpt-4", "gpt-4o"))

    def test_the_count_is_the_larger_where_no_encoding_is_known(self):
        self.assertTrue(any(count("gpt-4", text) != count("gpt-4o", text) for text in TEXTS))
        for text in TEXTS:
            gpt4, gpt4o = count("gpt-4", text), count("gpt-4o", text)
            self.assertEqual((engine.tokens(text, None), engine.tokens(text, "claude-sonnet-4-5")), (max(gpt4, gpt4o),) * 2)
            self.assertEqual((engine.tokens(text, "gpt-4-turbo"), engine.tokens(text, "gpt-5")), (gpt4, gpt4o))

    def test_the_budget_uses_the_selected_model(self):
        files = [source.File("a.py", "@@ -0,0 +1 @@\n+x = 1\n", "added")]
        with patch.object(cli, "tokens", wraps=engine.tokens) as counter:
            cli.fit(files, [], 6000, model="gpt-4-turbo")
        self.assertTrue(counter.call_args_list)
        self.assertEqual({call.args[1] for call in counter.call_args_list}, {"gpt-4-turbo"})


if __name__ == "__main__":
    unittest.main()
