from __future__ import annotations

import smtplib
from email.message import EmailMessage

import httpx

from buenapro_worker.settings import Settings


def send_email(settings: Settings, to_email: str, subject: str, body: str) -> None:
    if not settings.smtp_host or not settings.email_from:
        raise ValueError("SMTP settings are required for email notifications")
    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


def send_telegram(settings: Settings, chat_id: str, text: str, *, bot_token: str | None = None) -> None:
    token = bot_token or settings.telegram_bot_token
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    response = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    response.raise_for_status()
