import re
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("Install-HybridMonitor.ps1")
RUNNER_PATH = Path(__file__).with_name("Run-HybridMonitor.ps1")
REMINDER_RUNNER_PATH = Path(__file__).with_name("Run-SubscriptionReminder.ps1")
LIFECYCLE_RUNNER_PATH = Path(__file__).with_name("Run-BondLifecycleReminder.ps1")


class InstallHybridMonitorScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = SCRIPT_PATH.read_text(encoding="utf-8")
        cls.runner = RUNNER_PATH.read_text(encoding="utf-8")
        cls.reminder_runner = REMINDER_RUNNER_PATH.read_text(encoding="utf-8")
        cls.lifecycle_runner = LIFECYCLE_RUNNER_PATH.read_text(encoding="utf-8")

    def test_keeps_weekday_0830_trigger_and_adds_current_user_logon_trigger(self):
        self.assertRegex(
            self.script,
            r"New-ScheduledTaskTrigger\s+-Weekly\b[^\r\n]*"
            r"-DaysOfWeek\s+Monday,Tuesday,Wednesday,Thursday,Friday\b[^\r\n]*"
            r"-At\s+'08:30'",
        )
        self.assertIn(
            "$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
            self.script,
        )
        self.assertRegex(
            self.script,
            r"\$logonTrigger\s*=\s*New-ScheduledTaskTrigger\s+-AtLogOn\s+-User\s+\$currentUser",
        )
        self.assertRegex(
            self.script,
            r"Register-ScheduledTask\b[^\r\n]*"
            r"-Trigger\s+@\(\$weeklyTrigger,\s*\$logonTrigger\)",
        )

    def test_principal_remains_current_user_interactive_token(self):
        self.assertRegex(
            self.script,
            r"New-ScheduledTaskPrincipal\s+-UserId\s+\$currentUser\s+"
            r"-LogonType\s+Interactive\s+-RunLevel\s+Limited",
        )
        self.assertRegex(
            self.script,
            r"Register-ScheduledTask\b[^\r\n]*-Principal\s+\$principal",
        )

    def test_catch_up_and_duplicate_safety_settings_are_preserved(self):
        for expected in (
            "-StartWhenAvailable",
            "-MultipleInstances IgnoreNew",
            "-ExecutionTimeLimit (New-TimeSpan -Hours 1)",
            "-RestartCount 3",
            "-RestartInterval (New-TimeSpan -Minutes 10)",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.script)
        self.assertIn("-AllowStartIfOnBatteries", self.script)
        self.assertIn("-DontStopIfGoingOnBatteries", self.script)

    def test_scheduled_action_uses_guarded_runner_mode(self):
        self.assertRegex(
            self.script,
            r'\$arguments\s*=\s*"-NoProfile -File `"\$runner`" -Scheduled"',
        )
        self.assertRegex(self.runner, r"\[switch\]\$Scheduled")
        self.assertRegex(self.runner, r"if \(\$Scheduled\) \{")

    def test_scheduled_mode_skips_before_beijing_0830(self):
        self.assertIn("FindSystemTimeZoneById('China Standard Time')", self.runner)
        self.assertIn("$todayCutoff = $nowBeijing.Date.AddHours(8).AddMinutes(30)", self.runner)
        self.assertRegex(
            self.runner,
            r"if \(\$nowBeijing -lt \$todayCutoff\) \{[\s\S]*?exit 0\s*\}",
        )

    def test_scheduled_mode_skips_after_today_was_successfully_processed(self):
        self.assertIn("$statePath = Join-Path $root 'state.json'", self.runner)
        self.assertIn("[string]$state.lastSuccessfulCutoff", self.runner)
        self.assertRegex(
            self.runner,
            r"if \(\$lastSuccessfulCutoff -ge \$todayCutoffOffset\) "
            r"\{[\s\S]*?exit 0\s*\}",
        )

    def test_manual_runner_invocation_is_not_forced_into_scheduled_mode(self):
        scheduled_block = self.runner.index("if ($Scheduled) {")
        log_setup = self.runner.index("$logDir = Join-Path $root 'logs'")
        self.assertLess(scheduled_block, log_setup)
        self.assertNotIn("--scheduled", self.runner)

    def test_subscription_reminder_has_independent_0850_0950_and_logon_triggers(self):
        for time in ("08:50", "09:50"):
            with self.subTest(time=time):
                self.assertRegex(
                    self.script,
                    r"New-ScheduledTaskTrigger\s+-Weekly\b[^\r\n]*"
                    r"-DaysOfWeek\s+Monday,Tuesday,Wednesday,Thursday,Friday\b[^\r\n]*"
                    rf"-At\s+'{time}'",
                )
        self.assertIn("$reminderTaskName = '可转债配债-申购提醒'", self.script)
        self.assertRegex(
            self.script,
            r"\$reminderLogon\s*=\s*New-ScheduledTaskTrigger\s+-AtLogOn\s+-User\s+\$currentUser",
        )
        self.assertRegex(
            self.script,
            r"Register-ScheduledTask\s+-TaskName\s+\$reminderTaskName\b[^\r\n]*"
            r"-Trigger\s+@\(\$reminder0850,\s*\$reminder0950,\s*\$reminderLogon\)",
        )

    def test_subscription_reminder_action_uses_dedicated_runner(self):
        self.assertRegex(
            self.script,
            r'\$reminderArguments\s*=\s*"-NoProfile -File `"\$reminderRunner`" -Scheduled"',
        )
        self.assertIn("'--subscription-reminders'", self.reminder_runner)
        self.assertNotIn("lastSuccessfulCutoff", self.reminder_runner)
        self.assertNotIn("08:30", self.reminder_runner)

    def test_subscription_reminder_preserves_catch_up_and_retry_settings(self):
        self.assertRegex(
            self.script,
            r"\$reminderSettings\s*=\s*New-ScheduledTaskSettingsSet\b[^\r\n]*"
            r"-StartWhenAvailable\b[^\r\n]*"
            r"-MultipleInstances\s+IgnoreNew\b[^\r\n]*"
            r"-RestartCount\s+3\b[^\r\n]*"
            r"-RestartInterval\s+\(New-TimeSpan -Minutes 10\)",
        )

    def test_lifecycle_reminder_has_independent_0915_and_logon_triggers(self):
        self.assertIn("$lifecycleTaskName = '可转债配债-出签上市提醒'", self.script)
        self.assertRegex(
            self.script,
            r"\$lifecycle0915\s*=\s*New-ScheduledTaskTrigger\s+-Weekly\b[^\r\n]*"
            r"-DaysOfWeek\s+Monday,Tuesday,Wednesday,Thursday,Friday\b[^\r\n]*"
            r"-At\s+'09:15'",
        )
        self.assertRegex(
            self.script,
            r"\$lifecycleLogon\s*=\s*New-ScheduledTaskTrigger\s+-AtLogOn\s+-User\s+\$currentUser",
        )
        self.assertRegex(
            self.script,
            r"Register-ScheduledTask\s+-TaskName\s+\$lifecycleTaskName\b[^\r\n]*"
            r"-Trigger\s+@\(\$lifecycle0915,\s*\$lifecycleLogon\)",
        )

    def test_lifecycle_reminder_action_is_independent_and_retry_safe(self):
        self.assertRegex(
            self.script,
            r'\$lifecycleArguments\s*=\s*"-NoProfile -File `"\$lifecycleRunner`" -Scheduled"',
        )
        self.assertIn("lifecycle_reminder.py", self.lifecycle_runner)
        self.assertNotIn("monitor.py", self.lifecycle_runner)
        self.assertRegex(
            self.script,
            r"\$lifecycleSettings\s*=\s*New-ScheduledTaskSettingsSet\b[^\r\n]*"
            r"-StartWhenAvailable\b[^\r\n]*"
            r"-AllowStartIfOnBatteries\b[^\r\n]*"
            r"-DontStopIfGoingOnBatteries\b[^\r\n]*"
            r"-MultipleInstances\s+IgnoreNew\b[^\r\n]*"
            r"-RestartCount\s+3\b[^\r\n]*"
            r"-RestartInterval\s+\(New-TimeSpan -Minutes 10\)",
        )


if __name__ == "__main__":
    unittest.main()
