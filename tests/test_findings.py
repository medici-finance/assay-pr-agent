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
        self.assertIsNone(parsed["model_usage"])

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
                  "model_usage": None}
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


PATCH = ["def total(items):", "    total = 0", "    for item in items:", "        total += item", "    return total"]


def shown(*lines):
    return {"a.py": {n + 1: text + "\n" for n, text in enumerate(lines)}}


class QuotedEvidenceTests(unittest.TestCase):
    def parse(self, finding):
        return engine.parse_answer(envelope([finding]), shown(*PATCH))

    def test_a_quote_must_match_the_cited_lines(self):
        cases = (("        total += item\n    return total", 4, 5, "verified"),
                 ("4 +        total += item\n5 +    return total", 4, 5, "verified"),
                 ("total +=", 4, 4, "quoted code does not match the cited lines"),
                 ("return tot", 5, 5, "quoted code does not match the cited lines"),
                 ("4 +    return total", 5, 5, "quoted code does not match the cited lines"),
                 ("return total\ntotal += item", 4, 5, "quoted code does not match the cited lines"),
                 ("return total", 2, 3, "quoted code does not match the cited lines"),
                 ("raise ValueError", 4, 5, "quoted code does not match the cited lines"),
                 ("", 4, 5, "unverified"),
                 ("    for item in items:\n    ...\n    return total", 3, 5, "verified"),
                 ("3 +    for item in items:\n# …\n5 +    return total", 3, 5, "verified"),
                 ("__new hunk__\n4 +        total += item", 4, 4, "verified"),
                 ("4 +\n5 +", 4, 5, "unverified"),
                 ("...\nraise ValueError", 4, 5, "quoted code does not match the cited lines"))
        for quote, start, end, expected in cases:
            with self.subTest(quote=quote):
                parsed = self.parse(dict(issue(start=start, end=end), relevant_code=quote))
                if expected in ("verified", "unverified"):
                    self.assertEqual([(f["evidence"], f["line"], f["end_line"]) for f in parsed["findings"]],
                                     [(expected, start, end)])
                    self.assertEqual(parsed["dropped"], [])
                else:
                    self.assertEqual(parsed["findings"], [])
                    self.assertEqual(parsed["dropped"], [{"path": "a.py", "line": start, "reason": expected}])

    def test_a_quote_must_account_for_every_cited_line(self):
        cases = (("    return total", 4, 5, "unverified"),
                 ("        total += item", 3, 5, "unverified"),
                 ("    for item in items:\n    return total", 3, 5, "unverified"),
                 ("    for item in items:\n...", 3, 5, "verified"),
                 ("...\n    return total", 3, 5, "verified"),
                 ("    for item in items:\n        total += item\n    return total", 3, 5, "verified"))
        for quote, start, end, expected in cases:
            with self.subTest(quote=quote):
                parsed = self.parse(dict(issue(start=start, end=end), relevant_code=quote))
                self.assertEqual(([f["evidence"] for f in parsed["findings"]], parsed["dropped"]), ([expected], []))

    def test_blank_cited_lines_need_no_quote(self):
        anchors = shown("first = 1", "", "second = 2")
        parsed = engine.parse_answer(envelope([dict(issue(start=1, end=3), relevant_code="first = 1\nsecond = 2")]), anchors)
        self.assertEqual(parsed["findings"][0]["evidence"], "verified")

    def test_a_quote_of_only_trivial_lines_is_unverified_not_dropped(self):
        anchors = shown("call(", "    x", ")", "}", "return x", "x = 1", "});")
        cases = (("x", 2, 2, "unverified"), (")", 3, 3, "unverified"), ("}", 4, 4, "unverified"),
                 ("});", 7, 7, "unverified"), ("3 +)\n4 +}", 3, 4, "unverified"), ("...\n}", 1, 4, "unverified"),
                 ("return x", 5, 5, "verified"), ("x = 1", 6, 6, "verified"), ("return x\nx = 1\n});", 5, 7, "verified"),
                 ("call(\n...\n}", 1, 4, "verified"))
        for quote, start, end, expected in cases:
            with self.subTest(quote=quote):
                parsed = engine.parse_answer(envelope([dict(issue(start=start, end=end), relevant_code=quote)]), anchors)
                self.assertEqual(([f["evidence"] for f in parsed["findings"]], parsed["dropped"]), ([expected], []))
        # A short quote that is not on the cited lines still contradicts them.
        wrong = engine.parse_answer(envelope([dict(issue(start=4, end=4), relevant_code=")")]), anchors)
        self.assertEqual(wrong["dropped"][0]["reason"], "quoted code does not match the cited lines")

    def test_raw_numbers_plus_signs_and_literal_whitespace_stay_significant(self):
        anchors = shown("+value", "1 + 2", 'return "a  b"', 'value = "one\u2028two"', "offset,")
        for line, quote in ((1, "+value"), (1, "1 ++value"), (2, "1 + 2"), (3, 'return "a  b"'),
                            (4, '4 +value = "one\u2028two"')):
            with self.subTest(quote=quote):
                parsed = engine.parse_answer(envelope([dict(issue(start=line, end=line), relevant_code=quote)]), anchors)
                self.assertEqual(parsed["findings"][0]["evidence"], "verified")
        for line, quote in ((3, 'return "a b"'), (5, "5 + offset,")):
            wrong = engine.parse_answer(envelope([dict(issue(start=line, end=line), relevant_code=quote)]), anchors)
            self.assertEqual(wrong["dropped"][0]["reason"], "quoted code does not match the cited lines")

    def test_a_fenced_block_counts_as_the_quote_and_inline_spans_do_not(self):
        fenced = issue(start=4, end=4, body="It adds up.\n```\ntotal += item\n```\n")
        self.assertEqual(self.parse(fenced)["findings"][0]["evidence"], "verified")
        inline = issue(start=4, end=4, body="It uses `total` and `undefined_elsewhere()`.")
        self.assertEqual(self.parse(inline)["findings"][0]["evidence"], "unverified")
        both = dict(issue(start=4, end=4, body="```\nraise ValueError\n```"), relevant_code="total += item")
        self.assertEqual(self.parse(both)["findings"][0]["evidence"], "verified")
        # Without relevant_code, a fenced block that does not match may be a suggested fix: kept, unverified.
        fix = issue(start=4, end=4, body="It adds up.\n```\ntotal = sum(items)\n```\n")
        parsed = self.parse(fix)
        self.assertEqual(([f["evidence"] for f in parsed["findings"]], parsed["dropped"]), (["unverified"], []))

    def test_pack_records_the_text_of_every_shown_head_line(self):
        patch = "@@ -1,2 +1,2 @@\n first()\n-old\n+  third( x )\n"
        self.assertEqual(engine.pack([source.File("a.py", patch, "modified")], [])[1],
                         {"a.py": {1: "first()\n", 2: "  third( x )\n"}})

    def test_the_prompt_asks_for_the_quote_and_the_summary_counts_the_unchecked(self):
        prompt = engine.pack([source.File("a.py", "@@ -0,0 +1 @@\n+x = 1\n", "added")], [])[0]
        self.assertIn("Give each finding a `relevant_code` field quoting the lines it cites, exactly as shown.", prompt)
        posts = []
        report = {"provider": "cursor", "state": "reviewed", "checks": [], "omitted": [], "findings": [
            {"path": "a.py", "line": 1, "title": "t", "message": "m", "evidence": "unverified"},
            {"path": "a.py", "line": 1, "title": "t", "message": "m", "evidence": "verified"}]}
        with patch.object(cli, "post_comment", side_effect=lambda *args: posts.append(args[3])):
            cli.publish("org/repo#1", "a" * 40, report)
            cli.publish("org/repo#1", "a" * 40, dict(report, findings=report["findings"][1:]))
        self.assertIn("Could not verify quoted code for 1 model finding.", posts[0])
        self.assertNotIn("Could not verify quoted code", posts[1])


if __name__ == "__main__":
    unittest.main()
