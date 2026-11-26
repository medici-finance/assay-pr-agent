"""The released monitor must fail loudly through the poller, not just exit 2.

v0.5.0 in the cluster: the egress NetworkPolicy dropped DNS, so every forge read
degraded and `deskmonitor pr` exited 2 on every cycle. The poller swallowed the
monitor's own MONITOR-DEGRADED line and logged a bare exit code for a day. CI runs
this suite inside the image with --network none, so the real binary fails here the
same way; it is skipped where the desk tools are not installed.
"""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import credentials, discovery
from cursor_pr.config import Project
from cursor_pr.source import ReviewError


@unittest.skipUnless(shutil.which("deskmonitor"), "the Assay desk tools ship in the released image")
class DegradedMonitorTests(unittest.TestCase):
    def test_a_degraded_read_reaches_the_poller_with_its_cause(self):
        state = Path(tempfile.mkdtemp())
        project = Project(provider="github", url="https://github.com/medici-finance/assay-pr-agent",
                          slug="medici-finance/assay-pr-agent", checkout=state / "checkout", requesters=frozenset())
        project.checkout.mkdir()
        home = state / "forge-home"
        # The same roster and HOME the poller hands the desk tools, so the monitor gets
        # as far as the forge read and reports why that read failed.
        with patch.dict(os.environ, {"REVIEW_OPERATOR": "kryton:1", "REVIEW_ASSAY_HOME": str(home)}):
            credentials.write_roster(home, [project])
            with self.assertRaises(ReviewError) as raised:
                discovery.open_changes(project, state)
        message = str(raised.exception)
        self.assertIn("deskmonitor pr exited 2: MONITOR-DEGRADED: medici-finance/assay-pr-agent seed read FAILED", message)
        self.assertEqual(list((state / "monitor").glob("*/*.state")), [], "a degraded seed must not leave a baseline")
