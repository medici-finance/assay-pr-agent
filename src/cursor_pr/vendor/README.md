# Vendored from PR-Agent

This directory holds the parts of [PR-Agent](https://github.com/the-pr-agent/pr-agent) 0.50.0 that the reviewer uses, copied so that the `pr-agent` package and everything it pulls in no longer have to be installed. PR-Agent is MIT licensed; the license text is in `LICENSE.pr-agent`.

| File | Source in PR-Agent 0.50.0 | Kept as |
|---|---|---|
| `patch.py` | `pr_agent/algo/types.py`, and `RE_HUNK_HEADER`, `NO_NEWLINE_AT_EOF_MARKER`, `_SPLITLINES_BREAK_CHARS`, `split_diff_lines`, `to_hunk_only_patch`, `extract_hunk_headers` and `decouple_and_convert_to_hunks_with_lines_numbers` from `pr_agent/algo/git_patch_processing.py` | Verbatim, with one change marked `CHANGED` |
| `pr_review_prompt.system.txt` | `pr_review_prompt.system` in `pr_agent/settings/pr_reviewer_prompts.toml` | The template as this reviewer renders it, with the finding limit left as `__MAX_FINDINGS__` |
| `tokens.py` | The choice of encoding in `TokenEncoder._create_encoder`, and its pointing tiktoken at bundled files | The same logic |
| `tiktoken/` | `cl100k_base` and `o200k_base`, the encoding files LiteLLM bundles and PR-Agent points tiktoken at | Unchanged, under the names tiktoken's cache gives them |

`tiktoken/` holds two public data files from OpenAI's tiktoken distribution. Each is checked against the SHA-256 tiktoken publishes for it before first use, and tiktoken is pointed at this directory unless `TIKTOKEN_CACHE_DIR` or `DATA_GYM_CACHE_DIR` is already set. Without them a fresh container downloads both files from the internet before its first review, and the offline test run in the image fails.

The change in `patch.py`: the warning about a line that starts with `@@` and is not a hunk header no longer logs that line, because it is content from the change under review.

`tests/test_vendor.py` pins the rendered rubric and the formatter's output by hash. To move to a newer PR-Agent release, regenerate these files from that release, record the hashes in that test again, and change the release named here and in `engine.review`'s cache signature.
