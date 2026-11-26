import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_pr import cli, engine, source


class ProviderNeutralNamesTests(unittest.TestCase):
    def review(self, provider, team=""):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        file = source.File("app.py", "@@ -0,0 +1 @@\n+x = 1\n", "added")
        args = cli.parser().parse_args(["--change", "org/repo#1", "--repo", tmp.name, "--cache-dir", str(Path(tmp.name) / "cache"),
                                        "--max-input-tokens", "6000"])
        answer = {"findings": [], "dropped": [], "model_usage": {"input_tokens": 5}}
        with patch.dict(os.environ, {"REVIEW_PROVIDER": provider, "REVIEW_CURSOR_TEAM_ID": team}), \
             patch.object(cli, "forge_change", return_value=source.Change("a" * 40, [file], "org/repo#1")), \
             patch.object(cli.drift, "forge", return_value={"state": "could-not-check", "behind": None, "both": [], "references": []}), \
             patch.object(cli, "scan_files", return_value=[]), patch.object(engine, "invoke", return_value=answer):
            return cli.run_request(args)[0]

    def test_every_provider_reports_the_same_keys(self):
        self.assertFalse(hasattr(engine, "CursorFailure"))
        self.assertTrue(issubclass(engine.ModelFailure, source.ReviewError))
        for provider in ("cursor", "claude", "codex"):
            with self.subTest(provider=provider):
                report = self.review(provider)
                self.assertEqual((report["provider"], report["model_invocations"], report["model_usage"]),
                                 (provider, 1, {"input_tokens": 5}))
                self.assertEqual(report["receipt"]["usage"], {"input": 5})
                self.assertEqual([key for key in report if key.startswith("cursor_")], [])

    def test_a_cursor_team_keeps_its_own_name(self):
        report = self.review("cursor", team="42")
        self.assertEqual(report["cursor_team_id"], "42")


if __name__ == "__main__":
    unittest.main()
