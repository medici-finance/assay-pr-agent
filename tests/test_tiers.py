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
                     verification=answer(finding(42, "Real")))
        env = patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"], "GH_TOKEN": "fixture",
                                      "REVIEW_STATE_DIR": str(Path(tmp.name) / "state")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("REVIEW_MAX_INPUT_TOKENS", None)

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

    def test_regular_review_is_one_call_with_the_plain_prompt(self):
        self.answers(review=answer(finding(42, "Real")))
        code, report = self.check()
        files = sorted(cli.local_change(self.root, "main", self.head).files, key=lambda f: f.path)
        self.assertEqual(self.prompts(), [["review", engine.pack(files, [])[0]]])
        self.assertEqual((code, report["state"], report["tier"], report["delivered"]), (0, "reviewed", "regular", "regular"))
        self.assertEqual(report["verification"], {"state": "not-needed"})

    def test_mid_adds_context_to_the_plain_prompt_and_verifies_the_findings(self):
        code, report = self.check("--tier", "mid")
        (first, prompt), (second, check) = self.prompts()
        self.assertEqual((first, second), ("review", "verification"))
        files = sorted(cli.local_change(self.root, "main", self.head).files, key=lambda f: f.path)
        plain = engine.pack(files, [], max_findings=6)[0]
        self.assertTrue(prompt.startswith(plain + "\nContext from outside the patch"))  # Regular, plus more.
        for expected in ("# Change description\ndouble the value", "Compute now doubles its input.",
                         "# Surrounding code\n## src/app.py lines 14-70 at the head", "14: # filler 13",
                         "# Call sites\n## Uses of compute", "src/caller.py:3: RESULT = compute(3)"):
            self.assertIn(expected, prompt)
        self.assertIn("42 +    return value * 2", check)
        self.assertIn('"issue_header": "Spurious"', check)
        self.assertEqual((code, report["state"], report["delivered"]), (0, "reviewed", "mid"))
        self.assertEqual([(f["line"], f["title"]) for f in report["findings"]], [(42, "Real")])
        self.assertEqual(report["verification"], {"state": "filtered", "before": 2, "after": 1})
        self.assertEqual({row["name"]: row["state"] for row in report["context"]},
                         {"change description": "included", "overlap with the base": "empty",
                          "surrounding code": "included", "call sites": "included"})
        self.assertEqual(report["cursor_invocations"], 2)
        self.assertLessEqual(report["estimated_input_tokens"], report["max_input_tokens"])

    def test_max_reuses_the_mid_prompt_then_runs_a_deep_pass(self):
        self.check("--tier", "mid")
        mid_prompt = self.prompts()[0][1]
        (self.bin / "prompts").unlink()
        self.answers(deep=answer(finding(42, "Real"), finding(43, "From the deep pass")),
                     verification=answer(finding(42, "Real"), finding(43, "From the deep pass")))
        code, report = self.check("--tier", "max")
        self.assertEqual([kind for kind, _ in self.prompts()], ["deep", "verification"])  # The mid pass was cached.
        deep = self.prompts()[0][1]
        self.assertEqual([(row["name"], row["state"]) for row in report["passes"]],
                         [("review", "cached"), ("deep", "called"), ("verification", "called")])
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

    def test_deep_pass_can_review_a_file_the_first_pass_cannot_fit(self):
        self.write("src/data.py", "".join(f"VALUE_{n} = {n}  # generated row\n" for n in range(250)))
        self.commit("add data rows")
        deep_only = finding(17, "Data issue", "src/data.py")
        self.answers(deep=answer(deep_only), verification=answer(deep_only))
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
                self.assertLessEqual(report["estimated_input_tokens"], report["max_input_tokens"])

    def test_max_with_only_shared_context_delivers_mid(self):
        with patch.object(context, "gather", return_value={"change description": "Describes this change."}):
            code, report = self.check("--tier", "max")
        self.assertEqual((code, report["state"], report["delivered"]), (2, "degraded", "mid"))
        self.assertEqual([kind for kind, _ in self.prompts()], ["review", "verification"])

    def test_verifier_can_use_more_than_the_planning_reserve(self):
        issue = dict(finding(42, "Detailed"), issue_content="Verified behavior in the changed routine. " * 350)
        self.answers(review=answer(issue), verification=answer(issue))
        code, report = self.check("--tier", "mid", "--max-input-tokens", "5000")
        self.assertEqual((code, report["verification"]["state"]), (0, "filtered"))
        self.assertGreater(report["passes"][-1]["estimated_input_tokens"], 5000 // 4)
        self.assertLessEqual(report["estimated_input_tokens"], report["max_input_tokens"])

    def test_tier_falls_back_to_the_one_below_and_says_so(self):
        plain = self.check("--estimate")[1]["estimated_input_tokens"]
        # Twice the budget less the verification reserve leaves no room for context.
        tight = (plain + 60) * 4 // 7
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
        self.answers(review=answer(finding(42, "Real")), deep="fail", verification=answer(finding(42, "Real")))
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

    def test_one_oversized_changed_file_does_not_remove_the_others_context(self):
        self.write("src/generated.py", "".join(f"TABLE_{n} = {n}\n" for n in range(context.MAX_BLOB // 10)))
        self.commit("add a large generated file")
        head = self.git("rev-parse", "HEAD")
        files = cli.local_change(self.root, "main", head).files
        for text in (context.surrounding(self.root, head, files), context.whole_files(self.root, head, files)):
            self.assertIsNotNone(text)
            self.assertIn("## src/app.py", text)
            self.assertNotIn("TABLE_1 ", text)

    def test_binary_file_beside_the_tests_does_not_remove_the_existing_tests(self):
        (self.root / "tests/app_logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00binary")
        self.commit("add a binary fixture")
        head = self.git("rev-parse", "HEAD")
        files = [f for f in cli.local_change(self.root, "main", head).files if f.path == "src/app.py"]
        text = context.existing_tests(self.root, head, files)
        self.assertIsNotNone(text)
        self.assertIn("## tests/test_app.py at the head", text)

    def test_context_heading_alone_cannot_upgrade_the_delivered_tier(self):
        name = "whole changed files"
        long_line = "large source line " * 1000
        text, fates = context.assemble((name,), {name: "\n" + long_line}, 100)
        self.assertEqual((text, fates), ("", [{"name": name, "state": "dropped"}]))
        self.assertEqual(cli.reached([("review", [{"name": "change description", "state": "included"}]),
                                     ("deep", fates)]), "mid")

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
        with patch.object(discovery, "open_changes", return_value=[3]), \
             patch.object(discovery, "comment_threads", return_value={3: notes}):
            cycle({self.gh.key: self.gh}, self.state, jobs, {self.gh.key: self.since},
                  lambda project, number, state, *tier: ran.append(tier) or "reviewed")
        self.assertEqual(ran, [(), ("mid",), ("max",)])
        self.assertEqual(Jobs(self.state).tier((self.gh.key, 3, 83)), "max")  # Survives a restart.

    def test_poller_passes_the_tier_to_the_review_command(self):
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps({"state": "reviewed",
                                                                     "publication": "posted-or-already-present"}))
        for tier, expected in (("regular", False), ("mid", True), ("max", True)):
            with patch("cursor_pr.poll.subprocess.run", return_value=done) as run, \
                 patch("cursor_pr.poll.history.interval", return_value=0):
                self.assertEqual(run_review(self.gh, 3, self.state, tier), "reviewed")
            argv = run.call_args.args[0]
            self.assertEqual("--tier" in argv, expected)
            if expected:
                self.assertEqual(argv[argv.index("--tier") + 1], tier)


if __name__ == "__main__":
    unittest.main()
