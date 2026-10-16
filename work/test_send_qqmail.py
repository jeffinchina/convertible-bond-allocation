from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


def load_sender_module():
    path = Path(__file__).with_name("Send-QQMail.py")
    spec = importlib.util.spec_from_file_location("send_qqmail", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MailHeaderTests(unittest.TestCase):
    def test_automated_message_has_delivery_headers(self) -> None:
        sender = load_sender_module()
        request = {
            "from": "sender@invalid",
            "recipients": ["first@invalid", "second@invalid"],
            "isHtml": True,
        }
        message = sender.build_message(request, "日报", "<p>内容</p>")
        self.assertTrue(message["Date"])
        self.assertTrue(message["Message-ID"].startswith("<"))
        self.assertEqual(message["Auto-Submitted"], "auto-generated")
        self.assertEqual(message.get_content_subtype(), "html")

    def test_error_text_redacts_address_and_secret(self) -> None:
        sender = load_sender_module()
        address = "private.user" + "@" + "internal.example"
        secret = "not-a-real-credential"
        sanitized = sender.sanitize_error_text(f"failed for {address}: {secret}", (secret,))
        self.assertNotIn(address, sanitized)
        self.assertNotIn(secret, sanitized)
        self.assertIn("<redacted-email>", sanitized)
        self.assertIn("<redacted-secret>", sanitized)


class PowerShellMailSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work_dir = Path(__file__).resolve().parent
        self.send_script = (self.work_dir / "Send-QQMail.ps1").read_text(encoding="utf-8-sig")
        self.configure_script = (self.work_dir / "Configure-QQMail.ps1").read_text(
            encoding="utf-8-sig"
        )

    def test_public_scripts_do_not_embed_email_addresses(self) -> None:
        email_literal = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
        self.assertIsNone(email_literal.search(self.send_script))
        self.assertIsNone(email_literal.search(self.configure_script))

    def test_recipient_config_supports_canonical_and_legacy_shapes(self) -> None:
        canonical = self.send_script.index("$config.to")
        plural_legacy = self.send_script.index("$config.recipients")
        singular_legacy = self.send_script.index("$config.recipient)")
        self.assertLess(canonical, plural_legacy)
        self.assertLess(plural_legacy, singular_legacy)
        self.assertIn("$recipients.Count -eq 0", self.send_script)

    def test_send_ledger_mutex_wraps_check_delivery_and_write(self) -> None:
        wait = self.send_script.index("$sendLedgerMutex.WaitOne")
        read_log = self.send_script.index("$sentLog = if (Test-Path", wait)
        deliver = self.send_script.index("$transportResult = Send-QqSmtpMessageWithPython", read_log)
        write_log = self.send_script.index("$sentLog | ConvertTo-Json", deliver)
        release = self.send_script.index("$sendLedgerMutex.ReleaseMutex()", write_log)
        self.assertLess(wait, read_log)
        self.assertLess(read_log, deliver)
        self.assertLess(deliver, write_log)
        self.assertLess(write_log, release)
        self.assertIn("AbandonedMutexException", self.send_script)

    def test_noninteractive_recipient_migration_preserves_ciphertext(self) -> None:
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            self.skipTest("PowerShell is not available")

        with tempfile.TemporaryDirectory(dir=self.work_dir) as temp_dir:
            temp_path = Path(temp_dir)
            shutil.copy2(self.work_dir / "Configure-QQMail.ps1", temp_path)
            config_path = temp_path / "qqmail-config.json"
            original_ciphertext = "opaque-dpapi-ciphertext"
            config_path.write_text(
                json.dumps(
                    {
                        "smtpServer": "smtp.qq.com",
                        "smtpPort": 587,
                        "username": "sender@invalid",
                        "from": "sender@invalid",
                        "to": "old@invalid",
                        "encryptedAuthCode": original_ciphertext,
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-File",
                    str(temp_path / "Configure-QQMail.ps1"),
                    "-Recipients",
                    "first@invalid;second@invalid",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            response = json.loads(result.stdout.strip())
            self.assertEqual(response["recipientCount"], 2)
            self.assertNotIn("@invalid", result.stdout)
            migrated = json.loads(config_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(migrated["encryptedAuthCode"], original_ciphertext)
            self.assertEqual(migrated["to"], ["first@invalid", "second@invalid"])


if __name__ == "__main__":
    unittest.main()
