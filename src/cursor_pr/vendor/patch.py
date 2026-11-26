"""Diff formatting from PR-Agent 0.50.0, vendored.

Source: the-pr-agent/pr-agent at version 0.50.0, files ``pr_agent/algo/types.py`` and
``pr_agent/algo/git_patch_processing.py``. Everything below is verbatim except one change, marked
"CHANGED": a warning about an unparsable hunk header is logged without the offending line, because
that line is content from the change under review. MIT licensed; see LICENSE.pr-agent.
"""
import logging
import re
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)

RE_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[ ]?(.*)")

NO_NEWLINE_AT_EOF_MARKER = r'\ No newline at end of file'

_SPLITLINES_BREAK_CHARS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"

def split_diff_lines(text: str) -> list[str]:
    """Split on line feeds only, as git does; ``str.splitlines()`` also breaks on U+2028, form feeds, etc."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]

def to_hunk_only_patch(patch_str: str) -> str:
    """Drop unified-diff file metadata before the first hunk.

    ``FilePatchInfo.patch`` consumers expect hunk-only patches and may otherwise
    treat ``---``/``+++`` file headers as changed source lines. Returns an empty
    string when the diff has no textual hunk, for example a rename-only change.
    """
    hunk_start = patch_str.find("@@")
    while hunk_start != -1:
        if hunk_start == 0 or patch_str[hunk_start - 1] in _SPLITLINES_BREAK_CHARS:
            return patch_str[hunk_start:]
        hunk_start = patch_str.find("@@", hunk_start + 2)
    return ""


from dataclasses import dataclass
from enum import Enum


class EDIT_TYPE(Enum):
    ADDED = 1
    DELETED = 2
    MODIFIED = 3
    RENAMED = 4
    UNKNOWN = 5


@dataclass
class FilePatchInfo:
    base_file: str
    head_file: str
    patch: str
    filename: str
    tokens: int = -1
    edit_type: EDIT_TYPE = EDIT_TYPE.UNKNOWN
    old_filename: str = None
    num_plus_lines: int = -1
    num_minus_lines: int = -1
    language: str | None = None
    ai_file_summary: str = None
    head_file_is_complete: bool = True
    # Set when the provider could not read one side of the file, so no trustworthy patch can
    # be built for it. The file is kept with an empty patch and this flag, and diff generation
    # tells the model the file could not be reviewed instead of dropping it silently.
    content_fetch_failed: bool = False


def extract_hunk_headers(match):
    res = list(match.groups())
    start1 = int(res[0]) if res[0] is not None else 0
    size1 = int(res[1]) if res[1] is not None else 1
    start2 = int(res[2]) if res[2] is not None else 0
    size2 = int(res[3]) if res[3] is not None else 1
    section_header = res[4] if res[4] is not None else ""
    return section_header, size1, size2, start1, start2


def decouple_and_convert_to_hunks_with_lines_numbers(patch: str, file) -> str:
    """
    Convert a given patch string into a string with line numbers for each hunk, indicating the new and old content of
    the file.

    Args:
        patch (str): The patch string to be converted.
        file: An object containing the filename of the file being patched.

    Returns:
        str: A string with line numbers for each hunk, indicating the new and old content of the file.

    example output:
## src/file.ts
__new hunk__
881        line1
882        line2
883        line3
887 +      line4
888 +      line5
889        line6
890        line7
...
__old hunk__
        line1
        line2
-       line3
-       line4
        line5
        line6
           ...
    """

    # Add a header for the file
    if file:
        # if the file was deleted, return a message indicating that the file was deleted
        if hasattr(file, 'edit_type') and file.edit_type == EDIT_TYPE.DELETED:
            return f"\n\n## File '{file.filename.strip()}' was deleted\n"

        patch_with_lines_str = f"\n\n## File: '{file.filename.strip()}'\n"
    else:
        patch_with_lines_str = ""

    rendered_hunks = []
    patch_lines = split_diff_lines(patch)
    new_content_lines = []
    old_content_lines = []
    match = None
    start1, size1, start2, size2 = -1, -1, -1, -1
    prev_header_line = []
    header_line = []
    skip_hunk = False
    for line_i, line in enumerate(patch_lines):
        if line == NO_NEWLINE_AT_EOF_MARKER:
            continue

        if line.startswith('@@'):
            hunk_header_match = RE_HUNK_HEADER.match(line)
            if not hunk_header_match:
                # CHANGED: upstream logs the offending line with this warning. It is content from the change.
                log.warning("Skipping a line that starts with '@@' but is not a unified hunk header")
                skip_hunk = True
                continue
            skip_hunk = False
            header_line = line
            match = hunk_header_match
            if match and (new_content_lines or old_content_lines):  # found a new hunk, split the previous lines
                if prev_header_line:
                    patch_with_lines_str += f'\n{prev_header_line}\n'
                is_plus_lines = is_minus_lines = False
                if new_content_lines:
                    is_plus_lines = any(line.startswith('+') for line in new_content_lines)
                if old_content_lines:
                    is_minus_lines = any(line.startswith('-') for line in old_content_lines)
                # Always present the new hunk for the section, otherwise the LLM gets confused
                if is_plus_lines or is_minus_lines:
                    patch_with_lines_str = patch_with_lines_str.rstrip('\r\n') + '\n__new hunk__\n'
                    for i, line_new in enumerate(new_content_lines):
                        patch_with_lines_str += f"{start2 + i} {line_new}\n"
                if is_minus_lines:
                    patch_with_lines_str = patch_with_lines_str.rstrip('\r\n') + '\n__old hunk__\n'
                    for line_old in old_content_lines:
                        patch_with_lines_str += f"{line_old}\n"
                # Keep completed hunks out of subsequent rstrip/concatenation work.
                rendered_hunks.append(patch_with_lines_str)
                patch_with_lines_str = ""
                new_content_lines = []
                old_content_lines = []
            if match:
                prev_header_line = header_line

            section_header, size1, size2, start1, start2 = extract_hunk_headers(match)

        elif skip_hunk:
            continue
        elif match is None:
            # Ignore unified-diff file metadata before the first valid hunk.
            continue
        elif line.startswith('+'):
            new_content_lines.append(line)
        elif line.startswith('-'):
            old_content_lines.append(line)
        else:
            if not line and line_i: # if this line is empty and the next line is a hunk header, skip it
                if line_i + 1 < len(patch_lines) and patch_lines[line_i + 1].startswith('@@'):
                    continue
                elif line_i + 1 == len(patch_lines):
                    continue
            new_content_lines.append(line)
            old_content_lines.append(line)

    # finishing last hunk
    if match and (new_content_lines or old_content_lines):
        patch_with_lines_str += f'\n{header_line}\n'
        is_plus_lines = is_minus_lines = False
        if new_content_lines:
            is_plus_lines = any(line.startswith('+') for line in new_content_lines)
        if old_content_lines:
            is_minus_lines = any(line.startswith('-') for line in old_content_lines)
        # Always present the new hunk for the section, otherwise the LLM gets confused
        if is_plus_lines or is_minus_lines:
            patch_with_lines_str = patch_with_lines_str.rstrip('\r\n') + '\n__new hunk__\n'
            for i, line_new in enumerate(new_content_lines):
                patch_with_lines_str += f"{start2 + i} {line_new}\n"
        if is_minus_lines:
            patch_with_lines_str = patch_with_lines_str.rstrip('\r\n') + '\n__old hunk__\n'
            for line_old in old_content_lines:
                patch_with_lines_str += f"{line_old}\n"

    rendered_hunks.append(patch_with_lines_str)
    return "".join(rendered_hunks).rstrip('\r\n')
