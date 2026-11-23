"""Recognize an explicit review request in an authenticated forge payload."""
import hashlib
import hmac


def authenticated(project, headers, raw):
    if project.provider == "github":
        expected = b"sha256=" + hmac.new(project.secret, raw, hashlib.sha256).hexdigest().encode()
        supplied = headers.get("x-hub-signature-256", "")
    else:
        expected = project.secret
        supplied = headers.get("x-gitlab-token", "")
    return hmac.compare_digest(expected, supplied.encode())


def command(project, event, data):
    if project.provider == "github":
        if event != "issue_comment" or data.get("action") != "created":
            return None
        repo, issue, note = data["repository"], data["issue"], data["comment"]
        user = note["user"]
        if (repo["html_url"] != project.url or not isinstance(issue.get("pull_request"), dict)
                or issue.get("state") != "open" or user.get("type") != "User"
                or note.get("performed_via_github_app") or data["sender"].get("type") != "User"
                or user["id"] != data["sender"]["id"]):
            return None
        body, number = note["body"], issue["number"]
    else:
        if event != "Note Hook" or data.get("object_kind") != "note":
            return None
        note = data["object_attributes"]
        if (note.get("action") != "create" or note.get("noteable_type") != "MergeRequest"
                or note.get("system") is not False or note.get("internal")):
            return None
        repo, mr, user = data["project"], data["merge_request"], data["user"]
        if (repo["web_url"] != project.url or note["project_id"] != repo["id"]
                or mr["target_project_id"] != repo["id"] or mr.get("state") != "opened"
                or user.get("bot") or user["id"] != note["author_id"]):
            return None
        body, number = note["note"], mr["iid"]
    if (type(user["id"]) is not int or user["id"] not in project.requesters
            or type(number) is not int or number <= 0 or type(note["id"]) is not int
            or note["id"] <= 0 or body.strip() != "/review"):
        return None
    return number, note["id"]
