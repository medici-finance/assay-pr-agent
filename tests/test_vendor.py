import hashlib
import os
import re
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch as mock

import tiktoken
import tiktoken.model

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
# Recorded from PR-Agent 0.50.0's own to_hunk_only_patch on the patches above; the trim is unchanged.
TRIMMED = ["7613f0d0889151f5", "86e6d4ba6d8e5a39", "f43cf94fbff5a7be", "b00048f9733112c5", "6fdc7932921ca55a",
           "0020c3275bacef28", "03371dd976b22863", "e66fa3de3ec593c4", "e4724ce75e0d6b68"]
# Awkward but well-formed patches: each must number exactly the head lines unidiff numbers.
AWKWARD = {
    "deletions only": "@@ -1,3 +1,1 @@\n keep\n-a\n-b\n",
    "additions only": "@@ -1,1 +1,3 @@\n keep\n+a\n+b\n",
    "new file": "@@ -0,0 +1,2 @@\n+x\n+y\n",
    "no newline on either side": "@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file\n",
    "section text": "@@ -3,2 +3,3 @@ def f(): @@ x\n     a\n-    b\n+    c\n+    d\n",
    "omitted counts": "@@ -3 +3 @@\n-a\n+b\n",
    "several hunks": "@@ -1,2 +1,2 @@\n a\n-b\n+c\n@@ -10,2 +10,3 @@\n k\n+n\n k2\n@@ -20 +21 @@\n-z\n+zz\n",
    "crlf": "@@ -1,2 +1,2 @@ fn\r\n a\r\n-b\r\n+c\r\n",
    "content that looks like the format": "@@ -1,3 +1,5 @@\n @@ -9 +9 @@\n __new hunk__\n-__old hunk__\n+++++ b/f\n+---- a/f\n+__new hunk__\n",
    # A blank context line whose leading space was lost is still a line of the hunk, wherever it falls.
    "blank context first": "@@ -1,2 +1,2 @@\n\n-a\n+c\n",
    "blank context before the next hunk": "@@ -1,2 +1,2 @@\n-a\n+c\n\n@@ -10 +10 @@\n-x\n+y\n",
    "context-only hunk": "@@ -1 +1 @@\n-a\n+b\n@@ -5,1 +5,1 @@\n c\n",
    "long and non-ASCII lines": "@@ -1,2 +1,2 @@\n é\n-ü\n+日本語" + "x" * 5000 + "\n",
}
RUBRIC = "adc57144b7939b3ab541151f779b3071a7a3b42c96b315171b7cdd726846096a"


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class VendoredTests(unittest.TestCase):
    def test_the_rubric_is_what_pr_agent_renders_for_this_reviewer(self):
        instructions = engine.RUBRIC.replace("__MAX_FINDINGS__", str(engine.MAX_FINDINGS))
        self.assertEqual(hashlib.sha256(instructions.encode()).hexdigest(), RUBRIC)
        prompt = engine.pack([source.File("a.sh", "@@ -0,0 +1 @@\n+x\n", "added")], [])[0]
        self.assertTrue(prompt.startswith(instructions))

    def test_the_hunk_trimmer_matches_pr_agent(self):
        for sample, trimmed in zip(SAMPLES, TRIMMED):
            with self.subTest(sample=sample):
                self.assertEqual(digest(patch.to_hunk_only_patch(sample)), trimmed)
        self.assertEqual(patch.to_hunk_only_patch("rename only\n"), "")
        # Lines are split on line feeds only, as git and unidiff split them.
        self.assertEqual(patch.to_hunk_only_patch("diff --git a/x\x85@@ -1 +1 @@ b/x\n@@ -1 +1 @@\n-a\n+b\n"),
                         "@@ -1 +1 @@\n-a\n+b\n")

    def test_the_numbering_the_model_is_shown(self):
        diff = "@@ -1,2 +1,2 @@\n keep\n-old\n+new\n"
        shown = engine.section(source.File("dir/f.py", diff, "modified"))[0]
        # Byte for byte what PR-Agent 0.50.0 shows for a well-formed hunk.
        self.assertEqual(shown, "Change modified \n\n\n## File: 'dir/f.py'\n\n@@ -1,2 +1,2 @@\n__new hunk__\n1  keep\n2 +new\n"
                                "__old hunk__\n keep\n-old")
        self.assertEqual(patch.to_hunk_only_patch("--- a/f\n+++ b/f\n" + diff), diff)

    def test_every_anchor_is_shown_once_with_its_head_line_number(self):
        numbered = re.compile(r"(\d+) ([ +])(.*)")
        for name, diff in AWKWARD.items():
            with self.subTest(name):
                f = source.File("f.py", diff, "modified")
                text, anchors = engine.section(f)
                want = [(line.target_line_no, line.line_type, line.value.removesuffix("\n").removesuffix("\r"))
                        for hunk in engine.hunks(f) for line in hunk if line.target_line_no is not None]
                got = [m.groups() for m in map(numbered.fullmatch, text.split("\n")) if m]
                self.assertEqual([(int(n), marker, code) for n, marker, code in got], want)
                self.assertEqual(sorted(anchors), [n for n, _, _ in want])
                # Outside the numbered lines, only the format's own lines start a line without a marker.
                for line in text.split("\n"):
                    if not numbered.fullmatch(line) and line[:1] not in ("", " ", "-"):
                        self.assertRegex(line, r"^(?:Change |## File: |@@ -\d+,\d+ \+\d+,\d+ @@|__(?:new|old) hunk__$)")

    def test_a_line_break_inside_a_line_cannot_start_a_line_of_the_format(self):
        diff = "@@ -1 +1 @@\n-a\n+x\u2028__old hunk__\u2028-y\r__new hunk__\x0b\x1c\x85\n"
        f = source.File("f.py", diff, "modified")
        text, anchors = engine.section(f)
        self.assertIn("\n1 +x __old hunk__ -y __new hunk__   \n__old hunk__\n-a", text)
        self.assertEqual(text.splitlines(), text.split("\n"))
        # The quote check ignores whitespace, so a quote of the line as shown still matches.
        issue = {"relevant_file": "f.py", "start_line": 1, "end_line": 1, "issue_header": "Bug", "issue_content": "x",
                 "relevant_code": "1 +x __old hunk__ -y __new hunk__"}
        self.assertEqual(engine.finding(issue, {"f.py": anchors})[0]["evidence"], "verified")

    def test_the_encoding_is_pr_agent_s_choice(self):
        self.assertEqual([encoding(name).name for name in ("gpt-4", "gpt-4o", "gpt-4.1", "claude-sonnet-4-5", "unknown")],
                         ["cl100k_base", "o200k_base", "o200k_base", "o200k_base", "o200k_base"])

    def test_an_unreadable_encoding_is_not_replaced_by_another(self):
        loaded = tiktoken.get_encoding("o200k_base")

        def get_encoding(name):
            if name == "cl100k_base":
                raise OSError("cannot read the encoding file")
            return loaded
        with mock.object(tiktoken, "get_encoding", side_effect=get_encoding), \
                mock.object(tiktoken.model, "get_encoding", side_effect=get_encoding):
            with self.assertRaises(OSError):
                encoding("gpt-4")
            self.assertIs(encoding("gpt-unknown"), loaded)

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
