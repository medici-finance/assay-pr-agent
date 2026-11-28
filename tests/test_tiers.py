import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from cursor_pr import cli, context, discovery, engine
from cursor_pr.config import project_from
from cursor_pr.poll import Jobs, cycle, run_review

SHIM = """#!/usr/bin/env python3
import json, pathlib, sys
here = pathlib.Path(__file__).parent
prompt = sys.stdin.read()
assert prompt and prompt not in sys.argv, 'the prompt must arrive on standard input'
kind = ("verification" if prompt.startswith("You are checking the findings")
        else "deep" if "Look for what only this wider view shows" in prompt else "review")
with open(here / "prompts", "a") as log:
    log.write(json.dumps([kind, prompt]) + "\\n")
answer = (here / (kind + ".answer")).read_text()
if answer == "fail":
    sys.exit(3)
usage = {"input_tokens": {"review": 10, "deep": 50, "verification": 15}[kind], "output_tokens": 2}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": answer, "usage": usage}))
"""


def finding(line, header, path="src/app.py"):
    return {"relevant_file": path, "start_line": line, "end_line": line,
            "issue_header": header, "issue_content": header + " detail."}


def answer(*findings):
    return json.dumps({"review": {"key_issues_to_review": list(findings)}})


def verified_answer(*findings):
    return answer(*({"verification_status": "confirmed", **finding} for finding in findings))


class TierTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "repo"
        self.bin = Path(tmp.name) / "bin"
        self.bin.mkdir()
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        filler = "".join(f"# filler {n}\n" for n in range(40))
        self.write("src/app.py", filler + "def compute(value):\n    return value\n" + filler)
        self.write("src/caller.py", "from app import compute\n\nRESULT = compute(3)\n")
        self.write("tests/test_app.py", "from app import compute\n\ndef test_existing():\n    assert compute(1) == 1\n")
        self.commit("base")
        self.git("switch", "-qc", "feature")
        self.write("src/app.py", filler + "def compute(value):\n    return value * 2\n" + filler)
        self.commit("double the value\n\nCompute now doubles its input.")
        self.head = self.git("rev-parse", "HEAD")
        (self.bin / "cursor-agent").write_text(SHIM)
        (self.bin / "cursor-agent").chmod(0o700)
        self.answers(review=answer(finding(42, "Real"), finding(41, "Spurious")), deep=answer(finding(42, "Real")),
                     verification=verified_answer(finding(42, "Real")))
        env = patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"], "GH_TOKEN": "fixture",
                                      "REVIEW_STATE_DIR": str(Path(tmp.name) / "state")})
        env.start()
        self.addCleanup(env.stop)
        for name in ("REVIEW_MAX_INPUT_TOKENS", *cli.FINDINGS_SETTINGS.values()):
            os.environ.pop(name, None)

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                                        *args], cwd=self.root, text=True).strip()

    def write(self, path, text):
        (self.root / path).parent.mkdir(exist_ok=True)
        (self.root / path).write_text(text)

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)

    def answers(self, **kinds):
        for kind, text in kinds.items():
            (self.bin / (kind + ".answer")).write_text(text)

    def prompts(self):
        log = self.bin / "prompts"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def check(self, *args):
        argv = ["cursor-review", "--repo", str(self.root), "--cache-dir", str(self.root.parent / "cache"),
                "--base", "main", *args]
        out = io.StringIO()
        with patch("sys.argv", argv), contextlib.redirect_stdout(out):
            code = cli.main()
        return code, json.loads(out.getvalue())

    def test_budget_is_a_multiple_of_the_configured_limit(self):
        for limit in (6000, 9000):
            for tier, times in (("regular", 1), ("mid", 2), ("max", 10)):
                report = self.check("--tier", tier, "--estimate", "--max-input-tokens", str(limit))[1]
                self.assertEqual((report["max_input_tokens"], report["base_input_tokens"]), (limit * times, limit))
                self.assertLessEqual(report["estimated_input_tokens"], limit * times)
        self.assertEqual(self.prompts(), [])

    def test_empty_review_has_no_tier_degradation_or_provider_call(self):
        head = self.git("rev-parse", "main")
        for tier in cli.TIERS:
            with self.subTest(tier=tier):
                code, report = self.check("--tier", tier, "--head", head)
                self.assertEqual((code, report["state"], report["delivered"]), (0, "empty", None))
                code, estimated = self.check("--tier", tier, "--head", head, "--estimate")
                self.assertEqual((code, estimated["state"]), (0, "estimate"))
        self.assertEqual(self.prompts(), [])

    def test_regular_review_is_one_call_with_main_prompt_and_commit_context(self):
        self.answers(review=answer(dict(finding(42, "Real"), verification_status="unverified")))
        code, report = self.check()
        change = cli.local_change(self.root, "main", self.head)
        files = sorted(change.files, key=lambda f: f.path)
        prompt = engine.pack(files, [], title=change.title, description=change.description,
                             commit_messages=True)[0]
        self.assertEqual(self.prompts(), [["review", prompt]])
        self.assertIn("observable behavior on base and head", prompt)
        finding_schema = prompt.split("class KeyIssuesComponentLink(BaseModel):", 1)[1].split("class Review(BaseModel):", 1)[0]
        self.assertIn('verification_status: Literal["unverified"] | None', finding_schema)
        self.assertEqual((code, report["state"], report["tier"], report["delivered"]), (0, "reviewed", "regular", "regular"))
        self.assertEqual(report["verification"], {"state": "not-needed"})
        self.assertEqual(report["findings"][0]["verification_status"], "unverified")
        self.assertIn("Unverified Real", cli.finding_line(report["findings"][0]))

    def test_mid_adds_context_to_the_plain_prompt_and_verifies_the_findings(self):
        self.answers(verification=verified_answer(dict(finding(42, "Real"), verification_status="confirmed")))
        code, report = self.check("--tier", "mid")
        (first, prompt), (second, check) = self.prompts()
        self.assertEqual((first, second), ("review", "verification"))
        files = sorted(cli.local_change(self.root, "main", self.head).files, key=lambda f: f.path)
        plain = engine.pack(files, [], max_findings=6)[0]
        self.assertTrue(prompt.startswith(plain + "\nContext from outside the patch"))
        self.assertEqual(prompt.count("double the value"), 1)
        for expected in ("# Change description\ndouble the value", "Compute now doubles its input.",
                         "# Surrounding code\n## src/app.py lines 14-70 at the head", "14: # filler 13",
                         "# Call sites\n## Uses of compute", "src/caller.py:3: RESULT = compute(3)"):
            self.assertIn(expected, prompt)
        self.assertIn("42 +    return value * 2", check)
        self.assertIn('"issue_header": "Spurious"', check)
        self.assertIn("Base source (src/app.py)", check)
        self.assertIn("Head source (src/app.py)", check)
        self.assertIn("compare the same concrete scenario on base and head", check)
        self.assertEqual((code, report["state"], report["delivered"]), (0, "reviewed", "mid"))
        self.assertEqual([(f["line"], f["title"]) for f in report["findings"]], [(42, "Real")])
        self.assertEqual(report["findings"][0]["verification_status"], "confirmed")
        self.assertIn("Confirmed Real", cli.finding_line(report["findings"][0]))
        self.assertIn("Unverified Real", cli.finding_line(dict(report["findings"][0], verification_status="unverified")))
        self.assertEqual(report["verification"], {"state": "filtered", "before": 2, "after": 1})
        self.assertEqual({row["name"]: row["state"] for row in report["context"]},
                         {"change description": "included", "overlap with the base": "empty",
                          "surrounding code": "included", "call sites": "included"})
        self.assertEqual(report["cursor_invocations"], 2)
        self.assertLessEqual(report["estimated_input_tokens"], report["max_input_tokens"])

    def test_a_completed_verifier_can_leave_the_review_degraded(self):
        self.answers(verification=verified_answer(
            finding(42, "Real"), dict(finding(41, "Spurious"), verification_status="unverified")))
        code, report = self.check("--tier", "mid")
        self.assertEqual((code, report["state"], report["delivered"]), (2, "degraded", "mid"))
        self.assertEqual(report["verification"], {"state": "filtered", "before": 2, "after": 2})
        self.assertEqual([row["verification_status"] for row in report["findings"]], ["confirmed", "unverified"])
        with patch.object(cli, "post_comment") as post:
            cli.publish("org/repo#1", self.head, report)
        self.assertIn("Some retained findings are unverified.", post.call_args.args[3])

    def test_max_reuses_the_mid_prompt_then_runs_a_deep_pass(self):
        self.check("--tier", "mid")
        mid_prompt = self.prompts()[0][1]
        (self.bin / "prompts").unlink()
        self.answers(deep=answer(finding(42, "Real"), finding(43, "From the deep pass")),
                     verification=verified_answer(finding(42, "Real"), finding(43, "From the deep pass")))
        code, report = self.check("--tier", "max")
        self.assertEqual([kind for kind, _ in self.prompts()], ["deep", "verification"])  # The mid pass was cached.
        deep = self.prompts()[0][1]
        self.assertEqual(deep.count("double the value"), 1)
        self.assertEqual([(row["name"], row["state"]) for row in report["passes"]],
                         [("review", "cached"), ("deep", "called"), ("verification", "called")])
        self.assertFalse(report["receipt"]["cache_hit"])
        self.assertEqual(report["receipt"]["cached_passes"], 1)
        with patch.object(cli, "post_comment") as post:
            cli.publish("org/repo#1", self.head, report)
        self.assertIn("Receipt: 2 model calls (1 cached pass).", post.call_args.args[3])
        self.assertEqual(report["passes"][0]["estimated_input_tokens"], engine.tokens(mid_prompt))
        self.assertNotIn("model_usage", report["passes"][0])
        self.assertEqual([row["model_usage"]["input_tokens"] for row in report["passes"][1:]], [50, 15])
        self.assertEqual(report["model_usage"], {"input_tokens": 65, "output_tokens": 4})
        for expected in ("# Whole changed files\n## src/app.py at the head", "# Call sites in context\n## Use of compute in src/caller.py lines 1-3",
                         "# Existing tests\n## tests/test_app.py at the head", "def test_existing():", "# Change description"):
            self.assertIn(expected, deep)
        self.assertEqual((code, report["state"], report["delivered"]), (0, "reviewed", "max"))
        self.assertEqual([f["title"] for f in report["findings"]], ["Real", "From the deep pass"])
        self.assertEqual(report["verification"], {"state": "filtered", "before": 3, "after": 2})
        self.assertLessEqual(report["estimated_input_tokens"], report["max_input_tokens"])

    def test_configured_cap_is_asked_for_and_enforced_per_tier(self):
        # The fixture's review answer has two findings.
        os.environ["REVIEW_MAX_FINDINGS"] = "1"
        code, report = self.check()
        self.assertEqual((code, report["state"], report["max_findings"]), (0, "reviewed", 1))
        self.assertEqual([f["title"] for f in report["findings"]], ["Real"])
        self.assertEqual(report["dropped"],
                         [{"path": "src/app.py", "line": 41, "reason": "over the finding limit"}])
        self.assertIn("A concise list (0-1 issues)", self.prompts()[-1][1])
        os.environ["REVIEW_MAX_FINDINGS"] = "2"
        code, report = self.check()
        self.assertEqual((code, report["state"], report["cache_hit"]), (0, "reviewed", False))
        self.assertIn("A concise list (0-2 issues)", self.prompts()[-1][1])
        self.assertEqual(len(report["findings"]), 2)
        self.assertTrue(self.check()[1]["cache_hit"])
        # The option overrides the requested tier only, and a new cap is a new prompt and cache entry.
        code, report = self.check("--max-findings", "3")
        self.assertEqual((report["state"], report["max_findings"], report["cache_hit"]), ("reviewed", 3, False))
        self.assertIn("A concise list (0-3 issues)", self.prompts()[-1][1])
        (self.bin / "prompts").unlink()
        os.environ["REVIEW_MAX_FINDINGS_MID"] = "4"
        code, report = self.check("--tier", "mid")
        self.assertEqual(report["max_findings"], 4)
        self.assertIn("A concise list (0-4 issues)", self.prompts()[0][1])
        (self.bin / "prompts").unlink()
        os.environ["REVIEW_MAX_FINDINGS_MAX"] = "1"
        found = [finding(42, "Real"), finding(41, "Spurious"), finding(43, "Deep")]
        self.answers(deep=answer(found[0], found[2]), verification=verified_answer(*found))
        code, report = self.check("--tier", "max")
        # Two deep findings and a cap of one: the report keeps one deep finding.
        self.assertEqual((report["max_findings"], report["omitted_findings"]), (1, 2))
        self.assertEqual([f["title"] for f in report["findings"]], ["Real"])
        # A max review's first pass is the mid prompt, under the mid cap, and reuses the cached mid answer.
        self.assertEqual(report["passes"][0]["state"], "cached")

    def test_cli_override_replaces_an_invalid_configured_cap(self):
        os.environ.update(REVIEW_MAX_FINDINGS="13", REVIEW_MAX_FINDINGS_MID="invalid")
        code, report = self.check("--estimate", "--max-findings", "5")
        self.assertEqual((code, report["max_findings"]), (0, 5))

        os.environ.update(REVIEW_MAX_FINDINGS_MID="4", REVIEW_MAX_FINDINGS_MAX="invalid")
        code, report = self.check("--tier", "max", "--max-findings", "2")
        self.assertEqual((code, report["max_findings"]), (0, 2))
        self.assertIn("A concise list (0-4 issues)", self.prompts()[0][1])

    def test_cli_rejects_a_cap_outside_the_bound(self):
        for value in ("0", "13", "x"):
            with self.subTest(value=value), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                cli.parser().parse_args(["--base", "main", "--max-findings", value])
        self.assertEqual(cli.parser().parse_args(["--base", "main", "--max-findings", "12"]).max_findings, 12)

    def test_qualgen_reaches_both_max_passes_without_replacing_tier_context(self):
        _, baseline = self.check("--tier", "max", "--estimate")
        qualgen = {"state": "complete", "advisories": [
            {"path": "src/app.py", "kind": "co-change", "detail": "Review the callers."},
            {"path": "not-in-the-patch.py", "kind": "co-change", "detail": "Unrelated advice."},
        ], "unscreened": []}
        with patch.object(cli.history, "screen", return_value=qualgen) as screen:
            code, report = self.check("--tier", "max", "--qualgen-store", str(self.root / "qualgen"))
        screen.assert_called_once()
        self.assertEqual((code, report["delivered"], report["qualgen"]), (0, "max", qualgen))
        for original, advised in zip(baseline["passes"], report["passes"][:2], strict=True):
            self.assertLessEqual(advised["estimated_input_tokens"], original["estimated_input_tokens"])
        calls = self.prompts()
        self.assertEqual([kind for kind, _ in calls], ["review", "deep", "verification"])
        for _, prompt in calls[:2]:
            self.assertIn("History advisories (untrusted data)", prompt)
            self.assertIn("Review the callers.", prompt)
            self.assertIn("Context from outside the patch", prompt)
            self.assertNotIn("Unrelated advice.", prompt)
        self.assertNotIn("History advisories", calls[-1][1])

    def test_first_pass_history_never_displaces_max_patch_coverage(self):
        small, large = "tests/test_small.py", "src/large.py"
        for path, count in ((small, 5), (large, 780)):
            self.write(path, "".join(f"VALUE_{n} = {n}\n" for n in range(count)))
        self.commit("add fixtures with different patch sizes")
        self.answers(review=answer(), deep=answer())
        qualgen = {"state": "complete", "advisories": [
            {"path": small, "kind": "co-change", "detail": "Callers need review " * 14}
            for _ in range(6)
        ], "unscreened": []}
        raw = {"change description": "Check how the new values are used.",
               "whole changed files": "These values come from the fixtures."}
        files = [file for file in cli.local_change(self.root, "main", "HEAD").files if file.path in (small, large)]
        mid_size = engine.tokens(engine.pack([file for file in files if file.path == small], [])[0])
        deep_size = engine.tokens(engine.pack(files, [], focus=cli.DEEP_FOCUS)[0])
        limit = (mid_size + deep_size + 200 + 8) // 9
        args = ("--tier", "max", "--max-input-tokens", str(limit), "--path", small, "--path", large)
        with patch.object(context, "gather", return_value=raw), patch.object(cli.history, "screen", return_value=qualgen):
            _, plain = self.check(*args)
            _, advised = self.check(*args, "--qualgen-store", str(self.root / "qualgen"))
        self.assertEqual(plain["degraded"], [])
        self.assertEqual((advised["delivered"], advised["degraded"]), ("max", []))

    def test_history_leaves_room_to_verify_a_mid_finding(self):
        issue = dict(finding(42, "Check the returned value"), issue_content="behavior " * 1330 + "here")
        self.answers(review=answer(issue), verification=verified_answer(issue))
        qualgen = {"state": "complete", "advisories": [
            {"path": "src/app.py", "kind": "co-change", "detail": "Check prior use of this path. " * 9},
        ], "unscreened": []}
        raw = {"change description": "The patch adjusts a returned value."}
        files = cli.local_change(self.root, "main", "HEAD").files
        text, _ = context.assemble(context.MID, raw, 1000)
        first_size = engine.tokens(engine.pack(files, [], context=text, context_description="history")[0])
        row = dict(path="src/app.py", line=42, end_line=42, title=issue["issue_header"], message=issue["issue_content"])
        verify_size = engine.tokens(engine.verification([row], files, radius=15))
        limit = (first_size + verify_size + 1) // 2 + 8
        args = ("--tier", "mid", "--max-input-tokens", str(limit))
        with patch.object(context, "gather", return_value=raw), \
             patch.object(cli.history, "screen", return_value=qualgen):
            _, plain = self.check(*args)
            code, advised = self.check(*args, "--qualgen-store", str(self.root / "qualgen"))
        self.assertEqual(plain["verification"]["state"], "filtered")
        self.assertGreater(plain["passes"][-1]["estimated_input_tokens"], plain["base_input_tokens"])
        self.assertEqual((code, advised["verification"]["state"]), (0, "filtered"))
        self.assertEqual(advised["qualgen"], qualgen)

    def test_max_retains_deep_findings_and_reports_the_cap(self):
        # The cap PR #43 gave the max tier, now set through its environment override.
        os.environ["REVIEW_MAX_FINDINGS_MAX"] = "10"
        first = [finding(42, f"First {index}") for index in range(1, 7)]
        deep = [finding(42, f"Deep {index}") for index in range(1, 7)]
        self.answers(review=answer(*first), deep=answer(*deep))
        for state, verdict in (("filtered", verified_answer(*first, *deep)), ("unavailable", "fail")):
            with self.subTest(verification=state):
                self.answers(verification=verdict)
                _, report = self.check("--tier", "max", "--model", state)
                self.assertEqual(report["verification"]["state"], state)
                self.assertEqual(report["omitted_findings"], 2)
                self.assertEqual([row["title"] for row in report["findings"]],
                                 [row["issue_header"] for row in first[:4] + deep])
                with patch.object(cli, "post_comment") as post:
                    cli.publish("org/repo#1", self.head, report)
                self.assertIn("2 findings omitted by the 10-finding tier limit.", post.call_args.args[3])

    def test_deep_pass_can_review_a_file_the_first_pass_cannot_fit(self):
        self.write("src/data.py", "".join(f"VALUE_{n} = {n}  # generated row\n" for n in range(250)))
        self.commit("add data rows")
        deep_only = finding(17, "Data issue", "src/data.py")
        self.answers(deep=answer(deep_only), verification=verified_answer(deep_only))
        base = ["--max-input-tokens", "1000"]
        for deep_only_scope in (True, False):
            with self.subTest(deep_only_scope=deep_only_scope):
                selected = ["--path", "src/data.py"] if deep_only_scope else []
                mid = self.check("--tier", "mid", "--estimate", *base, *selected)[1]
                if deep_only_scope:
                    self.assertEqual(mid["state"], "over-budget")
                else:
                    self.assertEqual(mid["degraded"], ["src/data.py"])
                code, report = self.check("--tier", "max", *base, *selected)
                expected_status = (2, "partial") if deep_only_scope else (0, "reviewed")
                self.assertEqual((code, report["state"]), expected_status)
                self.assertEqual((report["delivered"], report["degraded"]), ("max", []))
                self.assertEqual([row["name"] for row in report["passes"]],
                                 (["deep", "verification"] if deep_only_scope else ["review", "deep", "verification"]))
                self.assertEqual([item["path"] for item in report["findings"]], ["src/data.py"])
                reviewed = 1 if deep_only_scope else 2
                self.assertEqual(report["receipt"]["coverage"]["reviewed"], reviewed)
                self.assertEqual((report["receipt"]["skipped"], mid["receipt"]["skipped"]), (0, 1))
                self.assertLessEqual(report["estimated_input_tokens"], report["max_input_tokens"])

    def test_a_failed_deep_pass_is_not_reported_as_a_budget_skip(self):
        self.write("src/data.py", "".join(f"VALUE_{n} = {n}  # generated row\n" for n in range(250)))
        self.commit("add data rows")
        self.answers(review=answer(), deep="fail")
        code, report = self.check("--tier", "max", "--max-input-tokens", "1000")
        self.assertEqual((code, report["state"], report["degraded"]), (2, "degraded", ["src/data.py"]))
        self.assertEqual(report["receipt"]["skipped"], 0)
        with patch.object(cli, "post_comment") as post:
            cli.publish("org/repo#1", self.head, report)
        comment = post.call_args.args[3]
        self.assertIn("- Not reviewed (pass failed): ` src/data.py `", comment)
        self.assertNotIn("Not reviewed (token limit)", comment)

    def test_max_collects_dropped_findings_from_both_review_passes(self):
        real = finding(42, "Real")
        self.answers(review=answer(finding(60, "Out of scope")), deep=answer(real, "malformed"),
                     verification=verified_answer(real))
        code, report = self.check("--tier", "max")
        self.assertEqual((code, report["state"], report["delivered"]), (0, "reviewed", "max"))
        self.assertEqual([row["title"] for row in report["findings"]], ["Real"])
        self.assertEqual(report["dropped"], [
            {"path": "src/app.py", "line": 60, "reason": "cited line outside the reviewed patch"},
            {"path": "", "line": None, "reason": "malformed finding"},
        ])

    def test_max_with_only_shared_context_delivers_mid(self):
        with patch.object(context, "gather", return_value={"change description": "Describes this change."}):
            code, report = self.check("--tier", "max")
        self.assertEqual((code, report["state"], report["delivered"]), (2, "degraded", "mid"))
        self.assertEqual([kind for kind, _ in self.prompts()], ["review", "verification"])

    def test_verifier_can_use_more_than_the_planning_reserve(self):
        issue = dict(finding(42, "Detailed"), issue_content="Verified behavior in the changed routine. " * 350)
        self.answers(review=answer(issue), verification=verified_answer(issue))
        code, report = self.check("--tier", "mid", "--max-input-tokens", "5000")
        self.assertEqual((code, report["verification"]["state"]), (0, "filtered"))
        self.assertGreater(report["passes"][-1]["estimated_input_tokens"], 5000 // 4)
        self.assertLessEqual(report["estimated_input_tokens"], report["max_input_tokens"])

    def test_verification_rejects_a_rewritten_finding_and_can_retry(self):
        real = finding(42, "Real")
        self.answers(verification=verified_answer(dict(real, issue_content="Rewritten detail.")))
        code, report = self.check("--tier", "mid")
        self.assertEqual((code, report["state"]), (2, "degraded"))
        self.assertEqual(report["verification"], {"state": "unavailable", "before": 2})
        self.assertEqual([(row["title"], row["message"]) for row in report["findings"]],
                         [("Real", "Real detail."), ("Spurious", "Spurious detail.")])
        self.assertEqual([row["verification_status"] for row in report["findings"]], ["unverified", "unverified"])
        self.assertEqual(report["passes"][-1]["state"], "failed")
        self.assertEqual(report["passes"][-1]["model_usage"]["input_tokens"], 15)
        self.assertEqual(report["model_invocations"], 2)

        self.answers(verification=verified_answer(real))
        code, report = self.check("--tier", "mid")
        self.assertEqual((code, report["state"]), (0, "reviewed"))
        self.assertEqual(report["verification"], {"state": "filtered", "before": 2, "after": 1})
        self.assertEqual([row["state"] for row in report["passes"]], ["cached", "called"])
        self.assertEqual([row["title"] for row in report["findings"]], ["Real"])
        self.assertEqual(report["model_invocations"], 1)

        with patch.object(cli, "fit_verification", return_value=(None, 0)):
            code, report = self.check("--tier", "mid")
        self.assertEqual((code, report["verification"]["state"]), (2, "unavailable"))
        self.assertEqual([row["verification_status"] for row in report["findings"]], ["unverified", "unverified"])

    def test_malformed_verifier_finding_cannot_silently_remove_originals(self):
        self.answers(verification=verified_answer(finding(60, "Outside")))
        code, report = self.check("--tier", "mid")
        self.assertEqual((code, report["state"]), (2, "degraded"))
        self.assertEqual(report["verification"], {"state": "unavailable", "before": 2})
        self.assertEqual([row["title"] for row in report["findings"]], ["Real", "Spurious"])
        self.assertEqual([row["verification_status"] for row in report["findings"]], ["unverified", "unverified"])
        self.assertEqual(report["passes"][-1]["state"], "failed")
        self.answers(verification=verified_answer(finding(42, "Real")))
        code, report = self.check("--tier", "mid")
        self.assertEqual((code, report["state"]), (0, "reviewed"))
        self.assertEqual([row["state"] for row in report["passes"]], ["cached", "called"])
        self.assertEqual([row["title"] for row in report["findings"]], ["Real"])

    def test_dropped_rows_and_a_verified_empty_result_have_distinct_messages(self):
        self.answers(review=answer(finding(42, "Real"), "malformed"), verification=verified_answer())
        code, report = self.check("--tier", "mid")
        self.assertEqual((code, report["state"], report["findings"]), (0, "reviewed", []))
        self.assertEqual(report["verification"], {"state": "filtered", "before": 1, "after": 0})
        with patch.object(cli, "post_comment") as post:
            cli.publish("org/repo#1", self.head, report)
        body = post.call_args.args[3]
        self.assertIn("Dropped 1 model finding: malformed finding.", body)
        self.assertIn("No model findings remained after verification.", body)
        self.assertNotIn("No model finding passed the checks.", body)

    def test_verification_retains_the_original_quoted_evidence_status(self):
        real = finding(42, "Real")
        self.answers(review=answer(dict(real, relevant_code="42 +    return value * 2")),
                     verification=verified_answer(real))
        code, report = self.check("--tier", "mid")
        self.assertEqual((code, report["state"]), (0, "reviewed"))
        self.assertEqual(report["verification"], {"state": "filtered", "before": 1, "after": 1})
        self.assertEqual([row["evidence"] for row in report["findings"]], ["verified"])

    def test_max_keeps_verified_evidence_from_a_duplicate_deep_finding(self):
        real = finding(42, "Real")
        self.answers(review=answer(real), deep=answer(dict(real, relevant_code="return value * 2")),
                     verification=verified_answer(real))
        code, report = self.check("--tier", "max")
        self.assertEqual((code, report["state"], report["delivered"]), (0, "reviewed", "max"))
        self.assertEqual([(row["title"], row["evidence"]) for row in report["findings"]], [("Real", "verified")])

    def test_malformed_yaml_cannot_filter_original_findings(self):
        keep = dict(finding(42, "Real"), verification_status="INVALID_DATE")
        malformed_status = yaml.safe_dump({"review": {"key_issues_to_review": [keep]}}).replace("INVALID_DATE", "2026-02-30")
        for name, verdict in (("merge", "review: {<<: invalid, key_issues_to_review: []}"),
                              ("status", malformed_status),
                              ("metadata", "review: {key_issues_to_review: [], checked_on: 2026-02-30}"),
                              ("tagged", "review: {key_issues_to_review: [], count: !!int ''}")):
            with self.subTest(name=name):
                self.answers(verification=verdict)
                code, report = self.check("--tier", "mid", "--model", name)
                self.assertEqual((code, report["state"]), (2, "degraded"))
                self.assertEqual(report["verification"], {"state": "unavailable", "before": 2})
                self.assertEqual([row["title"] for row in report["findings"]], ["Real", "Spurious"])

    def test_an_answer_with_only_invalid_findings_does_not_poison_a_retry(self):
        self.answers(review=answer(finding(60, "Outside")))
        code, report = self.check()
        self.assertEqual((code, report["state"], report["cache_hit"]), (2, "findings-dropped", False))
        self.answers(review=answer(finding(42, "Real")))
        code, report = self.check()
        self.assertEqual((code, report["state"], report["cache_hit"]), (0, "reviewed", False))
        self.assertEqual([row["title"] for row in report["findings"]], ["Real"])
        self.assertTrue(self.check()[1]["cache_hit"])

    def test_tier_falls_back_to_the_one_below_and_says_so(self):
        change = cli.local_change(self.root, "main", self.head)
        files = sorted(change.files, key=lambda f: f.path)
        patch_size = engine.tokens(engine.pack(files, [], max_findings=cli.DEFAULT_MAX_FINDINGS)[0])
        # The first-pass allowance leaves room for the patch, with too little left for context.
        tight = (patch_size + 60) * 4 // 7
        code, report = self.check("--tier", "mid", "--max-input-tokens", str(tight))
        self.assertEqual((code, report["state"], report["delivered"]), (2, "degraded", "regular"))
        self.assertTrue(all(row["state"] in ("dropped", "empty") for row in report["context"]), report["context"])
        self.answers(deep="fail")
        code, report = self.check("--tier", "max")
        self.assertEqual((code, report["state"], report["delivered"]), (2, "degraded", "mid"))
        self.assertEqual(report["passes"][1], report["passes"][1] | {"name": "deep", "state": "failed"})
        self.assertEqual([f["title"] for f in report["findings"]], ["Real"])
        self.answers(verification="fail")
        code, report = self.check("--tier", "mid", "--model", "other")
        self.assertEqual((code, report["state"], report["verification"]), (2, "degraded", {"state": "unavailable", "before": 2}))
        self.assertEqual(len(report["findings"]), 2)  # Unverified findings are kept and labelled.

    def test_delivered_tier_is_what_the_completed_prompts_contained(self):
        # A deep pass with max-only context, but no mid-tier block in any prompt, is not a max review.
        with patch.object(context, "gather", return_value={"whole changed files": "## src/app.py at the head\nx = 1"}):
            code, report = self.check("--tier", "max")
        self.assertEqual([kind for kind, _ in self.prompts()], ["review", "deep", "verification"])
        self.assertEqual((code, report["state"], report["delivered"]), (2, "degraded", "regular"))
        # A deep pass that only reviews another file adds no max context either.
        self.write("src/data.py", "".join(f"VALUE_{n} = {n}  # generated row\n" for n in range(250)))
        self.commit("add data rows")
        with patch.object(context, "gather", return_value={"change description": "Describes this change."}):
            code, report = self.check("--tier", "max", "--max-input-tokens", "1000")
        self.assertEqual([row["name"] for row in report["passes"]], ["review", "deep", "verification"])
        self.assertEqual((code, report["state"], report["delivered"]), (2, "degraded", "mid"))

    def test_comment_and_report_claim_no_tier_or_context_that_did_not_reach_the_model(self):
        def comment(report):
            with patch.object(cli, "post_comment") as post:
                cli.publish("org/repo#1", self.head, dict(report, provider="cursor"))
            return post.call_args.args[3]
        self.answers(review="fail")
        code, report = self.check("--tier", "mid")
        self.assertEqual((code, report["state"], report["delivered"]), (2, "model-unavailable", None))
        self.assertEqual({row["state"] for row in report["context"]} - {"empty"}, {"not sent"})
        text = comment(report)
        self.assertIn("Tier mid: budget 12000 tokens (2× the configured 6000).\n", text)
        self.assertNotIn("delivered", text)
        self.assertNotIn("Context beyond the patch", text)
        code, report = self.check("--tier", "mid", "--max-input-tokens", "10")
        self.assertEqual((code, report["state"], report["delivered"]), (2, "over-budget", None))
        self.assertNotIn("delivered", comment(report))
        # A failed deep pass sent none of its own context.
        self.answers(review=answer(finding(42, "Real")), deep="fail", verification=verified_answer(finding(42, "Real")))
        code, report = self.check("--tier", "max", "--model", "other")
        states = {row["name"]: row["state"] for row in report["context"]}
        self.assertEqual((report["delivered"], states["whole changed files"], states["surrounding code"]),
                         ("mid", "not sent", "included"))
        self.assertIn("Context beyond the patch: change description, surrounding code, call sites.", comment(report))

    def test_model_invocations_are_counted_not_inferred_from_the_error(self):
        real = os.replace

        def replace(source, target):
            if Path(target).parent.name == "cache":
                raise OSError("cache volume is full")
            return real(source, target)
        with patch.object(engine.os, "replace", side_effect=replace):
            code, report = self.check()
        self.assertEqual(len(self.prompts()), 1)  # The CLI ran; only storing its answer failed.
        self.assertEqual((code, report["state"], report["model_invocations"]), (2, "model-unavailable", 1))
        (self.bin / "cursor-agent").write_text("#!/nonexistent/interpreter\n")
        code, report = self.check("--model", "other")
        self.assertEqual((code, report["state"], report["model_invocations"]), (2, "model-unavailable", 0))

    def test_posted_comment_reports_the_tier_its_context_and_any_fallback(self):
        report = self.check("--tier", "max", "--estimate")[1]
        report.update(state="degraded", provider="cursor", delivered="mid",
                      verification={"state": "filtered", "before": 3, "after": 1})
        report["passes"][1]["state"] = "failed"
        report["context"][2]["state"] = "truncated"
        report["context"][4]["state"] = "unavailable"
        with patch.object(cli, "post_comment") as post:
            cli.publish("org/repo#1", self.head, report)
        lines = post.call_args.args[3].splitlines()
        self.assertEqual(lines[1], "degraded")
        self.assertRegex(lines[2], r"^Tier max: budget 60000 tokens \(10× the configured 6000\), estimated use \d+\.$")
        self.assertEqual(lines[3:8], [
            "Degraded review: max was requested and mid was delivered.",
            "Context beyond the patch: change description, surrounding code (truncated), call sites, "
            "whole changed files, call sites in context, existing tests.",
            "Not included: earlier comments (unavailable).",
            "The deep pass did not complete.",
            "A verification pass kept 1 of 3 findings."])

    def test_large_high_tier_comment_preserves_findings_and_static_summary(self):
        report = self.check("--tier", "max", "--estimate")[1]
        message = "多" * 1700
        findings = [dict(path="src/app.py", line=n, end_line=n, title=f"Finding {n}", message=message)
                    for n in range(1, 11)]
        checks = [{"tool": "fixture", "state": "could-not-check", "path": "src/example.py", "findings": []}]
        report.update(state="reviewed", provider="cursor", findings=findings, checks=checks)
        with patch.object(cli, "post_comment") as post:
            cli.publish("org/repo#1", self.head, report)
        comment = post.call_args.args[3]
        self.assertLessEqual(len(comment.encode("utf-8")), cli.FORGE_COMMENT_BYTES)
        for n in range(1, 11):
            self.assertIn(f"Finding {n} ", comment)
        self.assertIn("fixture: could not check", comment)
        self.assertIn("shortened to fit the forge comment", comment)
        self.assertTrue(all(item["message"] == message for item in report["findings"]))

    def test_compact_comment_keeps_exact_paths_and_reports_skipped_path_counts(self):
        report = self.check("--tier", "max", "--estimate")[1]
        directory = "dir/" * 40
        finding_paths = [directory + filename for filename in ("first.py", "second.py")]
        findings = [dict(path=path, line=1, end_line=1, title="Issue", message="多" * 5000)
                    for path in finding_paths]
        skipped = ["src/skipped.py"] + [f"src/{'long/' * 640}{n}.py" for n in range(52)]
        outside = ["src/outside.py"]
        report.update(state="degraded", provider="cursor", findings=findings, degraded=skipped, omitted=outside)
        with patch.object(cli, "post_comment") as post:
            cli.publish("org/repo#1", self.head, report)
        comment = post.call_args.args[3]
        self.assertLessEqual(len(comment.encode("utf-8")), cli.FORGE_COMMENT_BYTES)
        for path in [*finding_paths, *outside, skipped[0]]:
            self.assertIn(path, comment)
        disclosed = comment.count("- Not reviewed (token limit):")
        self.assertIn(f"{len(skipped) - disclosed} files skipped by the token limit omitted", comment)

    def test_compaction_uses_spare_space_for_all_coverage_paths(self):
        report = self.check("--tier", "max", "--estimate")[1]
        directory = "long/" * 30
        skipped = [f"{directory}skip_{n}.py" for n in range(35)]
        outside = [f"{directory}outside_{n}.py" for n in range(25)]
        findings = [dict(path="app.py", line=1, end_line=1, title="Issue", message="多" * 3000)]
        report.update(state="degraded", provider="cursor", findings=findings, degraded=skipped, omitted=outside)
        with patch.object(cli, "post_comment") as post:
            cli.publish("org/repo#1", self.head, report)
        comment = post.call_args.args[3]
        self.assertIn("shortened to fit the forge comment", comment)
        for path in skipped + outside:
            self.assertIn(path, comment)
        self.assertNotIn("paths omitted", comment)

    def test_compact_comment_preserves_summaries_when_all_exact_paths_will_not_fit(self):
        report = self.check("--tier", "max", "--estimate")[1]
        findings = [dict(path="long_dir/" * 190 + f"module_{n}.py", line=1, end_line=1,
                         title=f"Finding {n}", message="Details" * 100) for n in range(10)]
        report.update(state="reviewed", provider="cursor", findings=findings)
        with patch.object(cli, "post_comment") as post:
            cli.publish("org/repo#1", self.head, report)
        comment = post.call_args.args[3]
        self.assertLessEqual(len(comment.encode("utf-8")), cli.FORGE_COMMENT_BYTES)
        for n in range(10):
            self.assertIn(f"Finding {n} ", comment)
            self.assertIn(f"module_{n}.py", comment)
        self.assertIn("…/", comment)

    def test_one_oversized_changed_file_does_not_remove_the_others_context(self):
        self.write("src/generated.py", "".join(f"TABLE_{n} = {n}\n" for n in range(context.MAX_BLOB // 10)))
        self.commit("add a large generated file")
        head = self.git("rev-parse", "HEAD")
        files = cli.local_change(self.root, "main", head).files
        for text in (context.surrounding(self.root, head, files), context.whole_files(self.root, head, files)):
            self.assertIsNotNone(text)
            self.assertIn("## src/app.py", text)
            self.assertNotIn("TABLE_1 ", text)

    def test_gitlink_leaves_context_for_the_other_changed_files(self):
        self.git("update-index", "--add", "--cacheinfo", "160000", "1" * 40, "dependency")
        self.git("commit", "-qm", "add a submodule")
        head = self.git("rev-parse", "HEAD")
        files = cli.local_change(self.root, "main", head).files
        self.assertTrue(any("Subproject commit" in file.patch for file in files))
        for gather in (context.surrounding, context.whole_files):
            with self.subTest(gather=gather):
                text = gather(self.root, head, files)
                self.assertIsNotNone(text)
                self.assertIn("## src/app.py", text)
                self.assertNotIn("## dependency", text)

    def test_numbered_context_uses_the_same_line_breaks_as_git(self):
        module = self.root / "src/app.py"
        module.write_text(module.read_text().replace("# filler 0\n", "# filler 0 same Git line\n", 1))
        self.write("src/caller.py", "from app import compute same Git line\n\nRESULT = compute(3)\n")
        self.commit("add a Unicode separator inside each source line")
        head = self.git("rev-parse", "HEAD")
        files = cli.local_change(self.root, "main", head).files
        surrounding = context.surrounding(self.root, head, files)
        callers = context.call_sites(self.root, head, files, radius=1)
        self.assertIn("1: # filler 0 same Git line\n2: # filler 1", surrounding)
        self.assertIn("3: RESULT = compute(3)", callers)

    def test_binary_file_beside_the_tests_does_not_remove_the_existing_tests(self):
        (self.root / "tests/app_logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00binary")
        self.commit("add a binary fixture")
        head = self.git("rev-parse", "HEAD")
        files = [f for f in cli.local_change(self.root, "main", head).files if f.path == "src/app.py"]
        text = context.existing_tests(self.root, head, files)
        self.assertIsNotNone(text)
        self.assertIn("## tests/test_app.py at the head", text)

    def test_existing_tests_handles_paths_git_would_quote(self):
        name = "tests/test_app_é.py"
        self.write(name, "def test_accented_path(): pass\n")
        self.commit("add a test with a non-ASCII filename")
        head = self.git("rev-parse", "HEAD")
        files = [f for f in cli.local_change(self.root, "main", head).files if f.path == "src/app.py"]
        text = context.existing_tests(self.root, head, files)
        self.assertIn(f"## {name} at the head", text)
        self.assertIn("def test_accented_path(): pass", text)
        self.assertIn("## tests/test_app.py at the head", text)

    def test_max_reports_context_when_no_prompt_fits(self):
        raw = {name: "Some useful evidence." for name in (*context.MID, *context.DEEP)}
        with patch.object(context, "gather", return_value=raw):
            code, report = self.check("--tier", "max", "--estimate", "--max-input-tokens", "1")
        self.assertEqual((code, report["state"]), (2, "over-budget"))
        self.assertEqual({row["name"] for row in report["context"]}, set(raw))
        self.assertTrue(all(row["state"] == "dropped" for row in report["context"]))

    def test_context_heading_alone_cannot_upgrade_the_delivered_tier(self):
        name = "whole changed files"
        long_line = "large source line " * 1000
        text, fates = context.assemble((name,), {name: "\n" + long_line}, 100)
        self.assertEqual((text, fates), ("", [{"name": name, "state": "dropped"}]))
        self.assertEqual(cli.reached([("review", [{"name": "change description", "state": "included"}]),
                                     ("deep", fates)]), "mid")

        for block in ("## src/app.py at the head", "## src/app.py at the head\n" + long_line):
            with self.subTest(block_size=len(block)):
                self.assertEqual(context.assemble((name,), {name: block}, 30),
                                 ("", [{"name": name, "state": "dropped"}]))

        text, fates = context.assemble((name,), {name: "Useful context\n" + long_line}, 100)
        self.assertIn("Useful context", text)
        self.assertEqual(fates, [{"name": name, "state": "truncated"}])

    def test_context_blocks_share_an_allowance_in_order(self):
        raw = {"a": "one\n" * 400, "b": None, "c": "", "d": "two\n" * 5}
        text, fates = context.assemble(("a", "b", "c", "d"), raw, 300)
        self.assertEqual([(row["name"], row["state"]) for row in fates],
                         [("a", "truncated"), ("b", "unavailable"), ("c", "empty"), ("d", "included")])
        self.assertLessEqual(engine.tokens(text), 300)
        self.assertIn("[truncated to fit the token budget]", text)
        self.assertEqual([row["state"] for row in context.assemble(("a", "d"), raw, 10)[1]], ["dropped", "dropped"])


class TierRequestTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.gh = project_from(dict(provider="github", url="https://github.com/acme/app", requester_ids=[7],
                                    checkout=tmp.name))
        self.since = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def note(self, comment, body):
        return dict(authorId=7, databaseId=comment, authorType="User", body=body,
                    createdAt="2026-07-01T00:00:00+00:00", updatedAt="2026-07-01T00:00:00+00:00",
                    url=f"{self.gh.change_url(3)}#issuecomment-{comment}")

    def test_each_command_queues_its_tier_and_the_runner_receives_it(self):
        notes = [self.note(81, "/review"), self.note(82, " /review-mid\n"), self.note(83, "/review-max"),
                 self.note(84, "/review-maxx"), self.note(85, "/review-mid now")]
        self.assertEqual([discovery.is_request(self.gh, 3, note, self.since) for note in notes],
                         [True, True, True, False, False])
        jobs, ran = Jobs(self.state), []
        with patch.object(discovery, "open_changes", return_value=([3], True)), \
             patch.object(discovery, "comment_threads", return_value=({3: notes}, True)):
            cycle({self.gh.key: self.gh}, self.state, jobs, {self.gh.key: self.since},
                  lambda project, number, state, *tier: ran.append(tier) or "reviewed")
        self.assertEqual(ran, [(), ("mid",), ("max",)])
        self.assertEqual(Jobs(self.state).tier((self.gh.key, 3, 83)), "max")  # Survives a restart.

    def test_poller_passes_the_tier_to_the_review_command(self):
        done = json.dumps({"state": "reviewed", "publication": "posted-or-already-present"}).encode()
        for tier, expected in (("regular", False), ("mid", True), ("max", True)):
            with patch("cursor_pr.poll.supervised", return_value=done) as run, \
                 patch("cursor_pr.poll.history.interval", return_value=0):
                self.assertEqual(run_review(self.gh, 3, self.state, tier), "reviewed")
            argv = run.call_args.args[0]
            self.assertEqual("--tier" in argv, expected)
            if expected:
                self.assertEqual(argv[argv.index("--tier") + 1], tier)

    def test_poller_passes_the_cap_of_the_requested_tier(self):
        done = json.dumps({"state": "reviewed", "publication": "posted-or-already-present"}).encode()
        with patch.dict(os.environ, {"REVIEW_MAX_FINDINGS": "2", "REVIEW_MAX_FINDINGS_MID": "5"}):
            os.environ.pop("REVIEW_MAX_FINDINGS_MAX", None)
            for tier, expected in (("regular", "2"), ("mid", "5"), ("max", None)):
                with patch("cursor_pr.poll.supervised", return_value=done) as run, \
                     patch("cursor_pr.poll.history.interval", return_value=0):
                    self.assertEqual(run_review(self.gh, 3, self.state, tier), "reviewed")
                argv = run.call_args.args[0]
                self.assertEqual(argv[argv.index("--max-findings") + 1] if "--max-findings" in argv else None, expected)
                self.assertEqual(run.call_args.kwargs["env"]["REVIEW_MAX_FINDINGS_MID"], "5")


if __name__ == "__main__":
    unittest.main()
