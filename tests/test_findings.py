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

    def test_invalid_yaml_key_in_the_envelope_is_rejected(self):
        raw = json.loads(envelope([]))
        raw["result"] = "!!bool invalid: metadata\nreview: {key_issues_to_review: []}"
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

    def test_dropped_path_detail_stays_bounded_without_losing_reasons(self):
        raw = envelope([issue(), issue(body=""), issue(), issue()])
        with patch.object(engine, "DROPPED_PATH_CHARS", len("a.py")):
            parsed = engine.parse_answer(raw, ANCHORS, limit=1)
        self.assertEqual([row["path"] for row in parsed["findings"]], ["a.py"])
        self.assertEqual(parsed["dropped"], [
            {"path": "a.py", "line": 2, "reason": "empty finding"},
            {"path": "", "line": 2, "reason": "over the finding limit"},
            {"path": "", "line": 2, "reason": "over the finding limit"},
        ])

    def test_aliased_finding_text_is_normalized_once(self):
        document = """
message: &message "  A shared finding description.  "
review:
  key_issues_to_review:
  - {relevant_file: a.py, start_line: 2, end_line: 2, issue_header: First, issue_content: *message}
  - {relevant_file: a.py, start_line: 2, end_line: 2, issue_header: Second, issue_content: *message}
"""
        (first, _), (second, _) = engine.answer_findings(document, ANCHORS)
        self.assertEqual(first["message"], "A shared finding description.")
        self.assertIs(first["message"], second["message"])

    def test_invalid_unicode_is_dropped_without_losing_astral_text(self):
        text = "Unexpected 🐍 Value"
        raw = envelope([issue(header=text), issue(header=chr(0xD800))])
        for verification in (False, True):
            with self.subTest(verification=verification):
                parsed = engine.parse_answer(raw, ANCHORS, verification=verification)
                self.assertEqual([row["title"] for row in parsed["findings"]], [text])
                self.assertEqual([row["reason"] for row in parsed["dropped"]], ["malformed finding"])

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
                 ("4 +\n5 +", 4, 5, "quoted code does not match the cited lines"),
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

    def test_numbered_rows_are_matched_as_code_even_when_they_look_like_elisions(self):
        for bogus in ("5 +...", "5 +# …", "5 +__new hunk__", "5 +"):
            with self.subTest(bogus=bogus):
                parsed = self.parse(dict(issue(start=4, end=5),
                                         relevant_code="4 +        total += item\n" + bogus))
                self.assertEqual(parsed["findings"], [])
                self.assertEqual(parsed["dropped"][0]["reason"], "quoted code does not match the cited lines")
        for literal in ("...", "__new hunk__", ""):
            with self.subTest(literal=literal):
                anchors = shown("result = 1", literal)
                explicit = dict(issue(start=1, end=2), relevant_code="1 +result = 1\n2 +" + literal)
                [finding] = engine.parse_answer(envelope([explicit]), anchors)["findings"]
                self.assertEqual(finding["evidence"], "verified")

    def test_numbered_quotes_preserve_trailing_whitespace_from_the_prompt(self):
        parsed = engine.parse_answer(envelope([dict(issue(start=1, end=1), relevant_code="1 +  result = 1  ")]),
                                     shown("  result = 1  "))
        self.assertEqual(parsed["findings"][0]["evidence"], "verified")

    def test_raw_quote_preserves_relative_indentation(self):
        anchors = shown("    if ready:", "        send()", "    finish()")
        for quote, expected in (("if ready:\n    send()\nfinish()", "verified"),
                                ("if ready:\n    send()\n    finish()", "unverified")):
            with self.subTest(quote=quote):
                parsed = engine.parse_answer(envelope([dict(issue(start=1, end=3), relevant_code=quote)]), anchors)
                self.assertEqual(parsed["findings"][0]["evidence"], expected)

    def test_uniform_shift_is_allowed_when_the_elision_skips_an_outdent(self):
        anchors = shown("if ready:", "    prepare()", "    send()")
        parsed = engine.parse_answer(envelope([dict(issue(start=1, end=3),
                                                   relevant_code="...\nprepare()\nsend()")]), anchors)
        self.assertEqual(parsed["findings"][0]["evidence"], "verified")

    def test_common_line_endings_match_in_raw_numbered_and_fenced_quotes(self):
        anchors = shown("prepare()", "send()")
        for ending in ("\n", "\r\n", "\r"):
            for rows in (("prepare()", "send()"), ("1 +prepare()", "2 +send()"),
                         ("```python", "prepare()", "send()", "```")):
                with self.subTest(ending=ending, rows=rows):
                    raw = envelope([dict(issue(start=1, end=2), relevant_code=ending.join(rows))])
                    parsed = engine.parse_answer(raw, anchors)
                    self.assertEqual([row["evidence"] for row in parsed["findings"]], ["verified"])
                    self.assertEqual(parsed["dropped"], [])

    def test_an_embedded_carriage_return_in_a_shown_row_remains_source_text(self):
        patch = "@@ -0,0 +1,2 @@\n+x\ry\n+ready()\n"
        display, anchors = engine.section(source.File("a.py", patch, "modified"))
        quote = display.split("__new hunk__\n", 1)[1]
        for ending, supplied in (("\n", quote), ("\n", "```text\n" + quote + "\n```"),
                                 ("\r\n", "```text\n" + quote + "\n```")):
            with self.subTest(ending=ending, supplied=supplied):
                raw = envelope([dict(issue(start=1, end=2), relevant_code=supplied.replace("\n", ending))])
                parsed = engine.parse_answer(raw, {"a.py": anchors})
                self.assertEqual([row["evidence"] for row in parsed["findings"]], ["verified"])
                self.assertEqual(parsed["dropped"], [])

    def test_blank_separators_outside_the_quoted_code_around_hunk_markers(self):
        quote = "__new hunk__\n\n1 +send()\n\n__old hunk__"
        raw = envelope([dict(issue(start=1, end=1), relevant_code=quote)])
        parsed = engine.parse_answer(raw, shown("send()"))
        self.assertEqual([row["evidence"] for row in parsed["findings"]], ["verified"])
        self.assertEqual(parsed["dropped"], [])

    def test_unnumbered_hunk_marker_rows_can_be_literal_source(self):
        for marker in ("__new hunk__", "__old hunk__"):
            relevant = f"prepare()\n{marker}\nsend()"
            anchors = shown("prepare()", marker, "send()")
            for quote in (relevant, f"{marker}\n{relevant}", f"{marker}\n\n{relevant}\n\n{marker}",
                          f"{marker}\n\n1 +prepare()\n2 +{marker}\n3 +send()",
                          f"...\nprepare()\n{marker}\nsend()\n..."):
                with self.subTest(quote=quote):
                    raw = envelope([dict(issue(start=1, end=3), relevant_code=quote)])
                    parsed = engine.parse_answer(raw, anchors)
                    self.assertEqual([row["evidence"] for row in parsed["findings"]], ["verified"])
                    self.assertEqual(parsed["dropped"], [])

    def test_a_numbered_match_does_not_set_the_raw_indentation_margin(self):
        anchors = shown("3 +    first()", "if enabled:", "    first()", "    second()", "    third()")
        quote = "...\n3 +    first()\nsecond()\nthird()"
        parsed = engine.parse_answer(envelope([dict(issue(start=1, end=5), relevant_code=quote)]), anchors)
        self.assertEqual(parsed["findings"][0]["evidence"], "verified")

    def test_marker_padding_retries_charge_for_the_cited_range(self):
        marker = "__new hunk__"
        source_lines = [marker] + ["send()"] * 999
        quote = (marker + "\n\n") * 30 + "absent()\n\n" + (marker + "\n\n") * 30
        raw = envelope([dict(issue(start=1, end=len(source_lines)), relevant_code=quote)])
        with (patch.object(engine, "EVIDENCE_WORK_LIMIT", 4500),
              patch.object(engine, "quoted_in_order", wraps=engine.quoted_in_order) as order):
            parsed = engine.parse_answer(raw, shown(*source_lines))
        self.assertEqual([row["evidence"] for row in parsed["findings"]], ["unverified"])
        self.assertLessEqual(order.call_count, 4)

    def test_an_earlier_duplicate_does_not_choose_the_only_indent(self):
        for indent in ("", "  "):
            with self.subTest(indent=indent):
                anchors = shown(indent + "prepare()", "    prepare()", "    send()")
                quote = "...\nprepare()\nsend()"
                parsed = engine.parse_answer(envelope([dict(issue(start=1, end=3), relevant_code=quote)]), anchors)
                self.assertEqual(parsed["findings"][0]["evidence"], "verified")

    def test_raw_quotes_cannot_change_trailing_source_whitespace(self):
        anchors = shown("    Authorization: token ")
        exact = dict(issue(start=1, end=1), relevant_code="Authorization: token ")
        [finding] = engine.parse_answer(envelope([exact]), anchors)["findings"]
        self.assertEqual(finding["evidence"], "verified")
        edited = dict(exact, relevant_code="Authorization: token")
        parsed = engine.parse_answer(envelope([edited]), anchors)
        self.assertEqual(parsed["findings"], [])
        self.assertEqual(parsed["dropped"][0]["reason"], "quoted code does not match the cited lines")

    def test_display_markers_and_elisions_must_keep_their_spelling(self):
        for extra in ("__newhunk__", ". . .", ".\u00a0.\u00a0."):
            with self.subTest(extra=extra):
                quoted = dict(issue(start=1, end=1), relevant_code="check_config()\n" + extra)
                parsed = engine.parse_answer(envelope([quoted]), shown("check_config()"))
                self.assertEqual(parsed["findings"], [])
                self.assertEqual(parsed["dropped"][0]["reason"], "quoted code does not match the cited lines")

    def test_covered_alignment_must_include_a_substantive_match(self):
        parsed = engine.parse_answer(envelope([dict(issue(start=1, end=2), relevant_code="1 +x\n...")]),
                                     shown("x", "1 +x"))
        self.assertEqual(parsed["findings"][0]["evidence"], "unverified")

    def test_non_ascii_space_is_not_a_blank_source_line(self):
        parsed = engine.parse_answer(envelope([dict(issue(start=1, end=3), relevant_code="before()\nafter()")]),
                                     shown("before()", "\u00a0", "after()"))
        self.assertEqual(parsed["findings"][0]["evidence"], "unverified")

    def test_an_elision_scans_a_blank_run_once_per_quote_line(self):
        class BlankLine(str):
            inspections = 0

            def strip(self, *args):
                BlankLine.inspections += 1
                return super().strip(*args)

        cited = [(n, BlankLine(" ")) for n in range(1, 101)] + [(101, "return x")]
        self.assertTrue(engine.covers(engine.checkable("...\nreturn x"), cited))
        self.assertLess(BlankLine.inspections, 4 * len(cited))

    def test_repeated_elisions_do_not_revisit_each_remaining_source_line(self):
        cited = [(n, "check()") for n in range(1, 501)]
        parts = engine.checkable("...\ncheck()\n" * 50 + "...")
        with patch.object(engine, "evidence_matches", wraps=engine.evidence_matches) as matches:
            self.assertTrue(engine.covers(parts, cited))
        self.assertLess(matches.call_count, 5 * len(cited))

    def test_expensive_alignment_retains_the_finding_as_unverified(self):
        source_lines = ["check()"] * 220
        quote = "...\ncheck()\ncheck()\n" * 25 + "..."
        raw = envelope([dict(issue(start=1, end=len(source_lines)), relevant_code=quote)])
        with patch.object(engine, "EVIDENCE_WORK_LIMIT", 1200):
            parsed = engine.parse_answer(raw, shown(*source_lines))
        self.assertEqual([row["evidence"] for row in parsed["findings"]], ["unverified"])
        self.assertEqual(parsed["dropped"], [])

    def test_invisible_formatting_does_not_make_a_quote_substantive(self):
        for invisible in ("\u200d", "\u115f", "\u1160", "\u2800", "\u3164", "\uffa0"):
            with self.subTest(invisible=invisible):
                source_line = "x" + invisible * 2
                raw = envelope([dict(issue(start=1, end=1), relevant_code=source_line)])
                self.assertEqual(engine.parse_answer(raw, shown(source_line))["findings"][0]["evidence"], "unverified")

    def test_blank_cited_lines_need_no_quote(self):
        anchors = shown("first = 1", "", "second = 2")
        parsed = engine.parse_answer(envelope([dict(issue(start=1, end=3), relevant_code="first = 1\nsecond = 2")]), anchors)
        self.assertEqual(parsed["findings"][0]["evidence"], "verified")

    def test_an_interior_quoted_blank_line_must_exist_in_source(self):
        lines = ['text = """first', 'second"""']
        quote = "\n".join([lines[0], "", lines[1]])
        raw = envelope([dict(issue(start=1, end=2), relevant_code=quote)])
        parsed = engine.parse_answer(raw, shown(*lines))
        self.assertEqual(parsed["findings"], [])
        self.assertEqual(parsed["dropped"][0]["reason"], "quoted code does not match the cited lines")
        raw = envelope([dict(issue(start=1, end=3), relevant_code=quote)])
        parsed = engine.parse_answer(raw, shown(lines[0], "", lines[1]))
        self.assertEqual(parsed["findings"][0]["evidence"], "verified")

    def test_explicit_interior_whitespace_preserves_blank_source_contents(self):
        start, end = 'text = """first', 'last"""'
        anchors = shown(start, " ", end)
        for middle, evidence in ((" ", "verified"), ("\t", "unverified")):
            with self.subTest(middle=middle):
                raw = envelope([dict(issue(start=1, end=3), relevant_code="\n".join((start, middle, end)))])
                parsed = engine.parse_answer(raw, anchors)
                self.assertEqual([row["evidence"] for row in parsed["findings"]], [evidence])

    def test_canonically_equivalent_substantive_text_has_the_same_threshold(self):
        for word in ("\u1100\u1161\u11a8", "\uac01"):
            with self.subTest(word=word):
                raw = envelope([dict(issue(start=1, end=1), relevant_code=word)])
                parsed = engine.parse_answer(raw, shown(word))
                self.assertEqual(parsed["findings"][0]["evidence"], "unverified")

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

    def test_one_markdown_fence_can_wrap_an_explicit_quote(self):
        anchors = shown("return value * 2")
        exact = dict(issue(start=1, end=1), relevant_code="```python\n1 +return value * 2\n```")
        parsed = engine.parse_answer(envelope([exact]), anchors)
        self.assertEqual([row["evidence"] for row in parsed["findings"]], ["verified"])
        self.assertEqual(parsed["dropped"], [])
        for quote in ("```python\nreturn value * 3\n```",
                      "Prose before the quote.\n\n" + exact["relevant_code"],
                      exact["relevant_code"] + "\n\n[ref]: /elsewhere"):
            with self.subTest(quote=quote):
                wrong = engine.parse_answer(envelope([dict(exact, relevant_code=quote)]), anchors)
                self.assertEqual(wrong["findings"], [])
                self.assertEqual(wrong["dropped"][0]["reason"], "quoted code does not match the cited lines")

    def test_wrapped_quote_can_cover_code_outside_a_literal_fence(self):
        quote = "```python\n...\nsend()\n...\n```"
        raw = envelope([dict(issue(start=1, end=5), relevant_code=quote)])
        parsed = engine.parse_answer(raw, shown("Before.", "```python", "send()", "```", "After."))
        self.assertEqual(parsed["findings"][0]["evidence"], "verified")

    def test_explicit_quote_can_include_literal_source_fences(self):
        quote = "```text\nvalue\n```"
        raw = envelope([dict(issue(start=1, end=3), relevant_code=quote)])
        parsed = engine.parse_answer(raw, shown("```text", "value", "```"))
        self.assertEqual(parsed["findings"][0]["evidence"], "verified")

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

    def test_a_fenced_quote_can_contain_a_shorter_backtick_run(self):
        source_line = 'const fence = "```";'
        body = "````js\n" + source_line + "\n````"
        [finding] = engine.parse_answer(envelope([issue(start=1, end=1, body=body)]), shown(source_line))["findings"]
        self.assertEqual(finding["evidence"], "verified")

    def test_tilde_fences_and_first_block_fallback(self):
        exact = "~~~~py\nresult = 1\n~~~~"
        anchors = shown("result = 1")
        first = engine.parse_answer(envelope([issue(start=1, end=1, body=exact)]), anchors)
        self.assertEqual(first["findings"][0]["evidence"], "verified")
        later = engine.parse_answer(envelope([issue(start=1, end=1, body="~~~\nresult = 2\n~~~\n" + exact)]), anchors)
        self.assertEqual(later["findings"][0]["evidence"], "unverified")

    def test_fenced_quote_inside_a_blockquote_matches_source(self):
        for body, source in (("> ```python\n> send()\n> ```", "send()"),
                             ("> > ~~~~\n> > > result\n> > ~~~~", "> result")):
            with self.subTest(body=body):
                parsed = engine.parse_answer(envelope([issue(start=1, end=1, body=body)]), shown(source))
                self.assertEqual(parsed["findings"][0]["evidence"], "verified")

    def test_fence_indentation_matches_the_report_and_verifier_message(self):
        body = "  ```python\nif ready:\n      send()\n  ```"
        anchors = shown("if ready:", "    send()")
        primary = envelope([issue(start=1, end=2, body="\n \n" + body + "\r\n \t\r\n")])
        [finding] = engine.parse_answer(primary, anchors)["findings"]
        self.assertEqual(finding["evidence"], "verified")
        self.assertEqual(finding["message"], body)
        self.assertEqual(engine.fenced_quote(finding["message"]), "if ready:\n    send()\n")
        reply = envelope([dict(issue(start=1, end=2, body=body), verification_status="confirmed")])
        verifier = engine.parse_answer(reply, anchors, verification=True)
        self.assertEqual(engine.kept([finding], verifier), [dict(finding, verification_status="confirmed")])

    def test_malformed_explicit_quote_cannot_use_the_message_fallback(self):
        fallback = issue(start=1, end=1, body="```python\nsend()\n```")
        anchors = shown("send()")
        for code in (["different()"], {"text": "different()"}, 123, False, "\ud800"):
            with self.subTest(code=code):
                reply = envelope([dict(fallback, relevant_code=code), fallback])
                parsed = engine.parse_answer(reply, anchors)
                self.assertEqual([item["evidence"] for item in parsed["findings"]], ["verified"])
                self.assertEqual(parsed["dropped"], [{"path": "a.py", "line": 1, "reason": "malformed quoted code"}])
        parsed = engine.parse_answer(envelope([dict(fallback, relevant_code=None)]), anchors)
        self.assertEqual([item["evidence"] for item in parsed["findings"]], ["verified"])

    def test_explicit_yaml_null_tag_requires_a_valid_null_spelling(self):
        issue_with_quote = dict(issue(start=1, end=1, body="```python\nsend()\n```"), relevant_code="QUOTE")
        reply = json.loads(envelope([]))
        payload = yaml.safe_dump({"review": {"key_issues_to_review": [issue_with_quote]}})
        reply["result"] = payload.replace("relevant_code: QUOTE", "relevant_code: !!null different()")
        parsed = engine.parse_answer(json.dumps(reply), shown("send()"))
        self.assertEqual(parsed["findings"], [])
        self.assertEqual(parsed["dropped"][0]["reason"], "malformed quoted code")
        reply["result"] = payload.replace("relevant_code: QUOTE", "relevant_code: !!null ~")
        parsed = engine.parse_answer(json.dumps(reply), shown("send()"))
        self.assertEqual([item["evidence"] for item in parsed["findings"]], ["verified"])

    def test_a_fenced_explicit_quote_cannot_normalize_nul_into_source_text(self):
        quote = "```python\nvalue = '\x00'\n```"
        parsed = engine.parse_answer(envelope([dict(issue(start=1, end=1), relevant_code=quote)]),
                                     shown("value = '\ufffd'"))
        self.assertEqual(parsed["findings"], [])
        self.assertEqual(parsed["dropped"][0]["reason"], "quoted code does not match the cited lines")

    def test_a_fenced_message_quote_cannot_normalize_nul_into_source_text(self):
        body = "```python\nvalue = '\x00'\n```"
        parsed = engine.parse_answer(envelope([issue(start=1, end=1, body=body)]), shown("value = '\ufffd'"))
        self.assertEqual([row["evidence"] for row in parsed["findings"]], ["unverified"])
        self.assertEqual(parsed["dropped"], [])

    def test_fenced_quote_in_an_ordered_list(self):
        body = "10. Code:\n    ```python\n    send()\n    ```"
        parsed = engine.parse_answer(envelope([issue(start=1, end=1, body=body)]), shown("send()"))
        self.assertEqual(parsed["findings"][0]["evidence"], "verified")

    def test_fenced_quote_can_end_without_a_closing_fence(self):
        for body in ("> ```python\n> send()\nDiscussion outside the quote.", "```python\nsend()"):
            with self.subTest(body=body):
                parsed = engine.parse_answer(envelope([issue(start=1, end=1, body=body)]), shown("send()"))
                self.assertEqual(parsed["findings"][0]["evidence"], "verified")

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
