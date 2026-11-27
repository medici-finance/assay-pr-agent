import hashlib
import os
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch as mock

from cursor_pr import engine, source
from cursor_pr.vendor import patch
from cursor_pr.vendor import tokens
from cursor_pr.vendor.tokens import encoding


SAMPLES = [
    "@@ -0,0 +1,2 @@\n+#!/bin/sh\n+echo $argument\n",
    "@@ -1,4 +1,4 @@\n import os\n-x = 1\n+y = 2\n print(x)\n",
    "@@ -3,2 +3,3 @@ def f():\n     a\n-    b\n+    c\n+    d\n@@ -20,2 +21,2 @@\n-old\n+new\n",
    "@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n",
    "@@ -1,3 +0,0 @@\n-gone\n-gone too\n-gone three\n",
    "@@ -1,2 +1,2 @@\n same\n-\r\n+x\r\n",
    "@@ not a header\n+misleading\n@@ -1 +1 @@\n-a\n+b\n",
    "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n",
    "@@ -1 +1 @@\n+\u2028+\n-x\n",
]
# Recorded from PR-Agent 0.50.0's own functions on the patches above.
FORMATTED = ["1633541172cb9e6f", "8c6167373d0623f8", "8269c11e2ede023a", "55c8ec9a547380d3", "a8901b919504f9d3",
             "db8589bd8f414c89", "55c8ec9a547380d3", "55c8ec9a547380d3", "fc4a9d4b92448773"]
TRIMMED = ["7613f0d0889151f5", "86e6d4ba6d8e5a39", "f43cf94fbff5a7be", "b00048f9733112c5", "6fdc7932921ca55a",
           "0020c3275bacef28", "03371dd976b22863", "e66fa3de3ec593c4", "e4724ce75e0d6b68"]
RUBRIC = "adc57144b7939b3ab541151f779b3071a7a3b42c96b315171b7cdd726846096a"


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class VendoredTests(unittest.TestCase):
    def test_the_rubric_is_what_pr_agent_renders_for_this_reviewer(self):
        instructions = engine.RUBRIC.replace("__MAX_FINDINGS__", str(engine.MAX_FINDINGS))
        self.assertEqual(hashlib.sha256(instructions.encode()).hexdigest(), RUBRIC)
        prompt = engine.pack([source.File("a.sh", "@@ -0,0 +1 @@\n+x\n", "added")], [])[0]
        self.assertTrue(prompt.startswith(instructions))

    def test_the_formatter_and_the_hunk_trimmer_match_pr_agent(self):
        for sample, formatted, trimmed in zip(SAMPLES, FORMATTED, TRIMMED):
            with self.subTest(sample=sample):
                shown = patch.decouple_and_convert_to_hunks_with_lines_numbers(sample, patch.FilePatchInfo("", "", sample, "f.py"))
                self.assertEqual(digest(shown), formatted)
                self.assertEqual(digest(patch.to_hunk_only_patch(sample)), trimmed)

    def test_the_numbering_the_model_is_shown(self):
        diff = "@@ -1,2 +1,3 @@\n keep\n-old\n+new\n"
        shown = patch.decouple_and_convert_to_hunks_with_lines_numbers(diff, patch.FilePatchInfo("", "", diff, "dir/f.py"))
        self.assertEqual(shown, "\n\n## File: 'dir/f.py'\n\n@@ -1,2 +1,3 @@\n__new hunk__\n1  keep\n2 +new\n"
                                "__old hunk__\n keep\n-old")
        self.assertEqual(patch.to_hunk_only_patch("--- a/f\n+++ b/f\n" + diff), diff)
        self.assertEqual(patch.to_hunk_only_patch("rename only\n"), "")

    def test_the_encoding_is_pr_agent_s_choice(self):
        self.assertEqual([encoding(name).name for name in ("gpt-4", "gpt-4o", "gpt-4.1", "claude-sonnet-4-5", "unknown")],
                         ["cl100k_base", "o200k_base", "o200k_base", "o200k_base", "o200k_base"])

    def test_the_encoding_files_are_bundled_and_checked_before_use(self):
        saved = {name: os.environ.get(name) for name in ("TIKTOKEN_CACHE_DIR", "DATA_GYM_CACHE_DIR")}
        try:
            for name in saved:
                os.environ.pop(name, None)
            tokens._ready = False
            for name, (key, expected) in tokens.FILES.items():
                self.assertEqual(hashlib.sha256(tokens.DIRECTORY.joinpath(key).read_bytes()).hexdigest(), expected, name)
            self.assertEqual(encoding("gpt-4").name, "cl100k_base")
            self.assertEqual(Path(os.environ["TIKTOKEN_CACHE_DIR"]), tokens.DIRECTORY)
            # A changed file would change every token count, so it is refused before it is used.
            with tempfile.TemporaryDirectory() as tmp, mock.object(tokens, "DIRECTORY", Path(tmp)):
                os.environ.pop("TIKTOKEN_CACHE_DIR", None)
                tokens._ready = False
                with self.assertRaisesRegex(ValueError, "cl100k_base encoding file is missing or altered"):
                    encoding("gpt-4")
                # A second call is refused as well; it must not fall through to a download.
                with self.assertRaisesRegex(ValueError, "cl100k_base encoding file is missing or altered"):
                    encoding("gpt-4")
                self.assertNotIn("TIKTOKEN_CACHE_DIR", os.environ)
            # The host's own choice of cache directory stands.
            # cl100k_base is already loaded above, so this reads no file from the host's directory.
            with mock.dict(os.environ, {"TIKTOKEN_CACHE_DIR": "/elsewhere"}):
                tokens._ready = False
                encoding("gpt-4")
                self.assertEqual(os.environ["TIKTOKEN_CACHE_DIR"], "/elsewhere")
        finally:
            # Put back what the run started with; the next count prepares the bundled files again.
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            tokens._ready = False

    def test_the_license_travels_with_the_code(self):
        text = files("cursor_pr.vendor").joinpath("LICENSE.pr-agent").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("MIT License\n\nCopyright (c) 2026 The PR Agent"))


if __name__ == "__main__":
    unittest.main()
