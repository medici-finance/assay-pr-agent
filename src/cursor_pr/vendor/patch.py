"""The numbered hunk text the model reads, and the trim that makes a git diff hunk-only.

Both started as copies of PR-Agent 0.50.0 (``pr_agent/algo/git_patch_processing.py``) and have been
rewritten here; README.md beside this file lists what changed. MIT licensed; see LICENSE.pr-agent.
"""
import re

# Characters that some readers take as a line break. Inside a line of the change they could make text
# from the change look like a line of this format, so they are shown as a space; the quote check in
# engine.finding ignores whitespace, so a quote of such a line still matches.
_BREAKS = str.maketrans(dict.fromkeys("\r\v\f\x1c\x1d\x1e\x85  ", " "))
_HUNK = re.compile(r"^@@", re.M)


def to_hunk_only_patch(patch_str: str) -> str:
    """Drop the file metadata before the first hunk of a git diff.

    Lines are split on line feeds only, as git and unidiff split them. Returns an empty string when
    the diff has no textual hunk, for example a binary or mode-only change.
    """
    first = _HUNK.search(patch_str)
    return patch_str[first.start():] if first else ""


def one_line(value):
    """One line of the change as shown: its line ending removed, other line breaks shown as a space."""
    return value.removesuffix("\n").removesuffix("\r").translate(_BREAKS)


def numbered(name, hunks):
    """The text the model reads for one file, built from the unidiff hunks the anchors come from.

    Each hunk is its header, then ``__new hunk__`` with every head-side line prefixed by its head line
    number and its marker (``+`` added, a space unchanged), then, when the hunk removes lines,
    ``__old hunk__`` with the base side unnumbered (``-`` removed, a space unchanged). Every line of
    the change is shown with a marker or a number in front, so none can look like these markers.
    """
    out = [f"\n\n## File: '{one_line(name.strip())}'\n"]
    for hunk in hunks:
        # Content lines only: unidiff also keeps "\ No newline at end of file" and trailing blank lines.
        lines = [line for line in hunk if line.line_type in ("+", "-", " ")]
        section = one_line(hunk.section_header).strip()
        out.append(f"\n@@ -{hunk.source_start},{hunk.source_length} +{hunk.target_start},{hunk.target_length} @@"
                   + (" " + section if section else "") + "\n__new hunk__\n")
        out += [f"{line.target_line_no} {line.line_type}{one_line(line.value)}\n"
                for line in lines if line.target_line_no is not None]
        if any(line.is_removed for line in lines):
            out.append("__old hunk__\n")
            out += [f"{line.line_type}{one_line(line.value)}\n" for line in lines if line.source_line_no is not None]
    return "".join(out).rstrip("\n")
