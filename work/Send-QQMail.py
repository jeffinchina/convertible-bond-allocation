from __future__ import annotations

import json
import base64
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def sanitize_error_text(value: object, secrets: tuple[str, ...] = ()) -> str:
    text = EMAIL_PATTERN.sub("<redacted-email>", str(value))
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted-secret>")
    return text


def build_message(request: dict, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = request["from"]
    message["To"] = ", ".join(request["recipients"])
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="qq.com")
    message["Auto-Submitted"] = "auto-generated"
    subtype = "html" if request.get("isHtml") else "plain"
    message.set_content(body, subtype=subtype, charset="utf-8")
    return message


def main() -> int:
    request = json.load(sys.stdin)
    subject = base64.b64decode(request["subjectB64"]).decode("utf-8")
    body = base64.b64decode(request["bodyB64"]).decode("utf-8")
    auth_code = base64.b64decode(request["authCodeB64"]).decode("utf-8")
    message = build_message(request, subject, body)
    refused_recipients = {}

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(request["server"], int(request["port"]), timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(request["username"], auth_code)
            refused_recipients = smtp.send_message(message)
            if refused_recipients:
                raise RuntimeError(f"SMTP 拒绝了 {len(refused_recipients)} 个收件人")
    except smtplib.SMTPAuthenticationError as exc:
        detail = exc.smtp_error.decode("utf-8", errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        detail = sanitize_error_text(detail, (auth_code,))
        print(json.dumps({"ok": False, "errorType": type(exc).__name__, "smtpCode": exc.smtp_code, "error": detail}, ensure_ascii=True))
        return 1
    except Exception as exc:
        detail = sanitize_error_text(exc, (auth_code,))
        print(json.dumps({"ok": False, "errorType": type(exc).__name__, "error": detail}, ensure_ascii=True))
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "serverAccepted": True,
                "messageId": str(message["Message-ID"]),
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
