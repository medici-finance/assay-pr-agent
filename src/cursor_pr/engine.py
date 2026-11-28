"""One bounded Cursor request using the published PR-Agent review rubric."""
from bisect import bisect_left
from collections import defaultdict, deque
import fcntl
import hashlib
from importlib.metadata import distribution
from itertools import islice, product
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
import unicodedata

from jinja2 import Environment
from markdown_it import MarkdownIt
import yaml

from .source import TRUNCATED, ReviewError, hunks
from .login import auth_mode, login_directory, login_env
from .setup import state_dir
from .config import DEFAULT_MAX_FINDINGS
from .providers import KEYS, LABELS, provider

VERSION = "2"
MAX_FINDINGS = DEFAULT_MAX_FINDINGS
EVIDENCE_WORK_LIMIT = 250_000
DROPPED_PATH_CHARS = 32_768
REJECTION_DETAILS = 5
LINE_ENDING = re.compile(r"\r\n?|\n")
LF_LINE_ENDING = re.compile(r"\r?\n")
LOCKS = {"package-lock.json", "pnpm-lock.yaml", "go.sum", "npm-shrinkwrap.json"}


def rank(path):
    """Order in which whole files leave an oversized prompt: the least reviewable first."""
    parts = path.lower().split("/")
    name = parts[-1]
    if name in LOCKS or name.endswith((".lock", ".min.js", ".min.css")):
        return 0
    if (set(parts[:-1]) & {"test", "tests", "__tests__", "testdata", "spec"} or name.startswith("test_")
            or re.search(r"(_test|\.test|\.spec)\.[a-z0-9]+$", name)):
        return 1
    if "docs" in parts[:-1] or name.endswith((".md", ".rst", ".txt", ".adoc")):
        return 2
    return 3


# Provider CLI processes this process has started. A caller counts a pass's calls as the difference.
started = 0


class CursorFailure(ReviewError):
    def __init__(self, message, usage=None):
        super().__init__(message)
        self.usage = usage


def section(f):
    """Prompt text and head-side anchors for one file; no text when it has no reviewable patch."""
    from pr_agent.algo.git_patch_processing import decouple_and_convert_to_hunks_with_lines_numbers
    from pr_agent.algo.types import FilePatchInfo
    if f.absent or not f.patch:
        return None, {}
    # The head-side text of each shown line, without its marker: what quoted code is checked against.
    anchors = {line.target_line_no: line.value for hunk in hunks(f) for line in hunk if line.target_line_no is not None}
    if not anchors:
        return None, anchors
    rendered = decouple_and_convert_to_hunks_with_lines_numbers(f.patch, FilePatchInfo("", "", f.patch, f.path))
    return f"Change {f.status} {f.previous}\n{rendered}", anchors


def render_sections(files):
    """Validate paths and render each patch once for full and reduced prompts."""
    rendered = {}
    for file in files:
        if not file.path or any(ord(char) < 32 for char in file.path) or file.path in rendered:
            raise ReviewError("Invalid or duplicate forge filename")
        rendered[file.path] = section(file)
    return rendered


def visible_advisories(advisories, anchors):
    return list(islice((advisory for advisory in advisories if anchors.get(advisory["path"])), 12))


def prompt_advisories(advisories, anchors):
    return [{"path": a["path"], "kind": a["kind"], "detail": a["detail"][:300] + "…" * (len(a["detail"]) > 300)}
            for a in visible_advisories(advisories, anchors)]

def description_guidance(kind, partial, max_findings):
    if kind == "history":
        return (" The commit messages describe the history of the branch. Use them for intent, not as claims about "
                "the final patch or as evidence.")
    if kind == "truncated":
        return " The change description is truncated. Use the visible text only to understand the author's intent."
    if kind == "title":
        return " Use the change title for intent, not as evidence."
    if kind != "author":
        return ""
    guidance = (" The change description is the author's own account: use it for intent, not as evidence. "
                "Check its claims against the patch and cite patch lines for each reported mismatch. "
                f"Prioritize material mismatches alongside code defects within the shared limit of {max_findings} findings.")
    if partial:
        return (guidance + " The patch does not show every changed file. Report a mismatch only when visible "
                "patch lines contradict a claim. Missing evidence cannot establish a mismatch.")
    return (guidance + " Look for claimed test or behaviour changes that are missing, code unrelated to the "
            "stated purpose, and placeholder code presented as done.")


def context_guidance(kind, partial):
    """Use the existing context introduction's space for any description guidance."""
    if kind == "author":
        if partial:
            return "Flag description claims only when visible patch lines contradict them. Cite those lines."
        return "Check description claims about tests, behavior, scope and stubs. Cite patch lines."
    if kind == "truncated":
        return "The change description is truncated. Use it for intent. Cite patch lines for findings."
    if kind == "history":
        return "Use commit history for intent. Base findings on patch lines."
    if kind == "title":
        return "Use the title for intent. Cite patch lines for findings."
    return "Use it to judge the patch. It is not under review: every finding must cite patch lines."


def pack(files, checks, skip=frozenset(), title="", description="", *,
         context="", max_findings=MAX_FINDINGS, focus="", advisories=(), rendered=None, scoped=False,
         commit_messages=False, description_truncated=False, context_description=""):
    """Build the prompt. Paths in skip are left out to fit the budget and get no anchors.

    Metadata and context outside the patch can guide the review. Findings must still cite patch lines.
    """
    template = distribution("pr-agent").locate_file("pr_agent/settings/pr_reviewer_prompts.toml")
    with template.open("rb") as stream:
        rubric = tomllib.load(stream)["pr_review_prompt"]["system"]
    instructions = Environment().from_string(rubric).render(
        num_max_findings=max_findings,
        diff_hunk_format="New hunks are numbered at the head; old hunks show the former code.",
    )
    # Extend the finding schema in the pinned PR-Agent prompt.
    review_model = "\n\nclass Review(BaseModel):"
    instructions = instructions.replace(
        review_model,
        '\n    verification_status: Literal["unverified"] | None = None  # Set when attribution evidence is incomplete.'
        '\n    relevant_code: str | None = None  # Whole lines copied from the cited patch.'
        + review_model,
        1,
    )
    patches = render_sections(files).items() if rendered is None else ((f.path, rendered[f.path]) for f in files)
    sections, anchors, omitted = [], {}, []
    for path, (text, lines) in patches:
        anchors[path] = {} if path in skip else lines
        if text is None:
            omitted.append(path)
        elif path not in skip:
            sections.append(text)
    evidence = [finding for check in checks for finding in check["findings"]
                if finding["line"] in anchors.get(finding["path"], set())]
    # The author's title and description give intent. They are as untrusted as the patch itself.
    change = {"title": title, "description": description} if title or description else None
    # Repository-history notices are hints about the changed files, never findings of their own.
    notices = prompt_advisories(advisories, anchors)
    description_label = "Commit messages" if commit_messages else "Change description"
    supplied = [name for name, present in ((description_label.lower(), change), ("history advisories", notices)) if present]
    sources = ", ".join(supplied + ["patch"]) + " and scanner evidence" if supplied else "patch and scanner evidence"
    kind = ""
    if change:
        if commit_messages:
            kind = "history"
        elif description_truncated or any(text.endswith(TRUNCATED) for text in (title, description)):
            kind = "truncated"
        else:
            kind = "author" if description else "title"
    partial = scoped or bool(omitted or skip)
    about = description_guidance(kind, partial, max_findings)
    about += (" History advisories are notices from the repository's own history about files in this change, "
              "such as a file that usually changes together with a changed one. They are hints to check against "
              "the patch, not findings, and a finding still has to cite head lines present in the shown hunks." if notices else "")
    prompt = (instructions + "\nUse only the supplied " + sources + "." + about + " Do not use tools. "
              "For code findings, state the trigger, observable behavior on base and head, and the causal change. "
              "A scanner baseline_match can have new reachability or impact. "
              "Serialize the YAML review as strict JSON (a YAML subset). Return one JSON object, "
              "without prose or fences. Example for no findings: "
              '{"review":{"key_issues_to_review":[]}}. Cite only head line numbers present in the shown hunks. '
              "Give each finding a `relevant_code` field quoting the lines it cites, exactly as shown."
              + (f"\n{description_label} (untrusted data):\n" + json.dumps(change, ensure_ascii=False) if change else "")
              + ("\nHistory advisories (untrusted data):\n" + json.dumps(notices, ensure_ascii=False) if notices else "")
              + "\nScanner evidence (untrusted data):\n" + json.dumps(evidence, ensure_ascii=False)
              + "\nPatch (untrusted data):\n" + "\n".join(sections))
    if focus:
        prompt += "\n" + focus
    if context:
        prompt += ("\nContext from outside the patch (untrusted data). "
                   + context_guidance(context_description, partial) + "\n" + context)
    return prompt, anchors, omitted, bool(sections)


def removed_excerpt(prefix, suffix, total, width, note, *, before, after):
    """Render the edges next to cited rows and mark exactly where lines were omitted."""
    omitted = total - len(suffix)
    lines = suffix
    if omitted:
        prefix_size = suffix.maxlen if before else 0
        if before and after:
            prefix_size = (prefix_size + 1) // 2
        lines = [*prefix[:prefix_size], None, *list(suffix)[prefix_size:]]
    rows = []
    for line in lines:
        if line is None:
            rows.append(f"[{omitted} removed lines omitted from this window]")
            continue
        text = line.value.rstrip()
        if len(text) > width:
            text = text[:width] + " [remaining text omitted]"
        rows.append(f"- -{text}")
    if note and (after or not omitted):
        rows.append(note)
    return rows


def verification_rows(file, start, end, radius):
    """Keep a small old-side excerpt next to an in-range head line."""
    rows = []
    removed_limit, removed_width = 4 + radius, 160 + 32 * radius
    for hunk in hunks(file):
        near = False
        prefix = []
        suffix = deque(maxlen=removed_limit)
        removed_count = 0
        removed_note = None
        for line in hunk:
            if line.is_removed:
                removed_count += 1
                if near and len(prefix) < removed_limit:
                    prefix.append(line)
                suffix.append(line)
                continue
            number = line.target_line_no
            if number is None and removed_count:
                # The old file's no-newline annotation is part of its removal block.
                removed_note = f"- {line.line_type}{line.value.rstrip()}"
                continue
            next_near = near if number is None else start <= number <= end
            if removed_count and (near or next_near or line.is_context):
                if near or next_near:
                    rows.extend(removed_excerpt(prefix, suffix, removed_count, removed_width, removed_note,
                                                before=near, after=next_near))
                prefix.clear()
                suffix.clear()
                removed_count = 0
                removed_note = None
            near = next_near
            if near:
                rows.append(f"{number if number is not None else '-'} {line.line_type}{line.value.rstrip()}")
        if near and removed_count:
            rows.extend(removed_excerpt(prefix, suffix, removed_count, removed_width, removed_note,
                                        before=True, after=False))
    return rows


def verification(findings, files, radius=15, *, comparison=None, source_radius=None):
    """A second, small prompt: the findings and the patch lines they cite, to be filtered."""
    by_path = {f.path: f for f in files}
    shown = []
    for finding in findings:
        start, end = finding["line"] - radius, finding["end_line"] + radius
        file = by_path[finding["path"]]
        rows = verification_rows(file, start, end, radius)
        if comparison is not None:
            window = radius if source_radius is None else source_radius
            rows += comparison(file, finding["line"], finding["end_line"], window)
        shown.append(f"## {finding['path']} around lines {finding['line']}-{finding['end_line']}\n" + "\n".join(rows))
    listed = [{"relevant_file": f["path"], "start_line": f["line"], "end_line": f["end_line"],
               "issue_header": f["title"], "issue_content": f["message"]} for f in findings]
    return ("You are checking the findings of a code review against the code they cite. Drop a finding when "
            "the evidence disproves it or the code shown is sufficient to judge it and does not support it. "
            "For code findings compare the same concrete scenario on base and head, including reachability "
            "and impact. Drop a finding when the patch leaves that scenario's reachability, behavior and "
            "impact equivalent. A changed line can preserve behavior and a changed caller can expose an existing bug. "
            "If you need context outside the supplied code to decide, keep the finding unchanged and unverified. "
            "Do not add findings and do not use tools. "
            'Return one JSON object, without prose or fences, of the form {"review":{"key_issues_to_review":[...]}} '
            "holding only the findings you keep, each copied unchanged except for verification_status. "
            "Set verification_status to 'confirmed' if the evidence establishes attribution, or "
            "'unverified' if the evidence is incomplete."
            "\nFindings (untrusted data):\n" + json.dumps(listed, ensure_ascii=False)
            + "\nCited code (untrusted data; a leading number is the head line, - marks a removed line):\n"
            + "\n\n".join(shown))


def fit_verification(findings, files, limit, comparison):
    """Fit the widest cited patch, then spend any remaining budget on source excerpts."""
    for radius in (15, 8, 3, 0):
        patch = verification(findings, files, radius)
        patch_size = tokens(patch)
        if patch_size > limit:
            continue
        for source_radius in (15, 8, 3):
            prompt = verification(findings, files, radius, comparison=comparison, source_radius=source_radius)
            size = tokens(prompt)
            if size <= limit:
                return prompt, size
        return patch, patch_size
    return None, 0


def finding_key(finding):
    return tuple(finding[key] for key in ("path", "line", "end_line", "title", "message"))


def kept(findings, answer):
    """Accept only unchanged findings from the original review, in their original order."""
    if answer.get("dropped"):
        raise ReviewError("Verifier returned invalid findings")
    remaining = {finding_key(finding): finding.get("verification_status") for finding in answer["findings"]}
    if len(remaining) != len(answer["findings"]):
        raise ReviewError("Verifier repeated a finding")
    verified = []
    for finding in findings:
        key = finding_key(finding)
        if key in remaining:
            status = remaining.pop(key)
            verified.append(dict(finding, verification_status="confirmed" if status == "confirmed" else "unverified"))
    if remaining:
        raise ReviewError("Verifier returned findings outside the original review")
    return verified


def merge_findings(first, second):
    """Keep distinct findings in pass order, retaining any verified quote."""
    merged = {}
    for finding in (*first, *second):
        key = finding_key(finding)
        if key not in merged:
            merged[key] = finding
        elif finding.get("evidence") == "verified" and merged[key].get("evidence") != "verified":
            merged[key] = dict(merged[key], evidence="verified")
    return list(merged.values())


def tokens(prompt):
    from pr_agent.algo.token_handler import TokenEncoder
    return len(TokenEncoder.get_token_encoder("gpt-4o").encode(prompt, disallowed_special=()))


YAML_TAG = "tag:yaml.org,2002:"
YAML_VALUE_ERRORS = (ValueError, TypeError, LookupError, AttributeError, OverflowError, yaml.YAMLError)
FINDING_TEXT_FIELDS = ("relevant_file", "issue_header", "issue_content", "relevant_code")
FINDING_LINE_FIELDS = ("start_line", "end_line")
ANSWER_FIELDS = frozenset(("review", "key_issues_to_review", "verification_status",
                           *FINDING_TEXT_FIELDS, *FINDING_LINE_FIELDS))


def answer_mapping_fields(node, loader):
    if not isinstance(node, yaml.MappingNode) or node.tag != YAML_TAG + "map":
        raise ReviewError("Expected a YAML mapping in the review response")
    parents, explicit = [], {}
    for key, value in node.value:
        if not isinstance(key, yaml.ScalarNode):
            raise ReviewError("Expected a scalar key in the review response")
        if key.tag == YAML_TAG + "merge":
            if isinstance(value, yaml.SequenceNode) and value.tag == YAML_TAG + "seq":
                parents.extend(reversed(value.value))
            else:
                parents.append(value)
        elif key.tag == YAML_TAG + "str":
            if key.value in ANSWER_FIELDS:
                explicit[key.value] = value
        elif key.tag != YAML_TAG + "value":
            # Unknown keys need not be retained, but must still be valid YAML keys.
            try:
                hash(loader.construct_object(key))
            except YAML_VALUE_ERRORS:
                raise ReviewError("Invalid YAML mapping key in the review response") from None
    return parents, explicit


def answer_mapping(node, mappings, loader):
    # A chain of YAML aliases can be deep even when the text itself is not nested.
    pending, active = [(node, None)], set()
    try:
        while pending:
            current, fields = pending.pop()
            if current in mappings:
                if mappings[current] is None:
                    raise ReviewError("Invalid YAML merge in the review response")
                continue
            if fields is None:
                if current in active:
                    raise ReviewError("Cyclic YAML merge in the review response")
                active.add(current)
                fields = answer_mapping_fields(current, loader)
                pending.append((current, fields))
                pending.extend((parent, None) for parent in reversed(fields[0]))
            else:
                parents, explicit = fields
                merged = {}
                for parent in parents:
                    merged.update(mappings[parent])
                merged.update(explicit)
                mappings[current] = merged
                active.remove(current)
    except ReviewError:
        # Completed ancestors remain reusable; only mappings that depend on the failure are invalid.
        mappings.update(dict.fromkeys(active))
        raise
    return mappings[node]


def normalized_text(text, *, strip=True):
    # PyYAML leaves escaped UTF-16 surrogate pairs as two code units.
    try:
        decoded = text.encode("utf-16", "surrogatepass").decode("utf-16")
        return decoded.strip() if strip else decoded
    except UnicodeDecodeError:
        return None


def trim_blank_edges(text):
    """Remove blank lines at the edges while preserving indentation and line contents."""
    leading = len(text) - len(text.lstrip(" \t\r\n"))
    if leading == len(text):
        return ""
    start = max(text.rfind("\n", 0, leading), text.rfind("\r", 0, leading)) + 1
    trailing = len(text.rstrip(" \t\r\n"))
    line_end = LINE_ENDING.search(text, trailing)
    end = line_end.start() if line_end else len(text)
    return text[start:end]


def normalized_message(text):
    """Preserve Markdown whitespace in the message used for evidence and reports."""
    if "```" in text or "~~~" in text:
        return normalized_text(trim_blank_edges(text), strip=False)
    return normalized_text(text)


def answer_finding(fields, loader, scalars):
    item = {}
    if (status := fields.get("verification_status")) is not None:
        if not isinstance(status, yaml.ScalarNode):
            return None
        if status.tag != YAML_TAG + "null":
            if status.tag != YAML_TAG + "str" or status.value not in ("confirmed", "unverified"):
                return None
            item["verification_status"] = status.value
    quote = fields.get("relevant_code")
    if quote is not None and not (isinstance(quote, yaml.ScalarNode)
                                  and quote.tag == YAML_TAG + "null"
                                  and quote.value in ("", "~", "null", "Null", "NULL")):
        item["relevant_code"] = None  # Present but malformed unless decoded below.
    for name in FINDING_TEXT_FIELDS:
        value = fields.get(name)
        if isinstance(value, yaml.ScalarNode) and value.tag == YAML_TAG + "str":
            mode = name if name in ("issue_content", "relevant_code") else None
            key = (value, mode)
            if key not in scalars:
                scalars[key] = (normalized_message(value.value) if mode == "issue_content"
                                else normalized_text(value.value, strip=mode is None))
            item[name] = scalars[key]
    for name in FINDING_LINE_FIELDS:
        value = fields.get(name)
        if isinstance(value, yaml.ScalarNode) and value.tag == YAML_TAG + "int":
            if value not in scalars:
                try:
                    scalars[value] = loader.construct_yaml_int(value)
                except (ValueError, IndexError):
                    scalars[value] = None
            item[name] = scalars[value]
    return item


def answer_findings(answer, anchors, *, explain_dropped=False):
    """Read the flat finding schema without constructing unrelated YAML values."""
    loader = yaml.SafeLoader(answer)
    try:
        mappings = {}
        document = answer_mapping(loader.get_single_node(), mappings, loader)
        issues = answer_mapping(document.get("review"), mappings, loader).get("key_issues_to_review")
        if not isinstance(issues, yaml.SequenceNode) or issues.tag != YAML_TAG + "seq":
            raise ReviewError("Invalid Cursor finding list")
        entries, scalars = {}, {}
        evidence_budget = EvidenceBudget()
        remaining = REJECTION_DETAILS if explain_dropped else 0
        for node in issues.value:
            if node not in entries:
                try:
                    fields = answer_mapping(node, mappings, loader)
                except ReviewError:
                    item = None
                else:
                    item = answer_finding(fields, loader, scalars)
                valid, dropped = finding(item, anchors, normalized=True, evidence_budget=evidence_budget)
                if dropped and remaining:
                    dropped = explain_rejection(item, anchors, dropped)
                    remaining -= 1
                entries[node] = valid, dropped
            yield entries[node]
    except RecursionError:
        raise ReviewError("YAML nesting in the review response is too deep") from None
    finally:
        loader.dispose()


def model_text(raw):
    """The model's text and usage from one successful terminal result, whatever the text is asked to hold."""
    value = json.loads(raw)
    if value.get("type") != "result" or value.get("subtype") != "success" or value.get("is_error") is not False:
        raise ReviewError("Cursor did not return a successful terminal result")
    text = value["result"].strip()
    # In Auto mode the CLI can prepend its own routing notice to the model text.
    text = re.sub(r"^> Auto routed to [^\r\n]+\r?\n\s*\n", "", text, count=1)
    fenced = re.fullmatch(r"\x60{3}(?:ya?ml|json)?\s*\n(.*?)\n\x60{3}", text, re.S)
    if fenced:
        text = fenced.group(1)
    if len(text) > 32_768:
        raise ReviewError("Cursor answer exceeds the review size limit")
    usage = value.get("usage")
    return text, usage if isinstance(usage, dict) else None


def parse_answer(raw, anchors, limit=MAX_FINDINGS, *, verification=False, explain_dropped=False):
    answer, usage = model_text(raw)
    if verification:
        # A verdict can remove original findings, so validate its entire YAML document.
        try:
            issues = yaml.safe_load(answer)["review"]["key_issues_to_review"]
        except RecursionError:
            raise ReviewError("YAML nesting in the review response is too deep") from None
        except YAML_VALUE_ERRORS:
            raise ReviewError("Invalid YAML verifier response") from None
        if not isinstance(issues, list) or len(issues) > limit:
            raise ReviewError("Invalid verifier finding list")
        checked = (finding(issue, anchors, verification=True) for issue in issues)
    else:
        checked = answer_findings(answer, anchors, explain_dropped=explain_dropped)
    # A bad finding costs the others nothing: the call is already paid for and there is no retry.
    clean, dropped = [], []
    path_budget = DROPPED_PATH_CHARS
    for valid, why in checked:
        if not why and len(clean) < limit:
            clean.append(dict(valid))
            continue
        if not why:
            why = {"path": valid["path"], "line": valid["line"], "reason": "over the finding limit"}
            if explain_dropped:
                why["end_line"] = valid["end_line"]
        path_size = len(why["path"])
        if path_size > path_budget:
            why = dict(why, path="")
        else:
            path_budget -= path_size
        dropped.append(why)
    return {"findings": clean, "dropped": dropped, "cursor_usage": usage}


def finding(issue, anchors, *, normalized=False, verification=False, evidence_budget=None):
    """One validated finding, or the record of why it was dropped. A reason is fixed text, never the model's."""
    dropped = {"path": "", "line": None, "reason": "malformed finding"}
    if not isinstance(issue, dict):
        return None, dropped
    path, start, end = issue.get("relevant_file"), issue.get("start_line"), issue.get("end_line")
    header, body = issue.get("issue_header"), issue.get("issue_content")
    if isinstance(path, str):
        if not normalized:
            path = normalized_text(path)
        if path is not None and path in anchors:
            dropped["path"] = path
    if type(start) is int and 0 < start <= 2**31 - 1:
        dropped["line"] = start
    if not isinstance(path, str) or type(start) is not int or type(end) is not int:
        return None, dropped
    lines = anchors.get(path, set())
    if (start > end or start not in lines or end not in lines or end - start >= len(lines)
            or not all(line in lines for line in range(start, end + 1))):
        return None, dict(dropped, reason="cited line outside the reviewed patch")
    if not isinstance(header, str) or not isinstance(body, str):
        return None, dropped
    if not normalized:
        header, body = normalized_text(header), normalized_message(body)
        if header is None or body is None:
            return None, dropped
    if not header or not body:
        return None, dict(dropped, reason="empty finding")
    valid = {"path": path, "line": start, "end_line": end, "title": header, "message": body}
    status = issue.get("verification_status")
    if status is not None:
        if status not in ("confirmed", "unverified"):
            return None, dropped
        valid["verification_status"] = status
    if not verification:
        code = issue.get("relevant_code")
        if code is None and "relevant_code" in issue:
            return None, dict(dropped, reason="malformed quoted code")
        evidence = quoted_evidence(code, body, lines, start, end,
                                   evidence_budget if evidence_budget is not None else EvidenceBudget())
        if evidence is None:
            return None, dict(dropped, reason="quoted code does not match the cited lines")
        valid["evidence"] = evidence
    return valid, None


class EvidenceBudget:
    """Bound the quote work for one model answer, including repeated rows."""

    def __init__(self):
        self.remaining = EVIDENCE_WORK_LIMIT

    def spend(self, units):
        self.remaining -= units
        return self.remaining >= 0


def quoted_evidence(code, message, lines, start, end, budget):
    """Return an evidence status, or None when the explicit quote contradicts the patch."""
    asked = isinstance(code, str)
    if not budget.spend(len(code if asked else message)):
        return "unverified"
    quote = code if asked else fenced_quote(message)
    if not quote or not isinstance(lines, dict) or not budget.spend(end - start + 1):
        return "unverified"
    cited = [(line, lines[line].rstrip("\r\n")) for line in range(start, end + 1)]
    status = quote_coverage(quote, cited, budget)
    if not asked:
        # A fenced block in the message may be a suggested fix.
        return status or "unverified"
    if status == "verified" or ("```" not in code and "~~~" not in code):
        return status
    fence_modes = (True, False) if can_quote_literal_cr(code, cited) else (False,)
    for preserve_cr in fence_modes:
        if not budget.spend(len(code) + len(cited)):
            return "unverified"
        wrapped = fenced_quote(code, whole=True, preserve_cr=preserve_cr)
        if wrapped is not None:
            status = quote_coverage(wrapped, cited, budget) or status
            if status == "verified":
                break
    return status


def can_quote_literal_cr(quote, cited):
    return "\r" in quote and any("\r" in line for _, line in cited)


def quote_coverage(quote, cited, budget):
    separators = (LF_LINE_ENDING, LINE_ENDING) if can_quote_literal_cr(quote, cited) else (LINE_ENDING,)
    literals = set()
    if MARKER.search(quote):
        literals = {line.lstrip(" \t") for _, line in cited if MARKER.fullmatch(line.strip(" \t"))}
    status = None
    for separator in separators:
        if separator is not separators[0] and not budget.spend(len(quote) + len(cited)):
            return "unverified"
        parts = checkable(quote, separator=separator, literal_markers=literals)
        padding = product(marker_padding_offsets(parts), marker_padding_offsets(reversed(parts)))
        for leading, trailing in padding:
            if leading or trailing:
                if not budget.spend(len(parts) + len(cited)):
                    return "unverified"
                if leading + trailing >= len(parts):
                    continue
            status = quote_rows_coverage(parts[leading:len(parts) - trailing], cited, budget) or status
            if status == "verified":
                return status
    return status


def marker_padding_offsets(parts):
    """Try dropping an edge marker with its following blank padding as one unit."""
    yield 0
    marker_seen = False
    for index, part in enumerate(parts):
        if part is ELIDED or (part[1] is not OPTIONAL_MARKER and part[0].strip(" \t")):
            break
        if part[1] is OPTIONAL_MARKER:
            marker_seen = True
        elif marker_seen:
            yield index + 1


def quote_rows_coverage(parts, cited, budget):
    concrete = [part for part in parts if part is not ELIDED]
    if not any(part[0].strip(" \t") for part in concrete):
        return "unverified"
    required = [part for part in concrete if part[1] is not OPTIONAL_MARKER]
    if not quoted_in_order(required, cited):
        return None
    if covers(parts, cited, single_line=len(concrete) == 1, budget=budget):
        return "verified"
    for quote_margin, source_margin in candidate_margins(concrete, cited, budget):
        if covers(parts, cited, quote_margin=quote_margin, source_margin=source_margin, budget=budget):
            return "verified"
    return "unverified"


MARKDOWN = MarkdownIt("commonmark").disable("inline")
LITERAL_CR_MARKDOWN = MarkdownIt("commonmark").disable(["inline", "normalize"])
# A quoted line as the patch shows it: indented, numbered, and marked as added.
SHOWN = re.compile(r"([1-9]\d*) (?:\+| )(.*)")
# An explicit elision such as "..." or "# …": it stands for cited lines the quote leaves out.
ELISION = re.compile(r"(?:(?:#|//)[ \t]*)?(?:\.{3,}|…)")
# A hunk marker may come from the patch display or from an actual source row.
MARKER = re.compile(r"__(?:new|old) hunk__")
ELIDED = object()
OPTIONAL_MARKER = object()
# These printable glyphs are blank, including the Hangul fillers and blank Braille cell.
BLANK_GLYPHS = frozenset("\u115f\u1160\u2800\u3164\uffa0")


def fenced_quote(message, *, whole=False, preserve_cr=False):
    if "\x00" in message:
        # CommonMark would silently replace NUL in the evidence text.
        return None
    if preserve_cr:
        # Match LF or CRLF-delimited source, letting the block parser keep CR inside code rows.
        message = message.replace("\r\n", "\n")
    parser = LITERAL_CR_MARKDOWN if preserve_cr else MARKDOWN
    tokens = parser.parse(message)
    fence = next((token for token in tokens if token.type == "fence"), None)
    if fence is None:
        return None
    if whole:
        if len(tokens) != 1:
            return None
        # The parser can consume link definitions without producing any tokens.
        start, end = fence.map
        separator = LF_LINE_ENDING if preserve_cr else LINE_ENDING
        lines = separator.split(message)
        if any(line.strip(" \t") for line in lines[:start] + lines[end:]):
            return None
    return fence.content


def checkable(quote, *, separator=LINE_ENDING, literal_markers=()):
    """The quote's lines in order: each as its raw and numbered forms, or ELIDED for an explicit elision.

    Keep hunk markers that might match source as optional rows.
    """
    parts = []
    for raw in separator.split(quote):
        shown = SHOWN.fullmatch(raw.lstrip(" \t"))
        if shown:
            parts.append((raw, shown[1], shown[2]))
            continue
        standalone = raw.strip(" \t")
        if not standalone and not parts:
            continue
        if MARKER.fullmatch(standalone):
            if raw.lstrip(" \t") in literal_markers:
                parts.append((raw, OPTIONAL_MARKER, raw))
        elif ELISION.fullmatch(standalone):
            if parts[-1:] != [ELIDED]:
                parts.append(ELIDED)
        else:
            parts.append((raw, None, raw))
    while parts and parts[-1] is not ELIDED and not parts[-1][0].strip(" \t"):
        parts.pop()
    return parts


def numbered_match(part, index, line):
    return part[1] == str(index) and part[2] == line


def quoted_in_order(parts, cited):
    """Check for a matching ordered subset before testing complete coverage."""
    return quoted_prefix(parts, cited) == len(parts)


def quoted_prefix(parts, cited):
    """Count the leading quote rows that have an ordered match in the cited lines."""
    remaining = iter(cited)
    for index, part in enumerate(parts):
        text = part[0].lstrip(" \t")
        if not any(numbered_match(part, line_no, line) or text == line.lstrip(" \t") for line_no, line in remaining):
            return index
    return len(parts)


def explain_rejection(issue, anchors, dropped):
    """Return bounded line references and comparison results without copying model text."""
    if not isinstance(issue, dict):
        return dropped
    start, end = dropped["line"], issue.get("end_line")
    if start is not None and type(end) is int and 0 < end <= 2**31 - 1:
        dropped = dict(dropped, end_line=end)
    if dropped["reason"] != "quoted code does not match the cited lines":
        return dropped
    quote = issue["relevant_code"]
    if len(quote) > 8_000 or end - start >= 200:
        return dropped
    lines = anchors[dropped["path"]]
    cited = [(line, lines[line].rstrip("\r\n")) for line in range(start, end + 1)]
    if "```" in quote or "~~~" in quote:
        quote = fenced_quote(quote, whole=True) or quote
    # Keep the snippet's row numbers even when its elisions and hunk markers are skipped.
    numbered = [(number, part) for number, line in enumerate(LINE_ENDING.split(quote), 1)
                for part in checkable(line) if part is not ELIDED]
    parts = [part for _, part in numbered]
    kept = quoted_prefix(parts, cited)
    if kept == len(parts):
        return dropped
    stripped, count = re.subn(r"^[ \t]*\+", "", quote, flags=re.M)
    diff_markers = False
    if count:
        # A diff may put a leading space on its unchanged rows. Require the same validator
        # to verify a candidate after removing markers, including full citation coverage.
        with_context_stripped = re.sub(r"^(?:[ \t]*\+| )", "", quote, flags=re.M)
        diff_markers = any(quoted_evidence(candidate, "", lines, start, end, EvidenceBudget()) == "verified"
                           for candidate in (stripped, with_context_stripped))
    return dict(dropped, unmatched_quote_row=numbered[kept][0], diff_markers=diff_markers)


def candidate_margins(parts, cited, budget):
    """Try uniform shifts from possible matches, then let coverage choose the alignment."""
    if not budget.spend(len(cited) + len(parts)):
        return
    indents, numbered = defaultdict(dict), {}
    for index, line in cited:
        stripped = line.lstrip(" \t")
        indents[stripped][line[:len(line) - len(stripped)]] = None
        numbered[str(index)] = line
    parts = [part for part in parts if part[0]]
    raw = [part for part in parts if part[1] is not OPTIONAL_MARKER and numbered.get(part[1]) != part[2]]
    # Every covering alignment must match this row as raw code. Start with the fewest possible shifts.
    seeds = [min(raw, key=lambda part: len(indents[part[0].lstrip(" \t")]))] if raw else parts
    seen = {("", "")}
    for text, _, _ in seeds:
        stripped = text.lstrip(" \t")
        quote = text[:len(text) - len(stripped)]
        for source in indents[stripped]:
            if not budget.spend(1 + len(quote) + len(source)):
                return
            common = len(os.path.commonprefix((quote[::-1], source[::-1])))
            margins = (quote[:len(quote) - common], source[:len(source) - common])
            if margins not in seen:
                seen.add(margins)
                yield margins


def evidence_key(text, margin, single_line):
    if single_line:
        return text.lstrip(" \t")
    return text.removeprefix(margin) if text.startswith(margin) else None


def evidence_matches(part, index, line, quote_margin, source_margin, single_line):
    if numbered_match(part, index, line):
        return True
    text = evidence_key(part[0], quote_margin, single_line)
    return text is not None and text == evidence_key(line, source_margin, single_line)


def indexed_evidence(cited, source_margin, single_line):
    text_positions, numbered_positions = defaultdict(list), {}
    for pos, (index, line) in enumerate(cited):
        text = evidence_key(line, source_margin, single_line)
        if text is not None:
            text_positions[text].append(pos)
        numbered_positions[str(index)] = pos
    return text_positions, numbered_positions


def isolated_evidence(part, cited, indexed, first, strong_from, quote_margin, single_line):
    """Between elisions, retain only the earliest match with and without substantive evidence."""
    text_positions, numbered_positions = indexed
    positions = text_positions.get(evidence_key(part[0], quote_margin, single_line), ())
    matching = set()
    for start in (first, strong_from):
        offset = bisect_left(positions, start)
        if offset < len(positions):
            matching.add(positions[offset])
    numbered = numbered_positions.get(part[1])
    if numbered is not None and numbered >= first and part[2] == cited[numbered][1]:
        matching.add(numbered)
    return {pos + 1: pos >= strong_from or substantive(cited[pos][1]) for pos in matching}


def covers(parts, cited, *, quote_margin="", source_margin="", single_line=False, budget=None):
    """Find coverage with consistent indentation and a substantive match on the same alignment."""
    if budget is None:
        budget = EvidenceBudget()
    reach = {0: False}  # next cited position -> whether any alignment here matched substantive code
    eliding, indexed = False, None
    for offset, part in enumerate(parts):
        if not reach or not budget.spend(len(reach)):
            return False
        if part is ELIDED:
            eliding = True
            continue
        optional = part[1] is OPTIONAL_MARKER
        if eliding:
            eliding = False
            first = min(reach)
            strong_from = min((pos for pos, strong in reach.items() if strong), default=len(cited) + 1)
            if not optional and offset + 1 < len(parts) and parts[offset + 1] is ELIDED:
                if indexed is None:
                    if not budget.spend(len(cited)):
                        return False
                    indexed = indexed_evidence(cited, source_margin, single_line)
                reach = isolated_evidence(part, cited, indexed, first, strong_from, quote_margin, single_line)
                continue
            if not budget.spend(len(cited) + 1 - first):
                return False
            reach = {pos: pos >= strong_from for pos in range(first, len(cited) + 1)}
        step = reach.copy() if optional else {}
        last = max(reach)
        reachable = strong = False
        for position in range(min(reach), len(cited)):
            if not budget.spend(1):
                return False
            if position in reach:
                reachable = True
                strong = strong or reach[position]
            index, line = cited[position]
            if reachable and evidence_matches(part, index, line, quote_margin, source_margin, single_line):
                step[position + 1] = step.get(position + 1, False) or strong or substantive(line)
            if line.strip(" \t"):
                if position >= last:
                    break
                reachable = strong = False
        reach = step
    if eliding:
        return any(reach.values())
    if not budget.spend(len(cited)):
        return False
    last_nonblank = max((pos for pos, (_, line) in enumerate(cited) if line.strip(" \t")), default=-1)
    return any(strong and pos > last_nonblank for pos, strong in reach.items())


def substantive(content):
    """A line specific enough to identify code: three or more visible characters, one a letter or digit.

    `return x` or `x = 1` counts; `}` or `);` alone does not, since it would match almost any range.
    """
    visible = [char for char in unicodedata.normalize("NFC", content) if char.isprintable() and not char.isspace()
               and char not in BLANK_GLYPHS and unicodedata.category(char) not in ("Mn", "Me")]
    return len(visible) >= 3 and any(char.isalnum() for char in visible)


def invoke(prompt, model, anchors, limit=MAX_FINDINGS, timeout=120, *, verification=False, parse=None,
           explain_dropped=False):
    """One call of the provider CLI. `parse` reads an answer that is not a list of findings."""
    global started
    backend = provider()
    label = LABELS[backend]
    executable = (shutil.which("cursor-agent") or shutil.which("agent")) if backend == "cursor" else shutil.which(backend)
    if not executable:
        raise CursorFailure(f"{label} CLI is not installed")
    # Retain login discovery, but keep unrelated forge and CI secrets out of the child.
    env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR",
           "XDG_CONFIG_HOME", KEYS[backend], "SSL_CERT_FILE", "HTTPS_PROXY", "NO_PROXY")
           if k in os.environ}
    with tempfile.TemporaryDirectory(prefix="cursor-review-") as workspace:
        if backend == "cursor":
            if auth_mode(backend) == "login":
                env = login_env(backend, login_directory(state_dir()))
            elif os.getenv("CURSOR_CONFIG_DIR"):
                env["CURSOR_CONFIG_DIR"] = os.environ["CURSOR_CONFIG_DIR"]
                if env.get("CURSOR_API_KEY"):
                    env["HOME"] = env["CURSOR_CONFIG_DIR"]
            config = Path(workspace, ".cursor")
            config.mkdir()
            (config / "cli.json").write_text(json.dumps({"permissions": {"allow": [], "deny": [
                "Shell(*)", "Read(**)", "Write(**)", "WebFetch(*)", "Mcp(*:*)"]}}))
            argv = [executable, "-p", "--mode=ask", "--output-format", "json", "--trust"]
        else:
            env.update(HOME=workspace, XDG_CONFIG_HOME=workspace)
            use_login = auth_mode(backend) == "login"
            if use_login:
                env = login_env(backend, login_directory(state_dir()))
            elif not env.get(KEYS[backend]):
                raise CursorFailure(f"Set {KEYS[backend]} for {label}")
            if backend == "claude":
                argv = [executable, "-p", "--safe-mode" if use_login else "--bare", "--output-format", "json", "--tools", "",
                        "--strict-mcp-config", "--no-session-persistence", "--max-turns", "1"]
            else:
                if not use_login:
                    env["CODEX_HOME"] = workspace
                argv = [executable, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                        "--ignore-user-config", "--ignore-rules", "--disable", "shell_tool",
                        "--disable", "unified_exec", "--disable", "multi_agent", "--disable", "apps",
                        "-c", 'web_search="disabled"', "-c", 'approval_policy="never"',
                        "--output-last-message", str(Path(workspace, "answer"))]
        if model:
            argv += ["--model", model]
        try:
            if backend == "codex":
                argv.append("-")
            # Every provider reads the prompt on standard input. As an argument it would be
            # limited to 128 KiB on Linux, which a deep review exceeds.
            # Count a started CLI; one that fails to start raises OSError and made no call.
            try:
                response = subprocess.run(argv, input=prompt.encode(),
                                          cwd=workspace, env=env, capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                started += 1
                raise
            started += 1
            if not response.returncode and backend == "codex":
                answer = Path(workspace, "answer")
                if not answer.is_file() or answer.stat().st_size > 32768:
                    raise CursorFailure("Codex did not return a bounded final message")
                try:
                    events = [json.loads(line) for line in response.stdout.splitlines() if line.strip()]
                    completed = [event for event in events if event.get("type") == "turn.completed"]
                    if len(completed) != 1:
                        raise ValueError("Missing completed turn")
                    response.stdout = json.dumps(dict(type="result", subtype="success", is_error=False,
                                                      result=answer.read_text(), usage=completed[0].get("usage")))
                except (AttributeError, ValueError, OSError):
                    raise CursorFailure("Codex did not return one completed review turn") from None
        except subprocess.TimeoutExpired as err:
            raise CursorFailure(f"{label} exceeded the {timeout}-second limit") from err
    if response.returncode:
        diagnostic = (response.stderr + response.stdout).lower()
        reason = "check the CLI credentials and account access"
        for markers, explanation in [
            ((b"invalid project config",), f"{label} rejected the workspace configuration"),
            ((b"not logged in", b"unauthenticated", b"authentication required"), f"check the {label} credentials"),
            ((b"not trusted", b"workspace trust"), "workspace trust was refused"),
            ((b"rate limit", b"quota", b"usage limit"), f"{label} reported a usage or rate limit"),
            ((b"connection", b"unavailable", b"network"), f"{label} could not reach its service"),
            ((b"not allowed", b"not available"), "the selected model or capability is unavailable"),
        ]:
            if any(marker in diagnostic for marker in markers):
                reason = explanation
                break
        raise CursorFailure(f"{label} exited {response.returncode}: {reason}. No retry was made.")
    try:
        if parse is not None:
            return parse(response.stdout, anchors)
        return parse_answer(response.stdout, anchors, limit, verification=verification, explain_dropped=explain_dropped)
    except (ReviewError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError) as err:
        try:
            usage = json.loads(response.stdout).get("usage")
        except (ValueError, AttributeError):
            usage = None
        raise CursorFailure(f"{label} returned an invalid review: " + type(err).__name__,
                            usage if isinstance(usage, dict) else None) from err


def review(prompt, model, anchors, cache, source, *, limit=MAX_FINDINGS, timeout=120, originals=None,
           parse=None, version=None, explain_dropped=False):
    """A cached model answer. A prompt with its own answer format passes its parser and its own
    cache version, so that neither format is ever read as the other."""
    signature = json.dumps([version or VERSION, "pr-agent:0.50.0", model, source, prompt], ensure_ascii=False)
    if originals is not None:
        signature = json.dumps(["strict-verifier", signature])
    elif explain_dropped and parse is None:
        signature = json.dumps(["explain-dropped", signature])
    if provider() != "cursor" or os.getenv("REVIEW_CURSOR_TEAM_ID"):
        signature = json.dumps([provider(), os.getenv("REVIEW_CURSOR_TEAM_ID") if provider() == "cursor" else None, signature])
    key = hashlib.sha256(signature.encode()).hexdigest()
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = cache / (key + ".json")
    with (cache / (key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            cached = json.loads(path.read_text())
            if originals is None:
                return cached, True
            try:
                cached["findings"] = kept(originals, cached)
            except ReviewError:
                # Older versions could cache a verdict that rewrote the findings.
                path.unlink()
            else:
                return cached, True
        # The plain review keeps the call it always made.
        extra = {} if (limit, timeout) == (MAX_FINDINGS, 120) else {"limit": limit, "timeout": timeout}
        if originals is not None:
            extra["verification"] = True
        elif explain_dropped and parse is None:
            extra["explain_dropped"] = True
        if parse is not None:
            extra["parse"] = parse
        result = invoke(prompt, model, anchors, **extra)
        if parse is not None:
            pass  # Not a list of findings: its own parser has checked all there is to check.
        elif originals is None:
            for finding in result["findings"]:
                if finding.get("verification_status") == "confirmed":
                    finding.pop("verification_status")
        else:
            try:
                result["findings"] = kept(originals, result)
            except ReviewError as err:
                raise CursorFailure(str(err), result.get("cursor_usage")) from err
        if parse is None and result.get("dropped") and not result["findings"]:
            return result, False  # A later request can retry a reply that yielded no usable findings.
        with tempfile.NamedTemporaryFile(dir=cache, mode="w", delete=False) as file:
            json.dump(result, file)
        os.replace(file.name, path)
        return result, False
