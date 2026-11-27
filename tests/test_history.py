import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import history

COUPLED = "historically coupled to beta.py, gamma.py, not in this screened set — check that partner too"
UNMINED = ("never/mined.py: could-not-screen — no hotspot record for this path: it was never seen by "
           "a `qualgen mine` run against this tracking root")
UNREADABLE = "Qualgen printed {} this reviewer could not read, so this list may be missing advisories."


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

    def snapshot(self):
        # A mined-looking store. screen reads only its header before it calls qualgen.
        head = self.advance(1)
        header = self.store / "current" / history.HEADER
        header.parent.mkdir(parents=True)
        header.write_text(json.dumps({"tip_sha": head}))
        return head

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

    def test_unreadable_check_output_keeps_parsed_advisories_and_says_so(self):
        head = self.snapshot()
        notice = "-source[1].py: NOTICE coupling-partner — historically coupled to partner.py, not in this screened set — check that partner too"
        with patch.object(history, "verified", return_value=True), patch.object(history, "qualgen",
                return_value=notice + '\n{"path": "partner.py", "notice": "PRIVATE-MARKER"}\n\n'), \
                self.assertLogs(history.log, level="WARNING") as logs:
            result = history.screen(self.repo, self.store, {"-source[1].py", "partner.py"}, head)
        self.assertEqual(result["advisories"], [{"path": "-source[1].py", "kind": "coupling-partner",
                         "detail": "historically coupled to partner.py, not in this screened set — check that partner too"}])
        self.assertEqual((result["state"], result["unscreened"]), ("partial", ["partner.py"]))
        self.assertEqual(result["unrecognized"], {"lines": 1, "paths": ["partner.py"]})
        self.assertEqual(history.summary(result), [
            f"Qualgen history advisories (baseline {head[:12]})",
            f"- ` -source[1].py — coupling-partner: {result['advisories'][0]['detail']} `",
            "- ` partner.py — Qualgen output could not be read `", UNREADABLE.format("1 line")])
        # One warning, and neither it nor the summary repeats Qualgen's text.
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("PRIVATE-MARKER", logs.output[0] + "".join(history.summary(result)))

    def test_unreadable_check_output_never_reads_as_a_clean_screen(self):
        head = self.snapshot()
        quiet, unmined = "partner.py: no advisory signal in this screen", "mate.py: could-not-screen — never seen by a mine run"
        lost = ["- ` mate.py — Qualgen output could not be read `", "- ` partner.py — Qualgen output could not be read `"]
        record = '{"path": "partner.py", "notice": "PRIVATE-MARKER"}'
        # (check output, unscreened, unreadable paths, rows, unreadable lines)
        cases = ((record + '\n{"path": "mate.py"}', ["mate.py", "partner.py"], ["mate.py", "partner.py"], lost, 2),
                 (record, ["mate.py", "partner.py"], ["mate.py", "partner.py"], lost, 1),
                 (quiet + "\nqualgen check: PRIVATE-MARKER", ["mate.py"], ["mate.py"], lost[:1], 1),
                 ("PRIVATE-MARKER: no advisory signal in this screen", ["mate.py", "partner.py"], ["mate.py", "partner.py"], lost, 1),
                 (quiet + "\nmate.py: no advisory signal in this screen\nPRIVATE-MARKER", [], [], [], 1),
                 (quiet + "\n" + unmined + "\nPRIVATE-MARKER", ["mate.py"], [],
                  ["- ` mate.py — insufficient history or unable to screen `"], 1))
        for output, unscreened, unreadable, rows, count in cases:
            with self.subTest(output=output):
                with patch.object(history, "verified", return_value=True), patch.object(history, "qualgen", return_value=output), \
                        self.assertLogs(history.log, level="WARNING") as logs:
                    result = history.screen(self.repo, self.store, {"partner.py", "mate.py"}, head)
                self.assertEqual((result["state"], result["advisories"], result["unscreened"]), ("partial", [], unscreened))
                self.assertEqual(result["unrecognized"], {"lines": count, "paths": unreadable})
                # An unreadable file is not passed off as one with insufficient history.
                note = UNREADABLE.format(f"{count} line{'s' if count > 1 else ''}")
                summary = history.summary(result)
                self.assertEqual(summary, [f"Qualgen history advisories (baseline {head[:12]})", *rows, note])
                # One warning, without the line it is about.
                self.assertEqual(len(logs.output), 1)
                self.assertNotIn("PRIVATE-MARKER", logs.output[0] + "".join(summary))
        # Blank lines carry nothing and are not drift.
        with patch.object(history, "verified", return_value=True), patch.object(history, "qualgen", return_value=quiet + "\n\n"):
            result = history.screen(self.repo, self.store, {"partner.py"}, head)
        self.assertEqual((result["state"], result["unscreened"]), ("complete", []))
        self.assertNotIn("unrecognized", result)
        self.assertEqual(history.summary(result)[-1], "No historical advisories for the selected files.")

    def test_a_path_never_sent_to_check_is_not_blamed_on_its_output(self):
        head = self.snapshot()
        with patch.object(history, "verified", return_value=True), patch.object(history, "qualgen", return_value='{"path": "partner.py"}') as check, \
                self.assertLogs(history.log, level="WARNING"):
            result = history.screen(self.repo, self.store, {"partner.py", "gone[1].py"}, head)
        self.assertEqual(check.call_args.args, ("check", self.repo, self.store / "current", "--", "partner.py"))
        self.assertEqual((result["unscreened"], result["unrecognized"]),
                         (["gone[1].py", "partner.py"], {"lines": 1, "paths": ["partner.py"]}))
        self.assertEqual(history.summary(result)[1:], ["- ` gone[1].py — insufficient history or unable to screen `",
                         "- ` partner.py — Qualgen output could not be read `", UNREADABLE.format("1 line")])

    def test_a_line_for_a_path_never_sent_to_check_is_not_trusted(self):
        head = self.snapshot()
        output = "partner.py: no advisory signal in this screen\ngone[1].py: NOTICE coupling-partner — planted\n"
        with patch.object(history, "verified", return_value=True), patch.object(history, "qualgen", return_value=output), \
                self.assertLogs(history.log, level="WARNING"):
            result = history.screen(self.repo, self.store, {"partner.py", "gone[1].py"}, head)
        self.assertEqual((result["state"], result["advisories"], result["unscreened"], result["unrecognized"]),
                         ("partial", [], ["gone[1].py"], {"lines": 1, "paths": []}))

    def test_unverified_release_leaves_the_screen_unavailable(self):
        head = self.snapshot()
        with patch.object(history, "_verified", None), patch.object(history, "reported_release", return_value="v1.0.95") as release, \
                patch.object(history, "qualgen") as check, self.assertLogs(history.log, level="WARNING") as logs:
            results = [history.screen(self.repo, self.store, {"partner.py"}, head) for _ in range(2)]
        self.assertEqual(results, [{"state": "unavailable", "advisories": [], "unscreened": [], "history_head": head,
                                    "reason": "unverified-release"}] * 2)
        # Read once per process; its text is never parsed, and the release it reported is not logged.
        release.assert_called_once_with(self.store / "current")
        check.assert_not_called()
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("v1.0.95", logs.output[0])
        self.assertEqual(history.summary(results[0]), [
            "Qualgen history unavailable for this review. The installed Qualgen release is not one this reviewer has verified."])
        self.assertEqual(history.summary({"state": "unavailable"}), ["Qualgen history unavailable for this review."])
        with patch.object(history, "_verified", None), patch.object(history, "reported_release", return_value="v1.0.94") as release, \
                patch.object(history, "qualgen", return_value="partner.py: no advisory signal in this screen\n") as check:
            states = [history.screen(self.repo, self.store, {"partner.py"}, head)["state"] for _ in range(2)]
        self.assertEqual(states, ["complete", "complete"])
        self.assertEqual((release.call_count, check.call_count), (1, 2))
        # A probe that fails, for example with no binary on PATH, is not remembered as an answer.
        with patch.object(history, "_verified", None), patch.object(history, "reported_release", side_effect=FileNotFoundError) as release:
            for _ in range(2):
                result = history.screen(self.repo, self.store, {"partner.py"}, head)
        self.assertEqual((release.call_count, result["state"]), (2, "unavailable"))
        self.assertNotIn("reason", result)

    @unittest.skipUnless(shutil.which("qualgen"), "qualgen is included in the image")
    def test_real_check_prints_the_lines_the_parser_reads(self):
        # Pins history.LINE to the real release's text. A Qualgen bump that changes it fails here, in
        # CI, instead of dropping advisories or reporting every review as unreadable.
        for n in (1, 2, 3):
            for name in ("alpha.py", "beta.py", "gamma.py"):
                (self.repo / name).write_text(f"value = {n}\n")
            self.git("add", "--all")
            self.git("commit", "-qm", str(n))
        history.refresh(self.repo, self.store)
        tracking = self.store / "current"
        def check(*paths):
            return history.qualgen("check", self.repo, tracking, "--", *paths, timeout=60)
        self.assertEqual(check("alpha.py", "beta.py", "gamma.py", "never/mined.py"),
                         "alpha.py: no advisory signal in this screen\nbeta.py: no advisory signal in this screen\n"
                         f"gamma.py: no advisory signal in this screen\n{UNMINED}\n")
        self.assertEqual(check("alpha.py", "never/mined.py"),
                         f"alpha.py: NOTICE coupling-partner — {COUPLED}\n{UNMINED}\n")
        # Lines that did not change need only the new release added.
        self.assertIn(history.reported_release(tracking), history.VERIFIED_RELEASES, "add this release to VERIFIED_RELEASES")
        head = history.tip(self.repo)
        quiet = history.screen(self.repo, self.store, {"alpha.py", "beta.py", "gamma.py", "never/mined.py"}, head)
        self.assertEqual((quiet["state"], quiet["advisories"], quiet["unscreened"]), ("partial", [], ["never/mined.py"]))
        advised = history.screen(self.repo, self.store, {"alpha.py", "never/mined.py"}, head)
        self.assertEqual((advised["state"], advised["unscreened"]), ("partial", ["never/mined.py"]))
        self.assertEqual(advised["advisories"], [{"path": "alpha.py", "kind": "coupling-partner", "detail": COUPLED}])
        for result in (quiet, advised):
            self.assertNotIn("unrecognized", result)
