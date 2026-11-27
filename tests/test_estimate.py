import unittest
from unittest.mock import patch

from cursor_pr import cli, engine, source


TEXTS = ["def total(items):", "sum += item  # 🙂", "日本語のテキストです", "x = 1\n" * 50]


def count(name, text):
    from pr_agent.algo.token_handler import TokenEncoder
    return len(TokenEncoder.get_token_encoder(name).encode(text, disallowed_special=()))


class EstimateTests(unittest.TestCase):
    def test_the_encoding_follows_the_model(self):
        for model in ("gpt-5-codex", "GPT-4.1-mini", "gpt-4.5-preview", "o3-mini", "o1-preview", "gpt-4o", "o3", "o1"):
            with self.subTest(model=model):
                self.assertEqual(engine.encodings(model), ("gpt-4o",))
        for model in ("gpt-4-turbo", "gpt-3.5-turbo", "gpt-35-turbo"):
            with self.subTest(model=model):
                self.assertEqual(engine.encodings(model), ("gpt-4",))
        for model in ("claude-sonnet-4-5", "opus", "cursor-default", "gemini-2.5-pro", "pro4-x", "", None):
            with self.subTest(model=model):
                self.assertEqual(engine.encodings(model), ("gpt-4", "gpt-4o"))

    def test_the_count_is_the_larger_where_no_encoding_is_known(self):
        self.assertTrue(any(count("gpt-4", text) != count("gpt-4o", text) for text in TEXTS))
        for text in TEXTS:
            gpt4, gpt4o = count("gpt-4", text), count("gpt-4o", text)
            self.assertEqual((engine.tokens(text, None), engine.tokens(text, "claude-sonnet-4-5")), (max(gpt4, gpt4o),) * 2)
            self.assertEqual((engine.tokens(text, "gpt-4-turbo"), engine.tokens(text, "gpt-5")), (gpt4, gpt4o))

    def test_the_budget_uses_the_selected_model(self):
        files = [source.File("a.py", "@@ -0,0 +1 @@\n+x = 1\n", "added"),
                 source.File("big.py", "@@ -0,0 +1,400 @@\n" + "".join(f"+value_{n} = compute({n})\n" for n in range(400)), "added")]
        with patch.object(cli, "tokens", wraps=engine.tokens) as counter:
            cli.fit(files[:1], [], 6000, model="gpt-4-turbo")
            # Over the limit, so the per-file sizes, the overhead and the recount are all counted too.
            degraded = cli.fit(files, [], 2500, model="gpt-4-turbo")[3]
        self.assertEqual(degraded, ["big.py"])
        self.assertGreater(len(counter.call_args_list), 4)
        self.assertEqual({call.args[1] if len(call.args) > 1 else None for call in counter.call_args_list}, {"gpt-4-turbo"})

    def test_fallback_budget_uses_the_same_encoding_for_overhead_and_each_file(self):
        description = "日本語のテキストです" * 150
        large = source.File("large.py", "@@ -0,0 +1 @@\n+#" + " tiktoken" * 1000 + "\n", "added")
        small = source.File("tests/test_small.py", "@@ -0,0 +1 @@\n+#" + " tiktoken" * 120 + "\n", "added")
        files = [large, small]
        prompt = engine.pack(files, [], frozenset({large.path}), description=description)[0]
        limit = engine.tokens(prompt)
        _, estimated, _, degraded = cli.fit(files, [], limit, description=description)
        self.assertEqual((estimated, degraded), (limit, [large.path]))


if __name__ == "__main__":
    unittest.main()
