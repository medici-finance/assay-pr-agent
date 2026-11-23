import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import history


class HistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.store = self.root / "tracking"
        self.git("init", "-q")

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=test@example.org", *args],
                       cwd=self.repo, check=True, capture_output=True)

    def advance(self, n):
        for name in ("-source[1].py", "partner.py", "source1.py"):
            (self.repo / name).write_text(f"value={n}\n")
        self.git("add", "--all")
        self.git("commit", "-qm", str(n))
        return history.tip(self.repo)

    def test_incremental_refresh_and_failure_leave_readable_snapshot(self):
        head = self.advance(1)
        history.refresh(self.repo, self.store)
        previous = (self.store / "current").resolve()
        with patch.object(history, "qualgen") as tool:
            history.refresh(self.repo, self.store)
            tool.assert_not_called()
        new_head = self.advance(2)
        with patch.object(history, "qualgen", side_effect=OSError("mine interrupted")):
            with self.assertRaises(OSError):
                history.refresh(self.repo, self.store)
        self.assertEqual(history.mined_tip(previous), head)
        self.assertEqual((self.store / "current").resolve(), previous)
        history.refresh(self.repo, self.store)
        self.assertEqual(history.mined_tip(self.store / "current"), new_head)
        paths = {"-source[1].py", "new.py"}
        result = history.screen(self.repo, self.store, paths, new_head)
        self.assertEqual(result["unscreened"], ["new.py"])
        self.assertEqual({a["path"] for a in result["advisories"]}, {"-source[1].py"})
        self.assertIn("partner.py", str(result["advisories"]))

    def test_pending_and_disabled(self):
        self.assertEqual(history.screen(self.repo, self.store, {"x"}, "a"*40)["state"], "unavailable")
        with patch.dict(os.environ, {"REVIEW_QUALGEN_INTERVAL_SECONDS": "0"}):
            self.assertEqual(history.interval(), 0)
