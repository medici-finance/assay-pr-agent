import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from dataclasses import replace
import unittest
from unittest.mock import patch

from cursor_pr import engine, history, providers, setup, sync
from cursor_pr.config import Project, project_from

BROWSER = (shutil.which("google-chrome") or shutil.which("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
           or shutil.which("chromium"))


class InstallTests(unittest.TestCase):
    def test_membership_selection_and_verification(self):
        teams = [{"id": i, "name": n, "isSelectable": True, "isActive": i == 1}
                 for i, n in [(1, "Personal team"), (2, "Engineering")]]
        after = [dict(t, isActive=t["id"] == 2) for t in teams]
        with patch.object(providers, "team_command", return_value={"teams": teams}) as command:
            with self.assertRaisesRegex(ValueError, "Multiple Cursor teams"):
                providers.select_team("fixture", "", Path("."))

            command.side_effect = [{"teams": teams}, {"status":"switched", "team":{"id":2}}, {"teams":after}]
            self.assertEqual(providers.select_team("fixture", "", Path("."), lambda _: "2"), "2")
            self.assertEqual(command.call_args_list[-2].args[0], ["switch", "2"])
        with patch.object(providers, "team_command", return_value={"teams": [dict(teams[0], isSelectable=False)]}):
            with self.assertRaisesRegex(ValueError, "no eligible team"):
                providers.select_team("fixture", "", Path("."))
        with patch.object(providers, "team_command", return_value={"teams": []}):
            self.assertEqual(providers.select_team("x", "", Path(".")), "")
            with self.assertRaisesRegex(ValueError, "not eligible"):
                providers.select_team("x", "5", Path("."))

    def test_claude_and_codex_single_invocations_and_separate_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = root / "calls"
            program = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
assert 'forge-canary' not in os.environ.values()
assert sys.stdin.read() == 'review fixture'
output = json.dumps({'review': {'key_issues_to_review': []}})
if name == 'codex':
    assert 'ANTHROPIC_API_KEY' not in os.environ and sys.argv[1] == 'exec'
    Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(output)
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':23}}))
else:
    assert 'CODEX_API_KEY' not in os.environ and sys.argv[1] == '-p'
    assert sys.argv[sys.argv.index('--tools')+1] == ''
    print(json.dumps(dict(type='result', subtype='success', is_error=False, result=output, usage={'input_tokens':23})))
"""
            for name in ("codex", "claude"):
                binary = root / name
                binary.write_text(program + f"with open({str(calls)!r}, 'a') as f: f.write(name+'\\n')\n")
                binary.chmod(0o755)
            with patch.dict(os.environ, {"PATH": tmp + os.pathsep + os.environ["PATH"],
                    "CODEX_API_KEY": "x", "ANTHROPIC_API_KEY": "y", "GH_TOKEN": "forge-canary"}):
                for name in ("claude", "codex"):
                    with patch.dict(os.environ, {"REVIEW_PROVIDER": name}):
                        args = ("review fixture", None, {}, root / "cache", "source")
                        first, cached = engine.review(*args)
                        self.assertFalse(cached)
                        self.assertEqual(first["cursor_usage"], {"input_tokens": 23})
                        self.assertTrue(engine.review(*args)[1])
            self.assertEqual(calls.read_text().splitlines(), ["claude", "codex"])

    def test_selected_provider_mounted_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("fixture-codex")
            with patch.dict(os.environ, {"REVIEW_PROVIDER": "codex", "CODEX_API_KEY_FILE": str(key),
                                        "CURSOR_API_KEY_FILE": "/missing-unused"}, clear=True):
                setup.apply_saved_setup(Path(tmp))
                self.assertEqual(os.environ["CODEX_API_KEY"], "fixture-codex")
                self.assertNotIn("CURSOR_API_KEY", os.environ)

    def test_browser_auth_origin_and_stale_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = subprocess.Popen(["cursor-review-container", "setup", "--web", "--state-dir", tmp],
                                     stdout=subprocess.PIPE, text=True)
            try:
                # The console prints a fresh random key per wizard session; the URL carries nothing.
                def read_line():
                    line = child.stdout.readline()
                    self.assertTrue(line, "installer exited before printing the setup key")
                    return line.strip()
                first = read_line()
                while not first.startswith("Setup key: "):  # the build banner is printed first
                    first = read_line()
                key = first.split(": ", 1)[1]
                self.assertRegex(key, r"^[A-Za-z0-9_-]{43}$")  # secrets.token_urlsafe(32)
                second = child.stdout.readline().strip()
                self.assertIn("http://127.0.0.1:8765/", second)

                def request(method="GET", path="/step", headers=None, body=None):
                    conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
                    try:
                        conn.request(method, path, headers=headers or {}, body=body)
                        res = conn.getresponse()
                        return res.status, res.read(), res.getheader("Set-Cookie")
                    finally:
                        conn.close()

                origin = {"Origin": "http://127.0.0.1:8765", "Content-Type": "application/json"}
                # Without the cookie the page is asked for the console key; wrong Host is refused outright.
                self.assertEqual(request()[0], 401)
                self.assertEqual(request(headers={"Cookie": "pr_agent_setup=" + key, "Host": "other.invalid"})[0], 403)
                self.assertEqual(request("POST", "/login", {**origin, "Origin": "http://other.invalid"},
                                         json.dumps({"key": key}))[0], 403)
                self.assertEqual(request("POST", "/login", origin, json.dumps({"key": "wrong"}))[0], 401)
                status, _, issued = request("POST", "/login", origin, json.dumps({"key": key}))
                self.assertEqual(status, 200)
                self.assertTrue(issued.startswith("pr_agent_setup=" + key + ";"), issued)
                self.assertIn("HttpOnly", issued)
                self.assertIn("SameSite=Strict", issued)
                auth = {"Cookie": issued.split(";")[0]}
                # The page can ask the console to print the key again, at most once per interval.
                self.assertEqual(request("POST", "/print-key", headers={"Origin": "http://other.invalid"})[0], 403)
                self.assertEqual(request("POST", "/print-key", headers={"Origin": "http://127.0.0.1:8765"})[0], 204)
                self.assertEqual(child.stdout.readline().strip(), first)
                self.assertEqual(child.stdout.readline().strip(), second)
                self.assertEqual(request("POST", "/print-key", headers={"Origin": "http://127.0.0.1:8765"})[0], 429)
                for _ in range(50):
                    status, body, _ = request(headers=auth)
                    self.assertEqual(status, 200)
                    view = json.loads(body)
                    if view["status"] == "question":
                        break
                    time.sleep(0.02)
                self.assertEqual(view["status"], "question")
                answer = json.dumps({"id": view["id"], "answer": "gitlab"})
                headers = {**auth, **origin}
                self.assertEqual(request("POST", "/step", {**headers, "Origin": "http://other.invalid"}, answer)[0], 403)
                self.assertEqual(request("POST", "/step", headers, answer)[0], 200)
                self.assertEqual(request("POST", "/step", headers, answer)[0], 409)
                for _ in range(50):
                    current = json.loads(request(headers=auth)[1])
                    if current["status"] == "question":
                        break
                    time.sleep(0.02)
                quit_step = json.dumps({"action": "quit", "id": current["id"]})
                self.assertEqual(request("POST", "/step", headers, json.dumps({"action": "quit", "id": view["id"]}))[0], 409)
                self.assertEqual(request("POST", "/step", headers, quit_step)[0], 200)
                self.assertEqual(child.wait(timeout=5), 0)
                self.assertFalse((Path(tmp) / "setup").exists())
            finally:
                child.terminate()
                child.wait(timeout=5)
                child.stdout.close()

    def settings_page(self, state):
        """Start the installer on a saved setup and unlock its settings page."""
        bundle = state / ".review-setup-1"
        bundle.mkdir(mode=0o700)
        (bundle / "codex.key").write_text("fixture-codex\n")
        (bundle / "settings.json").write_text(json.dumps({"settings": {"REVIEW_PROVIDER": "codex", "REVIEW_AUTH_MODE": "api-key"},
                                                          "projects": [], "secrets": {"CODEX_API_KEY": "codex.key"}}))
        (state / "setup").symlink_to(bundle.name)
        child = subprocess.Popen(["cursor-review-container", "setup", "--web", "--state-dir", str(state)],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait, timeout=5)
        self.addCleanup(child.terminate)
        line = child.stdout.readline()
        while not line.startswith("Setup key: "):  # the build banner is printed first
            self.assertTrue(line, "installer exited before printing the setup key")
            line = child.stdout.readline()

        def request(method="GET", path="/step", headers=None, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
            try:
                conn.request(method, path, headers=headers or {}, body=body)
                res = conn.getresponse()
                return res.status, res.read(), res.getheader("Set-Cookie")
            finally:
                conn.close()
        origin = {"Origin": "http://127.0.0.1:8765", "Content-Type": "application/json"}
        status, _, issued = request("POST", "/login", origin, json.dumps({"key": line.split(": ", 1)[1].strip()}))
        self.assertEqual(status, 200)
        return child, request, {"Cookie": issued.split(";")[0], **origin}

    def current(self, request, headers, status):
        """The view once it reaches the given status."""
        for _ in range(50):
            view = json.loads(request(headers=headers)[1])
            if view["status"] == status:
                return view
            time.sleep(0.02)
        self.fail(f"The page never reached {status}: {view}")

    @unittest.skipUnless(BROWSER, "requires a Chromium-based browser")
    def test_settings_editor_keeps_a_single_draft_and_waits_for_import(self):
        fixture = Path(self.enterContext(tempfile.TemporaryDirectory()))
        assets = Path(setup.__file__).parent
        page = (assets / "setup.html").read_text().replace('src="/setup.js"', 'src="setup.js"')
        (fixture / "setup.js").write_text((assets / "setup.js").read_text())
        harness = """
<script>
const requests = [];
const view = {id: 1, status: "settings", messages: [], projects: [], sections: [
  {title: "Settings", settings: [
    {name: "model", kind: "text", label: "Model", value: "auto", help: ""},
    {name: "key", kind: "secret", label: "API key", state: "stored", help: ""}
  ]}
]};
window.fetch = async (path, options) => {
  if (path === "/setting") requests.push(JSON.parse(options.body));
  return new Response(JSON.stringify(view));
};
document.addEventListener("DOMContentLoaded", async () => {
  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const lookup = (key, selector) => document.querySelector('[data-key="' + key + '"] ' + selector);
  const select = (input, file) => {
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
    input.dispatchEvent(new Event("change"));
  };
  try {
    await tick();
    lookup("model", ".change").click();
    lookup("model", ".input").value = "draft";
    lookup("key", ".change").click();
    check(lookup("key", ".editor").hidden, "a second editor opened");
    check(lookup("model", ".input").value === "draft", "the draft disappeared");
    lookup("model", ".cancel").click();
    lookup("key", ".change").click();
    lookup("key", ".input").value = "old draft";
    const save = lookup("key", ".save"), disk = lookup("key", ".file");
    let finish;
    File.prototype.text = () => new Promise(resolve => { finish = resolve; });
    select(disk, new File(["ignored"], "key.txt"));
    save.click();
    check(save.disabled && requests.length === 0, "save submitted while the import was pending");
    finish("replacement");
    await tick();
    check(!save.disabled, "save stayed disabled after the import");
    const refreshed = new Promise(resolve => {
      const observer = new MutationObserver(() => { observer.disconnect(); resolve(); });
      observer.observe(document.querySelector("#groups"), {childList: true});
    });
    save.click();
    await refreshed;
    check(requests.length === 1 && requests[0].value === "replacement", "save sent the wrong value");
    check(!lookup("model", ".change").disabled, "the next editor stayed disabled");
    document.body.dataset.result = "passed";
  } catch (error) {
    document.body.dataset.result = error.message;
  }
});
</script>
"""
        (fixture / "index.html").write_text(page.replace("</head>", harness + "</head>"))
        result = subprocess.run([BROWSER, "--headless", "--disable-gpu", "--disable-background-networking",
                                 "--use-mock-keychain", "--no-first-run",
                                 f"--user-data-dir={fixture / 'profile'}", "--virtual-time-budget=3000",
                                 "--dump-dom", (fixture / "index.html").as_uri()],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('data-result="passed"', next(line for line in result.stdout.splitlines() if "<body" in line))

    def test_browser_settings_page_saves_one_setting(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        child, request, headers = self.settings_page(state)
        view = self.current(request, headers, "settings")
        names = [row["name"] for section in view["sections"] for row in section["settings"]]
        self.assertIn("REVIEW_MAX_INPUT_TOKENS", names)
        self.assertIn("CODEX_API_KEY", names)
        self.assertNotIn("fixture-codex", json.dumps(view))
        change = json.dumps({"id": view["id"], "name": "REVIEW_MAX_INPUT_TOKENS", "value": "9000"})
        # The wizard's guards apply: page origin, session cookie and the view the browser rendered.
        stale = json.dumps({"id": view["id"] - 1, "name": "REVIEW_MAX_INPUT_TOKENS", "value": "9000"})
        self.assertEqual(request("POST", "/setting", {k: v for k, v in headers.items() if k != "Origin"}, change)[0], 403)
        self.assertEqual(request("POST", "/setting", {**headers, "Host": "other.invalid"}, change)[0], 403)
        self.assertEqual(request("POST", "/setting", {"Origin": headers["Origin"], "Content-Type": "text/plain"}, change)[0], 403)
        self.assertEqual(request("POST", "/setting", {k: v for k, v in headers.items() if k != "Cookie"}, change)[0], 401)
        self.assertEqual(request("POST", "/setting", headers, stale)[0], 409)
        rejected = json.dumps({"id": view["id"], "name": "REVIEW_MAX_INPUT_TOKENS", "value": "0"})
        self.assertEqual(request("POST", "/setting", headers, rejected), (400, b'{"error": "Enter a positive integer"}', None))
        bundle = (state / "setup").readlink()
        status, body, _ = request("POST", "/setting", headers, change)
        self.assertEqual(status, 200)
        saved = json.loads(body)
        self.assertEqual(saved["status"], "settings")
        row = next(row for section in saved["sections"] for row in section["settings"] if row["name"] == "REVIEW_MAX_INPUT_TOKENS")
        self.assertEqual((row["value"], row["source"]), ("9000", "saved"))
        self.assertNotEqual((state / "setup").readlink(), bundle)
        self.assertEqual(json.loads((state / "setup" / "settings.json").read_text())["settings"]["REVIEW_MAX_INPUT_TOKENS"], "9000")
        # The view rendered before that save is now stale.
        self.assertEqual(request("POST", "/setting", headers, change)[0], 409)
        self.assertEqual(request("POST", "/step", headers, json.dumps({"id": saved["id"], "action": "quit"}))[0], 200)
        self.assertEqual(child.wait(timeout=5), 0)

    def test_browser_settings_page_starts_the_wizard(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        child, request, headers = self.settings_page(state)
        view = self.current(request, headers, "settings")
        self.assertEqual(request("POST", "/step", headers, json.dumps({"id": view["id"] - 1, "action": "wizard"}))[0], 409)
        self.assertEqual(request("POST", "/step", headers, json.dumps({"id": view["id"], "action": "wizard"}))[0], 200)
        question = self.current(request, headers, "question")
        self.assertEqual(question["label"], "Forge (github/gitlab):")
        # Nothing changes under a running wizard.
        self.assertEqual(request("POST", "/setting", headers, json.dumps({"id": view["id"], "name": "REVIEW_MAX_INPUT_TOKENS", "value": "9000"}))[0], 409)
        self.assertEqual(request("POST", "/step", headers, json.dumps({"id": question["id"], "action": "quit"}))[0], 200)
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertEqual((state / "setup").readlink(), Path(".review-setup-1"))

    def test_managed_paths_and_external_checkouts(self):
        row = dict(provider="github", url="https://github.com/org/repo", requester_ids=[2], managed=True)
        project = project_from(row)
        self.assertEqual(project.checkout, Path("/repos/github/github.com/org/repo/current"))
        with self.assertRaises(ValueError):
            project_from(row | {"url": "https://github.com/../repo"})
        with patch.object(sync, "reviewer_token_file") as token:
            sync.sync_project(project_from(row | {"managed":False,"checkout":"/tmp"}), Path("/tmp"))
            token.assert_not_called()

    @unittest.skipUnless(shutil.which("git-sync"), "git-sync is included in the image")
    def test_real_sidecar_updates_default_branch_retaining_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin = base / "upstream.git"
            subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
            project = Project("gitlab", (base/"upstream").as_uri(), "org/repo",
                              base/"managed"/"current", frozenset({1}), managed=True)
            secret = base/"token"
            secret.write_text("fixture")
            with patch.object(sync, "reviewer_token_file", return_value=secret):
                for version in ("one", "two"):
                    (origin/"source").write_text(version)
                    for args in (["add", "source"], ["commit", "-qm", version]):
                        subprocess.run(["git", "-C", str(origin), "-c", "user.name=Test",
                                        "-c", "user.email=test@example.org", *args], check=True)
                    sync.sync_project(project, base)
                    self.assertEqual((project.checkout/"source").read_text(), version)
            history.refresh(project.checkout, base / "tracking")
            self.assertEqual(history.mined_tip(base / "tracking/current"), history.tip(project.checkout))
            self.assertEqual(subprocess.check_output(["git", "-C", str(project.checkout),
                             "rev-list", "--count", "HEAD"], text=True).strip(), "2")

            # Review refs can point outside the synced default branch, including on forks.
            subprocess.run(["git", "-C", str(origin), "switch", "-qc", "candidate"], check=True)
            (origin/"source").write_text("review-only")
            subprocess.run(["git", "-C", str(origin), "-c", "user.name=Test", "-c",
                            "user.email=test@example.org", "commit", "-qam", "candidate"], check=True)
            head = history.tip(origin)
            for provider, namespace in (("github", "pull"), ("gitlab", "merge-requests")):
                subprocess.run(["git", "-C", str(origin), "update-ref", f"refs/{namespace}/8/head", head], check=True)
                with patch.object(sync, "reviewer_token_file", return_value=secret):
                    with sync.scan_checkout(project.checkout, replace(project, provider=provider), "8", head) as source:
                        self.assertEqual((source/"source").read_text(), "review-only")
                    self.assertFalse(source.exists())
            self.assertEqual((project.checkout/"source").read_text(), "two")
