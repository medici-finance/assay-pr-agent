import io
import unittest

from loguru import logger
from pr_agent.custom_merge_loader import load

import cursor_pr  # noqa: F401  importing the package applies the logger scope


class SettingsNoiseTests(unittest.TestCase):
    def test_pr_agent_missing_secrets_warning_is_silenced(self):
        out = io.StringIO()
        handler = logger.add(out, format="{message}")
        try:
            load(type("Settings", (), {"settings_files": ["/nonexistent/.secrets.toml"]})())
        finally:
            logger.remove(handler)
        self.assertEqual(out.getvalue(), "")
