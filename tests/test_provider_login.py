import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import engine, setup


class ContainerLoginTests(unittest.TestCase):
    def test_native_login_then_review_with_persisted_session(self):
        fixture = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
assert not any(k in os.environ for k in ('REVIEW_GITLAB_TOKEN','ANTHROPIC_API_KEY','CODEX_API_KEY'))
home = Path.home()
assert home.name == name
if name == 'codex':
    assert Path(os.environ['CODEX_HOME']) == home / '.codex'
flag = home / 'fixture-session'
args = sys.argv[1:]
if args in (['login','status'], ['auth','status','--json']):
    if name == 'claude':
        print(json.dumps({'loggedIn':True, 'authMethod':'claude.ai' if flag.exists() else 'api_key'}))
        sys.exit(0)
    sys.exit(0 if flag.exists() else 1)
if args in (['login','--device-auth'], ['auth','login','--claudeai']):
    assert Path.cwd() == home
    with flag.open('a') as f: f.write('login\\n')
    sys.exit(0)
assert flag.exists() and Path.cwd() != home and sys.stdin.read() == 'fixture patch'
answer = json.dumps({'review':{'key_issues_to_review':[]}})
if name == 'codex':
    assert args[0]=='exec' and '--ephemeral' in args
    Path(args[args.index('--output-last-message')+1]).write_text(answer)
    print(json.dumps({'type':'turn.completed'}))
else:
    assert args[0]=='-p' and '--safe-mode' in args and '--bare' not in args
    assert args[args.index('--tools')+1]=='' and args[args.index('--max-turns')+1]=='1'
    print(json.dumps(dict(type='result',subtype='success',is_error=False,result=answer)))
"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for provider in ("codex", "claude"):
                with self.subTest(provider=provider):
                    binary = directory / provider
                    binary.write_text(fixture)
                    binary.chmod(0o755)
                    auth = directory / "volume"
                    env = {"PATH": tmp + os.pathsep + os.environ["PATH"], "REVIEW_PROVIDER": provider,
                           "REVIEW_AUTH_MODE": "login", "REVIEW_LOGIN_DIR": str(auth),
                           "REVIEW_GITLAB_TOKEN": "fixture", "CODEX_API_KEY": "fixture",
                           "ANTHROPIC_API_KEY": "fixture"}
                    with patch.dict(os.environ, env, clear=True):
                        with self.assertRaisesRegex(ValueError, "requires an installer terminal"):
                            setup.check_login(directory)
                        with patch("sys.stdin.isatty", return_value=True):
                            setup.check_login(directory, interactive=True)
                        setup.check_login(directory)
                        self.assertEqual(engine.invoke("fixture patch", None, {})["findings"], [])
                        self.assertEqual((auth/provider/"fixture-session").read_text(), "login\n")
                        with patch("sys.stdin.isatty", return_value=True):
                            setup.check_login(directory, interactive=True, force=True)
                        self.assertEqual((auth/provider/"fixture-session").read_text(), "login\nlogin\n")

    def test_saved_mode_without_a_model_secret_and_legacy_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            bundle = state / ".review-setup-1"
            bundle.mkdir()
            (state / "setup").symlink_to(bundle.name)
            key_file = bundle / "old-key"
            key_file.write_text("fixture-key")
            settings = {"REVIEW_PROVIDER": "codex"}
            for mode in ("legacy", "login"):
                if mode == "login":
                    settings["REVIEW_AUTH_MODE"] = mode
                    key_file.unlink()
                (bundle/"settings.json").write_text(json.dumps({"settings":settings, "projects":[],
                    "secrets":{"CODEX_API_KEY":"old-key"}}))
                with patch.dict(os.environ, {}, clear=True):
                    setup.apply_saved_setup(state)
                    if mode == "legacy":
                        self.assertEqual(os.environ["CODEX_API_KEY"], "fixture-key")
                    else:
                        self.assertNotIn("CODEX_API_KEY", os.environ)
                        self.assertEqual(os.environ["REVIEW_AUTH_MODE"], "login")
