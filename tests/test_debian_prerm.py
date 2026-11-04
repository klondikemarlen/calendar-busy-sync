import io
import runpy
import subprocess
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import mock_open, patch

HOOK = runpy.run_path(str(Path(__file__).resolve().parents[1] / "debian" / "prerm"))
GLOBALS = HOOK["main"].__globals__
BEGIN = HOOK["BEGIN"]
END = HOOK["END"]
SYSTEM_JOB = (
    "*/15 * * * * XDG_CONFIG_HOME='/home/test/config space' "
    "XDG_STATE_HOME=/home/test/state PATH=/usr/bin:/bin "
    "/usr/bin/python3 -m calendar_busy_sync sync"
)


def block(command=SYSTEM_JOB):
    return f"{BEGIN}\n{command}\n{END}\n"


def result(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class DebianScheduleSelectionTests(unittest.TestCase):
    def test_preserves_unrelated_entries_byte_for_byte(self):
        before = "MAILTO=test@example.org\n\n*/2 * * * * unrelated\n"
        after = "# comment without final newline"
        self.assertEqual(HOOK["without_system_schedule"](before + block() + after), before + after)

    def test_no_markers_and_empty_text_are_unchanged(self):
        for text in ("", "\n", "*/1 * * * * /usr/bin/python3 -m calendar_busy_sync sync\n"):
            with self.subTest(text=text):
                self.assertEqual(HOOK["without_system_schedule"](text), text)

    def test_other_python_installations_are_preserved(self):
        for executable in ("/home/test/venv/bin/python3", "/usr/local/bin/python3"):
            text = block(SYSTEM_JOB.replace("/usr/bin/python3", executable))
            with self.subTest(executable=executable):
                self.assertEqual(HOOK["without_system_schedule"](text), text)

    def test_shell_quoted_paths_and_cron_percent_escaping_are_not_executed(self):
        command = SYSTEM_JOB.replace(
            "XDG_STATE_HOME=/home/test/state", "XDG_STATE_HOME='/home/test/\";$(touch forbidden)\\%state'"
        )
        with patch("subprocess.run", side_effect=AssertionError("selection must not execute commands")):
            self.assertEqual(HOOK["without_system_schedule"](block(command)), "")

    def test_malformed_or_ambiguous_ownership_is_rejected(self):
        for text in (
            f"{BEGIN}\n{SYSTEM_JOB}\n",
            f"{END}\n",
            block() + block(),
            f"{BEGIN}\n{SYSTEM_JOB}\nextra\n{END}\n",
            f" {BEGIN}\n{SYSTEM_JOB}\n{END}\n",
            block().replace("schedule v1", "schedule v2"),
        ):
            with self.subTest(text=text), self.assertRaises(HOOK["CleanupError"]):
                HOOK["without_system_schedule"](text)

    def test_unrecognized_owned_commands_fail_without_exposing_content(self):
        for command in (
            SYSTEM_JOB + " && secret-value",
            SYSTEM_JOB + " > secret-value",
            SYSTEM_JOB.replace("*/15", "@daily"),
            SYSTEM_JOB.replace("/usr/bin/python3", "python3"),
            SYSTEM_JOB.replace("calendar_busy_sync", "another_module"),
            SYSTEM_JOB.replace("PATH=/usr/bin:/bin", "PYTHONPATH=secret-value"),
            SYSTEM_JOB + " '",
        ):
            with self.subTest(command=command), self.assertRaises(HOOK["CleanupError"]) as caught:
                HOOK["without_system_schedule"](block(command))
            self.assertNotIn("secret-value", str(caught.exception))


class DebianScheduleCleanupTests(unittest.TestCase):
    def test_removes_only_system_job_across_synthetic_accounts(self):
        unrelated = "MAILTO=test@example.org\n*/3 * * * * unrelated\n"
        venv_job = block(SYSTEM_JOB.replace("/usr/bin/python3", "/home/test/venv/bin/python3"))
        tables = {"system": unrelated + block(), "venv": venv_job, "empty": ""}
        calls = []

        def fake_invoke(account, *arguments, input_text=None):
            calls.append((account, arguments, input_text))
            if arguments == ("-l",):
                if account not in tables:
                    return result(1, stderr=f"no crontab for {account}\n")
                return result(stdout=tables[account])
            self.assertEqual(arguments, ("-",))
            tables[account] = input_text
            return result()

        with patch.dict(
            GLOBALS, local_accounts=lambda: ["system", "venv", "empty", "absent"], invoke=fake_invoke
        ):
            HOOK["remove_schedules"]()
        self.assertEqual(tables, {"system": unrelated, "venv": venv_job, "empty": ""})
        mutations = [call for call in calls if call[1] != ("-l",)]
        self.assertEqual(mutations, [("system", ("-",), unrelated)])

    def test_only_owned_job_removes_table_instead_of_installing_empty_table(self):
        calls = []

        def fake_invoke(account, *arguments, input_text=None):
            calls.append((account, arguments, input_text))
            return result(stdout=block()) if arguments == ("-l",) else result()

        with patch.dict(GLOBALS, local_accounts=lambda: ["test"], invoke=fake_invoke):
            HOOK["remove_schedules"]()
        self.assertEqual(calls[-1], ("test", ("-r",), None))

    def test_changed_crontab_stops_before_mutation(self):
        calls = []

        def fake_invoke(account, *arguments, input_text=None):
            calls.append(arguments)
            return result(stdout=block() if len(calls) == 1 else block() + "# new\n")

        with patch.dict(GLOBALS, local_accounts=lambda: ["test"], invoke=fake_invoke):
            with self.assertRaisesRegex(HOOK["CleanupError"], "changed during cleanup"):
                HOOK["remove_schedules"]()
        self.assertEqual(calls, [("-l",), ("-l",)])

    def test_malformed_later_account_prevents_all_mutations(self):
        calls = []

        def fake_invoke(account, *arguments, input_text=None):
            calls.append(arguments)
            return result(stdout=block() if account == "first" else BEGIN + "\n")

        with patch.dict(GLOBALS, local_accounts=lambda: ["first", "broken"], invoke=fake_invoke):
            with self.assertRaisesRegex(HOOK["CleanupError"], "Account 'broken'"):
                HOOK["remove_schedules"]()
        self.assertEqual(calls, [("-l",), ("-l",)])

    def test_read_failures_are_not_treated_as_absent_tables(self):
        for response in (
            result(1, stderr="permission denied"),
            result(2),
            result(1, stderr="no crontab for someone-else"),
        ):
            with (
                self.subTest(response=response),
                patch.dict(GLOBALS, invoke=lambda *args, response=response: response),
            ):
                with self.assertRaises(HOOK["CleanupError"]):
                    HOOK["read_crontab"]("test")

    def test_mutation_failure_stops_uninstall_with_safe_diagnostic(self):
        def fake_invoke(account, *arguments, input_text=None):
            if arguments == ("-l",):
                return result(stdout=block())
            return result(1, stderr="secret-value")

        output = io.StringIO()
        with (
            patch.dict(GLOBALS, local_accounts=lambda: ["test"], invoke=fake_invoke),
            redirect_stderr(output),
        ):
            self.assertEqual(HOOK["main"](["remove"]), 1)
        self.assertIn("Account 'test'", output.getvalue())
        self.assertNotIn("secret-value", output.getvalue())

    def test_upgrade_and_other_actions_do_not_enumerate_accounts_or_run_commands(self):
        def forbidden(*args, **kwargs):
            self.fail("Non-removal must not inspect accounts or crontabs")

        with patch.dict(GLOBALS, local_accounts=forbidden, invoke=forbidden):
            for action in ([], ["upgrade", "2.0"], ["deconfigure"], ["failed-upgrade"], ["anything"]):
                with self.subTest(action=action):
                    self.assertEqual(HOOK["main"](action), 0)

    def test_command_execution_failures_are_safe(self):
        for error in (OSError("secret-value"), subprocess.TimeoutExpired("secret-value", 15)):
            with self.subTest(error=error), patch("subprocess.run", side_effect=error):
                with self.assertRaises(HOOK["CleanupError"]) as caught:
                    HOOK["invoke"]("test", "-l")
                self.assertNotIn("secret-value", str(caught.exception))

    def test_accounts_come_only_from_local_passwd(self):
        contents = "root:x:0:0:root:/root:/bin/sh\ntest:x:1000:1000::/home/test:/bin/sh\n"
        with patch("builtins.open", mock_open(read_data=contents)) as opened:
            self.assertEqual(HOOK["local_accounts"](), ["root", "test"])
        opened.assert_called_once_with("/etc/passwd", encoding="utf-8")

    def test_invalid_local_accounts_fail_closed(self):
        for row in ("broken", "-option:x:1:1::/:/bin/sh", "test:x:bad:1::/:/bin/sh"):
            with self.subTest(row=row), patch("builtins.open", mock_open(read_data=row)):
                with self.assertRaises(HOOK["CleanupError"]):
                    HOOK["local_accounts"]()


if __name__ == "__main__":
    unittest.main()
