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
        parsed = engine.parse_answer(envelope(issues), ANCHORS, limit=3)
        self.assertEqual([f["title"] for f in parsed["findings"]], ["Possible Bug", "xxxxx", "Possible Bug"])
        self.assertEqual(sorted(d["reason"] for d in parsed["dropped"]),
                         ["cited line outside the reviewed patch", "empty finding", "malformed finding",
                          "malformed finding", "over the finding limit", "over the finding limit"])

    def test_all_dropped_still_answers(self):
        parsed = engine.parse_answer(envelope([issue("@team secret"), issue("a.py", 2, 2, body="")]), ANCHORS)
        self.assertEqual(parsed["findings"], [])
        self.assertEqual(parsed["dropped"], [{"path": "", "line": 2, "reason": "cited line outside the reviewed patch"},
                                             {"path": "a.py", "line": 2, "reason": "empty finding"}])

    def test_a_dropped_record_holds_only_a_path_a_line_and_a_fixed_reason(self):
        for dropped in engine.parse_answer(envelope(["junk", issue("a.py", 50)]), ANCHORS)["dropped"]:
            self.assertEqual(set(dropped), {"path", "line", "reason"})

    def test_dropped_line_number_cannot_break_result_serialization(self):
        raw = json.loads(envelope([issue(), issue(start=123456)]))
        raw["result"] = raw["result"].replace("123456", "9" * 4500)
        result = engine.parse_answer(json.dumps(raw), ANCHORS)
        [dropped] = json.loads(json.dumps(result))["dropped"]
        self.assertIn(dropped["path"], ("", "a.py"))
        self.assertIsNone(dropped["line"])
        self.assertEqual(len(result["findings"]), 1)

    def test_a_yaml_value_that_cannot_be_constructed_discards_only_its_finding(self):
        raw = json.loads(envelope([]))
        review = {"review": {"key_issues_to_review": [issue(), issue(header="INVALID_DATE"), issue(header="Other Bug")]}}
        for scalar in ("2026-02-30", "!!bool invalid"):
            with self.subTest(scalar=scalar):
                raw["result"] = yaml.safe_dump(review).replace("INVALID_DATE", scalar)
                result = engine.parse_answer(json.dumps(raw), ANCHORS)
                self.assertEqual([finding["title"] for finding in result["findings"]], ["Possible Bug", "Other Bug"])
                self.assertEqual(result["dropped"], [{"path": "a.py", "line": 2, "reason": "malformed finding"}])

    def test_shared_nested_yaml_outside_the_finding_schema_is_ignored(self):
        raw = json.loads(envelope([]))
        extra = {"nested": ["INVALID_DATE"]}
        review = {"review": {"key_issues_to_review": [
            dict(issue(), extra=extra), dict(issue(header="Other Bug"), extra=extra),
        ]}}
        raw["result"] = yaml.safe_dump(review).replace("INVALID_DATE", "2026-02-30")
        result = engine.parse_answer(json.dumps(raw), ANCHORS)
        self.assertEqual([finding["title"] for finding in result["findings"]], ["Possible Bug", "Other Bug"])
        self.assertEqual(result["dropped"], [])

    def test_explicit_yaml_fields_override_shared_fields(self):
        raw = json.loads(envelope([]))
        raw["result"] = """
first: &first {relevant_file: a.py, start_line: 2, end_line: 2, issue_header: First}
second: &second {relevant_file: a.py, start_line: 3, end_line: 3, issue_header: Second}
review:
  key_issues_to_review:
  - <<: [*first, *second]
    issue_header: Explicit Header
    issue_content: Details.
"""
        [finding] = engine.parse_answer(json.dumps(raw), ANCHORS)["findings"]
        self.assertEqual((finding["line"], finding["end_line"], finding["title"]), (2, 2, "Explicit Header"))

    def test_bad_yaml_merge_in_the_envelope_is_rejected(self):
        raw = json.loads(envelope([]))
        for document in ("<<: invalid\nreview: {key_issues_to_review: []}",
                         "review: {<<: 0, key_issues_to_review: []}",
                         "review: {<<: [{key_issues_to_review: []}, null], key_issues_to_review: []}"):
            with self.subTest(document=document):
                raw["result"] = document
                with self.assertRaises(source.ReviewError):
                    engine.parse_answer(json.dumps(raw), ANCHORS)

    def test_bad_yaml_merge_in_one_finding_leaves_valid_siblings(self):
        raw = json.loads(envelope([]))
        good, bad = issue(), dict(issue(header="Bad Merge"), **{"<<": "BAD_MERGE"})
        raw["result"] = yaml.safe_dump({"review": {"key_issues_to_review": [good, bad, bad]}})
        raw["result"] = raw["result"].replace("'<<': BAD_MERGE", "<<: invalid")
        parsed = engine.parse_answer(json.dumps(raw), ANCHORS)
        self.assertEqual([finding["title"] for finding in parsed["findings"]], ["Possible Bug"])
        self.assertEqual([item["reason"] for item in parsed["dropped"]], ["malformed finding"] * 2)

    def test_aliased_citation_is_decoded_once(self):
        raw = json.loads(envelope([]))
        raw["result"] = """
citation: &line 2
review:
  key_issues_to_review:
  - {relevant_file: a.py, start_line: *line, end_line: *line, issue_header: First, issue_content: Details.}
  - {relevant_file: a.py, start_line: *line, end_line: *line, issue_header: Second, issue_content: Details.}
"""
        original = yaml.SafeLoader.construct_yaml_int
        with patch.object(yaml.SafeLoader, "construct_yaml_int", autospec=True, side_effect=original) as decode:
            parsed = engine.parse_answer(json.dumps(raw), ANCHORS)
        self.assertEqual([finding["line"] for finding in parsed["findings"]], [2, 2])
        decode.assert_called_once()

    def test_long_yaml_merge_chain_preserves_findings_from_shared_defaults(self):
        raw = json.loads(envelope([]))
        parents = ["base: &p0 {relevant_file: a.py, start_line: 2, end_line: 2, issue_content: Details.}"]
        parents.extend(f"k{i}: &p{i} {{<<: *p{i-1}}}" for i in range(1, 1051))
        raw["result"] = "\n".join([*parents, "review:", "  key_issues_to_review:",
                                    "  - {<<: *p1050, issue_header: Chain End}",
                                    "  - {<<: *p500, issue_header: Chain Middle}"])
        parsed = engine.parse_answer(json.dumps(raw), ANCHORS)
        self.assertEqual([finding["title"] for finding in parsed["findings"]], ["Chain End", "Chain Middle"])
        self.assertEqual(parsed["dropped"], [])

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
            cli.publish("org/repo#1", "a" * 40, self.report(state="findings-dropped", dropped=dropped))
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
        self.assertEqual((code, report["state"], report["findings"], report["dropped"]), (2, "findings-dropped", [], parsed["dropped"]))
        self.assertIn("Dropped 1 model finding: cited line outside the reviewed patch.", posts[0])
        self.assertNotIn("No model findings in the reviewed scope.", posts[0])
        self.assertIn("findings-dropped", posts[0])

    def run_with(self, parsed, files=None, scan=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        args = cli.parser().parse_args(["--repo", tmp.name, "--cache-dir", str(Path(tmp.name) / "cache"),
                                        "--change", "org/repo#1", "--max-input-tokens", "6000"])
        files = files or [source.File("a.py", "@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n", "added")]
        with patch.object(cli, "forge_change", return_value=source.Change("a" * 40, files, "org/repo#1")), \
             patch.object(cli, "scan_files", return_value=list(scan)), patch.object(engine, "invoke", return_value=parsed):
            return cli.run_request(args)

    def test_all_dropped_outranks_incomplete_coverage(self):
        parsed = {"findings": [], "dropped": [{"path": "a.py", "line": 9, "reason": "malformed finding"}], "cursor_usage": None}
        check = {"tool": "osv", "path": "go.mod", "state": "could-not-check", "findings": []}
        report, code = self.run_with(parsed, scan=[check])
        self.assertEqual((code, report["state"]), (2, "findings-dropped"))

    def test_some_dropped_or_none_returned_keeps_the_usual_state(self):
        kept = {"path": "a.py", "line": 1, "end_line": 1, "title": "T", "message": "M"}
        for parsed, state in (({"findings": [kept], "dropped": [{"path": "a.py", "line": 9, "reason": "malformed finding"}],
                                "cursor_usage": None}, "reviewed"),
                              ({"findings": [], "dropped": [], "cursor_usage": None}, "reviewed"),
                              ({"findings": [], "cursor_usage": None}, "reviewed")):
            with self.subTest(parsed=parsed):
                report, code = self.run_with(parsed)
                self.assertEqual((code, report["state"]), (0, state))


if __name__ == "__main__":
    unittest.main()
