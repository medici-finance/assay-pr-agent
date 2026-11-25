import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import buildinfo, poll


class BuildInfoTests(unittest.TestCase):
    def record(self, **fields):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "build-info.json"
        path.write_text(json.dumps(fields))
        return buildinfo.load(path)

    def test_published_image_reports_commit_tags_and_build_time(self):
        build = self.record(sha="c9c6c72c7b8ec1caa2689bcfd8543b50cff028a2",
                            tags="main latest", built_at="2026-11-25T18:42:10Z")
        self.assertEqual(build, buildinfo.Build("c9c6c72c7b8ec1caa2689bcfd8543b50cff028a2",
                                                ("main", "latest"), "2026-11-25T18:42:10Z"))
        self.assertEqual(buildinfo.banner(build),
                         "Assay PR Agent build: commit c9c6c72c7b8ec1caa2689bcfd8543b50cff028a2, "
                         "tags main, latest, built 2026-11-25T18:42:10Z")

    def test_local_build_without_tags_omits_them(self):
        build = self.record(sha="unknown", tags="", built_at="2026-11-25T18:42:10Z")
        self.assertEqual(build.tags, ())
        self.assertEqual(buildinfo.banner(build),
                         "Assay PR Agent build: commit unknown, built 2026-11-25T18:42:10Z")

    def test_missing_or_malformed_record_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-info.json"
            self.assertIsNone(buildinfo.load(path))
            path.write_text("not json")
            self.assertIsNone(buildinfo.load(path))
            path.write_text(json.dumps(["not", "a", "record"]))
            self.assertIsNone(buildinfo.load(path))
            path.write_text(json.dumps({"sha": "", "tags": "", "built_at": ""}))
            self.assertIsNone(buildinfo.load(path))
        self.assertEqual(buildinfo.banner(None),
                         "Assay PR Agent build: unknown (no build record in this image)")

    def test_container_startup_prints_banner_before_dispatch(self):
        build = buildinfo.Build("abc123", ("main", "latest"), "2026-11-25T18:42:10Z")
        out = io.StringIO()
        with patch("sys.argv", ["cursor-review-container", "manage"]), \
                patch("cursor_pr.buildinfo.load", return_value=build), \
                patch("cursor_pr.manager.console", side_effect=SystemExit(0)) as console, \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                poll.container_main()
        console.assert_called_once_with()
        self.assertEqual(out.getvalue(),
                         "Assay PR Agent build: commit abc123, tags main, latest, "
                         "built 2026-11-25T18:42:10Z\n")
