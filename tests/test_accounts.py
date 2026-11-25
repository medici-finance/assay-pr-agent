import unittest
from unittest.mock import patch

import httpx

from cursor_pr import accounts
from cursor_pr.config import project_from


def serve(status, payload):
    """Route accounts.httpx.get through a mock transport; return the recorded requests."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    patcher = patch.object(accounts.httpx, "get",
                           lambda url, **kwargs: client.get(url, **{k: v for k, v in kwargs.items() if k != "verify"}))
    patcher.start()
    return calls, patcher.stop


class AccountTests(unittest.TestCase):
    def setUp(self):
        accounts.cache.clear()

    def test_requester_tokens_are_ids_or_usernames(self):
        self.assertEqual(accounts.requester_id(" 12 "), 12)
        self.assertEqual(accounts.requester_id("alice"), "alice")
        for bad in ("", "0", "007", "alice bob", "-alice", "alice,bob", "alice:1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                accounts.requester_id(bad)

    def test_prompt_reasks_after_an_unknown_github_username(self):
        from cursor_pr.config import prompt_config
        answers = ["github", "https://github.com/org/repo", "/repos/x", "ghost,27", "alice,27", "", "n"]
        with patch("builtins.input", side_effect=answers), patch("cursor_pr.config.notice") as notice, \
                patch.object(accounts, "github_id", side_effect=[ValueError("GitHub has no user account named 'ghost'"), 13]):
            rows = prompt_config()["projects"]
        self.assertEqual(rows[0]["requester_ids"], [13, 27])
        self.assertEqual([str(c.args[0]) for c in notice.call_args_list],
                         ["GitHub has no user account named 'ghost'", "Resolved GitHub user alice to account 13."])

    def test_runtime_config_still_requires_numeric_ids(self):
        row = dict(provider="github", url="https://github.com/org/repo", requester_ids=["alice"], managed=True)
        with self.assertRaisesRegex(ValueError, "positive numeric IDs"):
            project_from(row)

    def test_github_lookup_accepts_human_accounts_and_caches(self):
        calls, stop = serve(200, {"id": 1024025, "type": "User"})
        self.addCleanup(stop)
        self.assertEqual(accounts.github_id("torvalds"), 1024025)
        self.assertEqual(accounts.github_id("Torvalds"), 1024025)
        self.assertEqual([(c.url.host, c.url.path) for c in calls], [("api.github.com", "/users/torvalds")])

    def test_github_lookup_rejects_unknown_or_non_user_accounts(self):
        for payload in ({"message": "Not Found"}, {"id": 1, "type": "Organization"}, {"id": "1", "type": "User"}, [1]):
            with self.subTest(payload=payload):
                accounts.cache.clear()
                _, stop = serve(404 if "message" in payload else 200, payload)
                self.addCleanup(stop)
                with self.assertRaisesRegex(ValueError, "no user account named 'ghost'"):
                    accounts.github_id("ghost")

    def test_gitlab_lookup_needs_an_exact_visible_match(self):
        calls, stop = serve(200, [{"id": 7, "username": "alice"}])
        self.addCleanup(stop)
        self.assertEqual(accounts.gitlab_id("Alice", "https://gitlab.example", "pat"), 7)
        self.assertEqual(calls[0].url.path, "/api/v4/users")
        self.assertEqual(calls[0].url.params["username"], "Alice")
        self.assertEqual(calls[0].headers["PRIVATE-TOKEN"], "pat")
        for payload in ([], [{"id": 7, "username": "alicia"}], [{"id": 7, "username": "alice"}, {"id": 8, "username": "Alice"}]):
            with self.subTest(payload=payload):
                accounts.cache.clear()
                _, stop = serve(200, payload)
                self.addCleanup(stop)
                with self.assertRaisesRegex(ValueError, "no user named 'alice'"):
                    accounts.gitlab_id("alice", "https://gitlab.example", "pat")

    def test_resolve_leaves_gitlab_names_pending_until_a_token_exists(self):
        _, stop = serve(200, {"id": 1024025, "type": "User"})
        self.addCleanup(stop)
        seen = []
        rows = [dict(provider="github", url="https://github.com/org/repo", requester_ids=["torvalds", 5]),
                dict(provider="gitlab", url="https://gitlab.com/group/proj", requester_ids=["alice"])]
        first = accounts.resolve(rows, notice=seen.append)
        self.assertEqual([row["requester_ids"] for row in first], [[1024025, 5], ["alice"]])
        self.assertEqual(accounts.pending(first), ["alice"])
        self.assertEqual(seen, ["Resolved GitHub user torvalds to account 1024025."])
        self.assertEqual(rows[0]["requester_ids"], ["torvalds", 5])
        with patch.object(accounts, "gitlab_id", return_value=7) as lookup:
            second = accounts.resolve(first, token="pat", notice=seen.append)
        lookup.assert_called_once_with("alice", "https://gitlab.com", "pat", verify=True)
        self.assertEqual(second[1]["requester_ids"], [7])
        self.assertEqual(accounts.pending(second), [])
        self.assertEqual(seen[-1], "Resolved GitLab user alice to account 7.")

    def test_operator_username_resolves_on_the_configured_forge(self):
        _, stop = serve(200, {"id": 1024025, "type": "User"})
        self.addCleanup(stop)
        self.assertEqual(accounts.resolve_operator("alice:123", {"github", "gitlab"}), "alice:123")
        self.assertEqual(accounts.resolve_operator("torvalds", {"github"}), "torvalds:1024025")
        self.assertEqual(accounts.resolve_operator("alice", {"gitlab"}), "alice")
        with patch.object(accounts, "gitlab_id", return_value=7) as lookup:
            self.assertEqual(accounts.resolve_operator("alice", {"gitlab"}, base="https://gitlab.com", token="pat"), "alice:7")
        lookup.assert_called_once_with("alice", "https://gitlab.com", "pat", verify=True)
        with self.assertRaisesRegex(ValueError, "username:numeric-id"):
            accounts.resolve_operator("alice", {"github", "gitlab"})
        for bad in ("bad name", "alice:", ":123", "alice:0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                accounts.resolve_operator(bad, {"github"})
