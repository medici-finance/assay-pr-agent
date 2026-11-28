"""Token counting, adapted from PR-Agent 0.50.0 and modified here (see README.md beside this file).

PR-Agent chooses a tiktoken encoding the way ``encoding`` below does, and points tiktoken at encoding
files bundled in LiteLLM so that nothing is downloaded at run time. The two files this reviewer needs
are bundled here instead, under the names tiktoken's cache gives them, and are checked against the
hashes tiktoken publishes before first use. The code is MIT licensed; see LICENSE.pr-agent.
"""
import hashlib
import os
from pathlib import Path

import tiktoken

# tiktoken reads <TIKTOKEN_CACHE_DIR>/<sha1 of the download URL>. tiktoken_ext/openai_public.py
# publishes that URL and a SHA-256 for each file; these are the values for the two this reviewer uses.
DIRECTORY = Path(__file__).with_name("tiktoken")
FILES = {"cl100k_base": ("9b5ad71b2ce5302211f9c61530b329a4922fc6a4", "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"),
         "o200k_base": ("fb374d419588a4632f3f557e76b4b70aebbca790", "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d")}
_ready = False


def prepare():
    """Point tiktoken at the bundled files unless the host chose a cache directory, after checking them.

    A changed encoding file would change every token count, so it is refused before it is used.
    """
    global _ready
    if _ready:
        return
    if "TIKTOKEN_CACHE_DIR" in os.environ or "DATA_GYM_CACHE_DIR" in os.environ:
        _ready = True
        return
    for name, (key, expected) in FILES.items():
        path = DIRECTORY / key
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            # Not marked ready: every later call is refused too, instead of falling back to a download.
            raise ValueError(f"The vendored {name} encoding file is missing or altered")
    os.environ["TIKTOKEN_CACHE_DIR"] = str(DIRECTORY)
    _ready = True


def encoding(model):
    """The encoding tiktoken maps a GPT model name to, and o200k_base for any other name.

    Only an unknown name falls back. A file that cannot be read is an error: falling back to another
    encoding would quietly change every count.
    """
    prepare()
    try:
        name = tiktoken.encoding_name_for_model(model) if "gpt" in model else "o200k_base"
    except KeyError:
        name = "o200k_base"
    return tiktoken.get_encoding(name)
