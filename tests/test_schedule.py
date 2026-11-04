import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from calendar_busy_sync import schedule


class ScheduleTests(unittest.TestCase):
    def test_owned_block_removal_preserves_unrelated_lines(self):
        lines = ["MAILTO=x", schedule._BEGIN, "*/15 * * * * do-work", schedule._END, "# comment"]
        clean, count = schedule._owned_lines(lines)
        self.assertEqual(count, 1)
        self.assertEqual(clean, ["MAILTO=x", "# comment"])

    def test_incomplete_or_ambiguous_block_is_rejected(self):
        for lines in (
            [schedule._BEGIN, "*/5 * * * * unrelated"],
            [schedule._BEGIN, "*/5 * * * * owned", schedule._END, schedule._BEGIN],
        ):
            with self.subTest(lines=lines), self.assertRaises(schedule.ScheduleError):
                schedule._owned_lines(lines)

    def test_crontab_rc_one_without_no_crontab_diagnostic_is_not_empty(self):
        result = type("Result", (), {"returncode": 1, "stderr": "permission denied", "stdout": ""})()
        with patch("calendar_busy_sync.schedule._invoke", return_value=result):
            with self.assertRaises(schedule.ScheduleError):
                schedule._read("fake-crontab")

    def test_enable_and_disable_preserve_unrelated_jobs_with_fake_crontab(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            crontab = bin_dir / "crontab"
            database = root / "cron.txt"
            crontab.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-l" ]; then\n'
                f"  if [ -f {str(database)!r} ]; then cat {str(database)!r}; else echo 'no crontab for test' >&2; exit 1; fi\n"
                'elif [ "$1" = "-r" ]; then rm -f ' + repr(str(database)) + "\n"
                f"else cat > {str(database)!r}; fi\n"
            )
            crontab.chmod(0o755)
            database.write_text("MAILTO=test@example.org\n*/2 * * * * unrelated\n")
            paths = type("Paths", (), {"config_dir": root / "cfg", "state_dir": root / "state"})()
            paths.config_file = paths.config_dir / "config.json"
            with (
                patch.dict(os.environ, {"PATH": f"{bin_dir}:/usr/bin:/bin"}),
                patch("calendar_busy_sync.schedule.settings.load_settings", return_value=object()),
                patch("calendar_busy_sync.schedule.state.has_success", return_value=True),
                patch("calendar_busy_sync.schedule.engine.sync_lock", return_value=nullcontext()),
                patch("calendar_busy_sync.schedule._entry", return_value="*/15 * * * * /absolute/app sync"),
            ):
                schedule.enable_schedule(paths=paths)
                installed = database.read_text()
                self.assertIn("MAILTO=test@example.org", installed)
                self.assertIn("*/2 * * * * unrelated", installed)
                self.assertIn(schedule._BEGIN, installed)
                self.assertIn(schedule._END, installed)
                schedule.disable_schedule(paths=paths)
            self.assertEqual(database.read_text(), "MAILTO=test@example.org\n*/2 * * * * unrelated\n")


if __name__ == "__main__":
    unittest.main()
