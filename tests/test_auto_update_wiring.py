#!/usr/bin/env python3
"""
Regression tests for the two reasons Chromium could never auto-update.

Both were found by running the thing end to end on 2026-09-29 rather than by
reading it, and both are the same shape: a piece of correct logic elsewhere in
the system was computed and then thrown away, leaving a string comparison to
make the decision that a proper version comparison should have made.

  1. The version detector was never registered, so `image_update` had no
     detector at all. Its default is fail-closed, so every check reported
     "update availability unknown" and the feature silently did nothing.
  2. Even once wired, the worker compared `latest == current` as raw strings.
     Alpine's index publishes "124.0.6367.78-r0"; the image's own manifest and
     `chromium-browser --version` say "124.0.6367.78". Those are the SAME
     browser, but the string compare says they differ, so an image already at
     124 reported "update available" to 124.0.6367.78-r0 -- forever.
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(os.path.dirname(HERE), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import bridge  # noqa: E402
import image_update  # noqa: E402


class TestVersionComparison(unittest.TestCase):
    """The Alpine package revision must not read as a version difference."""

    def test_package_revision_is_not_a_different_browser(self):
        self.assertTrue(image_update._is_up_to_date(
            "124.0.6367.78", "124.0.6367.78-r0"))

    def test_a_real_newer_version_is_still_an_update(self):
        self.assertFalse(image_update._is_up_to_date(
            "124.0.6367.78", "152.0.7977.82-r0"))

    def test_lexical_order_does_not_win(self):
        # '99...' > '152...' as strings, but 99 is older.
        self.assertFalse(image_update._is_up_to_date(
            "99.0.1.2", "152.0.7977.82"))

    def test_missing_trailing_component_is_zero(self):
        self.assertTrue(image_update._is_up_to_date(
            "152.0.7977", "152.0.7977.0-r0"))

    def test_image_ahead_of_the_index_is_not_an_update(self):
        # A branch whose release has not propagated to the mirror.
        self.assertTrue(image_update._is_up_to_date(
            "152.0.7977.82", "124.0.6367.78-r0"))

    def test_unknown_is_never_up_to_date(self):
        # "cannot tell" must never read as "up to date" -- that direction
        # hides a real update behind a green UI.
        self.assertFalse(image_update._is_up_to_date(None, "152.0.7977.82"))
        self.assertFalse(image_update._is_up_to_date("124.0.6367.78", None))
        self.assertFalse(image_update._is_up_to_date("", ""))

    def test_detector_verdict_is_preferred_over_a_local_guess(self):
        # The detector is the component that knows how to compare an index
        # version to an installed one. If it says "no update", that wins even
        # where a naive local compare would have said otherwise.
        self.assertTrue(image_update._is_up_to_date(
            "124.0.6367.78", "152.0.7977.82-r0", {"update_available": False}))
        self.assertFalse(image_update._is_up_to_date(
            "124.0.6367.78", "152.0.7977.82-r0", {"update_available": True}))

    def test_undetermined_detector_verdict_falls_through(self):
        # None means "could not determine" -- must not be read as False/uptodate.
        self.assertTrue(image_update._is_up_to_date(
            "124.0.6367.78", "124.0.6367.78-r0", {"update_available": None}))

    def test_version_tuple_rejects_nonsense(self):
        self.assertIsNone(image_update._version_tuple("not-a-version"))
        self.assertIsNone(image_update._version_tuple(None))
        self.assertIsNone(image_update._version_tuple(123))


class TestDetectorIsRegistered(unittest.TestCase):
    """A detector that is computed but never installed cannot help anyone."""

    def test_bridge_registers_the_detector_on_first_check(self):
        with mock.patch.object(bridge, "_detector_registered", False), \
             mock.patch.object(bridge, "image_update") as fake_updater:
            fake_updater.check_and_start.return_value = {"state": "checking"}
            bridge._handle({"id": "1", "method": "image_update_check",
                            "params": {"reason": "test"}})
            self.assertTrue(
                image_update.get_version_detector is not None,
                "bridge must expose the registration hook")
            # The updater must have been asked to run the check.
            fake_updater.check_and_start.assert_called()

    def test_registration_is_idempotent(self):
        with mock.patch.object(bridge, "_detector_registered", False), \
             mock.patch("chromium_version.register_detector") as reg:
            bridge._ensure_version_detector()
            bridge._ensure_version_detector()
            self.assertEqual(reg.call_count, 1,
                             "the detector must only be installed once")

    def test_a_broken_detector_does_not_break_the_bridge(self):
        with mock.patch.object(bridge, "_detector_registered", False), \
             mock.patch("chromium_version.register_detector",
                        side_effect=RuntimeError("boom")):
            bridge._ensure_version_detector()   # must not raise

    def test_nothing_is_registered_at_import_time(self):
        """Importing the backend must not do network or heavy work."""
        code = (
            "import sys, os\n"
            f"sys.path.insert(0, {BACKEND!r})\n"
            "import bridge\n"
            "assert 'chromium_version' not in sys.modules, "
            "'the detector module is imported on the import path'\n"
            "print('OK')\n"
        )
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0,
                         f"import-time registration detected:\n{out.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestWorkerActuallyUsesTheComparison(unittest.TestCase):
    """
    Tests the CALL SITE, not just the helper.

    Written because the first version of this file tested `_is_up_to_date`
    directly, and a mutation that restored the shipped raw string comparison at
    the call site left all 13 tests GREEN -- the helper still worked, nothing
    checked that the worker consults it. That is the same gap as the tmpfs
    finding: correct logic, tested in isolation, never asserted where the
    decision is actually made.
    """

    def _drive_worker(self, current, latest):
        """Run the real worker with a stubbed detector. No Docker, no build."""
        seen = {}
        import tempfile
        lock = tempfile.TemporaryFile()
        lock_fd = lock.fileno()
        with mock.patch.object(image_update.ImageUpdater, "_detect_versions",
                               return_value=(current, latest, None)), \
             mock.patch.object(image_update.ImageUpdater,
                               "_read_installed_versions",
                               return_value=(current, "3.19.9")), \
             mock.patch.object(image_update.ImageUpdater, "_build") as build, \
             mock.patch.object(image_update.ImageUpdater, "_verify"), \
             mock.patch.object(image_update.ImageUpdater, "_promote"), \
             mock.patch.object(image_update.ImageUpdater, "_discard_tag"), \
             mock.patch.object(image_update.ImageUpdater,
                               "auto_rebuild_requested", return_value=False), \
             mock.patch.object(image_update, "_now", return_value=1000.0, create=True):
            up = image_update.ImageUpdater()
            up._min_interval = 0
            up._write_state = lambda **kw: seen.update(kw)
            up._worker(lock_fd=lock_fd, force=False, reason="test")
        return seen, build

    def test_same_browser_with_package_revision_is_up_to_date(self):
        """The exact live symptom: 124 installed, index publishes 124.0.6367.78-r0.

        Those are the SAME browser, but the shipped code compared the strings,
        saw a difference, and reported an update available forever.
        """
        seen, build = self._drive_worker("124.0.6367.78", "124.0.6367.78-r0")
        self.assertEqual(seen.get("state"), "up_to_date",
                         f"same browser reported as {seen.get('state')!r}")
        build.assert_not_called()

    def test_genuinely_newer_browser_is_reported_as_available(self):
        seen, build = self._drive_worker("124.0.6367.78", "152.0.7977.82-r0")
        self.assertEqual(seen.get("state"), "update_available",
                         f"real update reported as {seen.get('state')!r}")
        # Default policy is notify-only: an available update must NOT build.
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
