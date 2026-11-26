import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_pr import cli, engine, source


def added(path, lines):
    return "@@ -0,0 +1,%d @@\n" % lines + "".join(f"+{path} line {n}\n" for n in range(lines))


class BudgetTriageTests(unittest.TestCase):
    def setUp(self):
        # app.py has a scanner finding on a shown line; deploy.sh and the workflow are what scanners check.
        self.files = [source.File("README.md", added("README", 12), "added"),
                      source.File(".github/workflows/ci.yml", added("ci", 4), "added"),
                      source.File("deploy.sh", added("deploy", 8), "added"),
                      source.File("app.py", added("app", 6), "added")]
        self.checks = [{"tool": "shellcheck", "path": "app.py", "state": "complete",
                        "findings": [{"path": "app.py", "line": 2, "rule": "SC2086", "message": "Quote it."}]}]
        self.change = source.Change("a" * 40, self.files, "org/repo#1")

    def size(self, files):
        return engine.tokens(engine.pack(files, self.checks)[0])

    def test_the_order_is_findings_then_scanned_then_other_then_documentation(self):
        order = sorted(self.files, key=lambda f: cli.priority(f, {"app.py": 1}.get(f.path, 0)))
        self.assertEqual([f.path for f in order], ["app.py", "deploy.sh", ".github/workflows/ci.yml", "README.md"])

    def test_nothing_is_skipped_while_everything_fits(self):
        plan = cli.fit(self.files, self.checks, self.change, (), 10000)
        self.assertEqual((plan["skipped"], plan["estimated"] == plan["full"], plan["text"]), ([], True, 4))
        self.assertEqual(plan["built"][0], engine.pack(self.files, self.checks)[0])

    def test_the_lowest_risk_files_are_skipped_until_the_prompt_fits(self):
        base = self.size([])
        cost = {f.path: self.size([f]) - base for f in self.files}
        budget = base + cost["app.py"] + cost["deploy.sh"] + 5
        plan = cli.fit(self.files, self.checks, self.change, (), budget)
        self.assertEqual((plan["skipped"], plan["text"]), ([".github/workflows/ci.yml", "README.md"], 4))
        self.assertLessEqual(plan["estimated"], budget)
        self.assertGreater(plan["full"], budget)
        for kept in ("deploy line", "app line", "SC2086"):
            self.assertIn(kept, plan["built"][0])
        for skipped in ("README line", "ci line"):
            self.assertNotIn(skipped, plan["built"][0])

    def test_nothing_fitting_is_refused_and_no_text_never_selects(self):
        plan = cli.fit(self.files, self.checks, self.change, (), self.size([]) + 1)
        self.assertEqual((plan["built"], plan["skipped"]), (None, sorted(f.path for f in self.files)))
        binary = [source.File("logo.png", "", "added", True)]
        plan = cli.fit(binary, [], source.Change("a" * 40, binary, "org/repo#1"), (), 1)
        self.assertEqual((plan["built"][3], plan["skipped"], plan["unseen"]), (False, [], ["logo.png"]))

    def test_a_review_reports_what_was_skipped(self):
        shown, posts = [], []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # scan_files is patched below, so there is no scanner evidence: the order is deploy.sh, ci.yml, app.py, README.md.
        def size(files):
            return engine.tokens(engine.pack(files, [])[0])
        base = size([])
        budget = base + sum(size([f]) - base for f in self.files[:3]) + 12
        args = cli.parser().parse_args(["--change", "org/repo#1", "--repo", tmp.name, "--cache-dir", str(Path(tmp.name) / "cache"),
                                        "--max-input-tokens", str(budget)])

        def invoke(prompt, model, anchors):
            shown.append(prompt)
            return {"findings": [], "dropped": [], "cursor_usage": None}
        with patch.object(cli, "forge_change", return_value=self.change), patch.object(cli, "scan_files", return_value=[]), \
             patch.object(engine, "invoke", side_effect=invoke), \
             patch.object(cli, "run_desk", side_effect=lambda argv: posts.append(Path(argv[-1]).read_text())):
            report, code = cli.run_request(args)
            cli.publish("org/repo#1", report["head"], report)
        self.assertEqual((code, report["state"]), (2, "partial"))
        self.assertEqual(report["skipped_for_budget"], ["README.md"])
        self.assertEqual(report["omitted"], ["README.md"])
        self.assertEqual((report["text_files"], report["full_estimated_input_tokens"] > budget), (4, True))
        self.assertIn("deploy line", shown[0])
        self.assertIn("app line", shown[0])
        self.assertNotIn("README line", shown[0])
        self.assertIn("the model reviewed 3 of 4 files with changes to review", posts[0])
        self.assertIn("` README.md `", posts[0])
        self.assertNotIn("Outside review coverage", posts[0])


if __name__ == "__main__":
    unittest.main()
