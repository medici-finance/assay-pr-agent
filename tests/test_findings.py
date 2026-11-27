import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from cursor_pr import cli, engine, source


ANCHORS = {"a.py": {2, 3, 4}}


def issue(path="a.py", start=2, end=2, header="Possible Bug", body="Why it matters."):
    return {"relevant_file": path, "start_line": start, "end_line": end, "issue_header": header, "issue_content": body}


def envelope(issues):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "result": json.dumps({"review": {"key_issues_to_review": issues}})})


class ParseAnswerTests(unittest.TestCase):
    def test_one_bad_finding_does_not_cost_the_others(self):
        parsed = engine.parse_answer(envelope([issue(), issue("a.py", 9)]), ANCHORS)
        self.assertEqual([f["path"] for f in parsed["findings"]], ["a.py"])
        self.assertEqual(parsed["dropped"], [{"path": "a.py", "line": 9, "reason": "cited line outside the reviewed patch"}])
        self.assertIsNone(parsed["cursor_usage"])

    def test_every_reason_and_the_finding_limit(self):
        issues = [issue(), "text", {"relevant_file": "a.py", "start_line": True, "end_line": 2}, issue("a.py", 4, 2),
                  issue(header=" "), dict(issue(), issue_header="x" * 5), issue(), issue(), issue()]
        parsed = engine.parse_answer(envelope(issues), ANCHORS)
        self.assertEqual([f["title"] for f in parsed["findings"]], ["Possible Bug", "xxxxx", "Possible Bug"])
        self.assertEqual(sorted(d["reason"] for d in parsed["dropped"]),
                         ["cited line outside the reviewed patch", "empty finding", "malformed finding",
                          "malformed finding", "over the finding limit", "over the finding limit"])

    def test_all_dropped_still_answers(self):
        parsed = engine.parse_answer(envelope([issue("gone.py"), issue("a.py", 2, 2, body="")]), ANCHORS)
        self.assertEqual(parsed["findings"], [])
        self.assertEqual(parsed["dropped"], [{"path": "gone.py", "line": 2, "reason": "cited line outside the reviewed patch"},
                                             {"path": "a.py", "line": 2, "reason": "empty finding"}])

    def test_a_dropped_record_holds_only_a_path_a_line_and_a_fixed_reason(self):
        for dropped in engine.parse_answer(envelope(["junk", issue("a.py", 50)]), ANCHORS)["dropped"]:
            self.assertEqual(set(dropped), {"path", "line", "reason"})

    def test_dropped_line_number_cannot_break_result_serialization(self):
        parsed_answer = {"review": {"key_issues_to_review": [issue(), issue(start=2**20_000)]}}
        with patch.object(engine.yaml, "safe_load", return_value=parsed_answer):
            result = engine.parse_answer(envelope([]), ANCHORS)
        self.assertEqual(json.loads(json.dumps(result))["dropped"],
                         [{"path": "a.py", "line": None, "reason": "cited line outside the reviewed patch"}])
        self.assertEqual(len(result["findings"]), 1)

    def test_the_answer_itself_must_still_be_valid(self):
        base = {"type": "result", "subtype": "success", "is_error": False}
        bad = [json.dumps(dict(base, subtype="error", result="{}")),
               json.dumps(dict(base, result="{")),
               json.dumps(dict(base, result="x" * 32769)),
               json.dumps(dict(base, result='{"review": {}}')),
               json.dumps(dict(base, result='{"review": {"key_issues_to_review": {}}}'))]
        for raw in bad:
            with self.subTest(raw=raw[:48]), self.assertRaises((source.ReviewError, ValueError, KeyError, yaml.YAMLError)):
                engine.parse_answer(raw, ANCHORS)


class DroppedSummaryTests(unittest.TestCase):
    def report(self, **extra):
        return {"provider": "cursor", "state": "reviewed", "findings": [], "checks": [], "omitted": [], **extra}

    def test_summary_discloses_dropped_findings_by_count_and_reason(self):
        posts = []
        dropped = [{"path": "@all", "line": 17, "reason": "cited line outside the reviewed patch"},
                   {"path": "b.py", "line": 3, "reason": "cited line outside the reviewed patch"},
                   {"path": "c.py", "line": None, "reason": "malformed finding"}]
        with patch.object(cli, "post_comment", side_effect=lambda *args: posts.append(args[3])):
            cli.publish("org/repo#1", "a" * 40, self.report(dropped=dropped))
            cli.publish("org/repo#1", "a" * 40, self.report())
        self.assertIn("Dropped 2 model findings: cited line outside the reviewed patch.", posts[0])
        self.assertIn("Dropped 1 model finding: malformed finding.", posts[0])
        self.assertIn("No model finding passed the checks.", posts[0])
        self.assertNotIn("No model findings in the reviewed scope.", posts[0])
        self.assertNotIn("@all", posts[0])
        self.assertNotIn("Dropped", posts[1])
        self.assertIn("No model findings in the reviewed scope.", posts[1])

    def test_a_review_whose_findings_were_all_dropped_is_not_reported_as_clean(self):
        parsed = {"findings": [], "dropped": [{"path": "a.py", "line": 9, "reason": "cited line outside the reviewed patch"}],
                  "cursor_usage": None}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        posts = []
        args = cli.parser().parse_args(["--repo", tmp.name, "--cache-dir", str(Path(tmp.name) / "cache"),
                                        "--change", "org/repo#1", "--post", "--max-input-tokens", "6000"])
        change = source.Change("a" * 40, [source.File("a.py", "@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n", "added")], "org/repo#1")
        with patch.object(cli, "forge_change", return_value=change), patch.object(cli, "scan_files", return_value=[]), \
             patch.object(engine, "invoke", return_value=parsed), \
             patch.object(cli, "post_comment", side_effect=lambda *args: posts.append(args[3])):
            report, code = cli.run_request(args)
        self.assertEqual((code, report["state"], report["findings"], report["dropped"]), (0, "reviewed", [], parsed["dropped"]))
        self.assertIn("Dropped 1 model finding: cited line outside the reviewed patch.", posts[0])
        self.assertNotIn("No model findings in the reviewed scope.", posts[0])

if __name__ == "__main__":
    unittest.main()
