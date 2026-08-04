from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from buenapro_worker.notifications.channels import send_email, send_telegram
from buenapro_worker.notifications.templates import build_notification_payload
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.settings import Settings


logger = logging.getLogger(__name__)


def verdict_meets_threshold(verdict: str | None, minimum: str | None) -> bool:
    rank = {"gris": 0, "rojo": 0, "ambar": 1, "verde": 2}
    return rank.get(str(verdict or "").lower(), 0) >= rank.get(str(minimum or "verde").lower(), 2)


def notification_score_passes(score: int | float | None, threshold: int | float | None) -> bool:
    return int(score or 0) > int(threshold if threshold is not None else 50)


def send_notification_job(settings: Settings, repo: JobRepository, *, notification_id: int) -> str:
    notification = repo.conn.execute(
        """
        SELECT n.*, u.email
        FROM notifications n
        JOIN users u ON u.id = n.user_id
        WHERE n.id = %s
        """,
        (notification_id,),
    ).fetchone()
    if notification is None:
        raise ValueError(f"Notification not found: {notification_id}")

    if _exceeds_daily_limit(repo, notification):
        repo.conn.execute("UPDATE notifications SET status = 'suppressed' WHERE id = %s", (notification_id,))
        return "suppressed"

    payload = notification["payload"] or {}
    subject = payload.get("subject") or "Nueva oportunidad BuenaPro"
    body = payload.get("body") or str(payload)
    channel = notification["channel"]

    if channel == "email":
        send_email(settings, notification["email"], subject, body)
    elif channel == "telegram":
        chat_id, bot_token = _telegram_destination(settings, repo, notification)
        send_telegram(settings, chat_id, body, bot_token=bot_token)
    elif channel != "in_app":
        raise ValueError(f"Unsupported channel: {channel}")

    repo.conn.execute(
        "UPDATE notifications SET status = 'sent', sent_at = now() WHERE id = %s",
        (notification_id,),
    )
    logger.info("notification_sent", extra={"notification_id": notification_id, "channel": channel})
    return "sent"


def enqueue_match_notifications(repo: JobRepository, *, match_id: int, reason: str) -> int:
    match = repo.conn.execute(
        """
        SELECT m.id,m.score,m.verdict,m.business_line_id,m.breakdown_json,
          cp.tenant_id,c.id_contrato,c.codigo,c.descripcion,c.entidad_nombre,c.fec_fin_cotizacion,
          COALESCE(ar.min_notification_score, 50) AS min_notification_score,
          tx.summary_json,tx.raw_extraction_json
        FROM matches m
        JOIN company_profiles cp ON cp.id = m.profile_id
        JOIN seace_contracts c ON c.id_contrato = m.id_contrato
        LEFT JOIN automation_rules ar ON ar.profile_id = m.profile_id
        LEFT JOIN LATERAL (
          SELECT te.summary_json,te.raw_extraction_json
          FROM contract_documents d
          JOIN tdr_extractions te ON te.contract_document_id = d.id AND te.is_current = true
          WHERE d.id_contrato = c.id_contrato
          ORDER BY te.created_at DESC LIMIT 1
        ) tx ON true
        WHERE m.id = %s
        """,
        (match_id,),
    ).fetchone()
    if match is None or not notification_score_passes(match["score"], match["min_notification_score"]):
        return 0

    breakdown = match["breakdown_json"] if isinstance(match["breakdown_json"], dict) else {}
    summary = match["summary_json"] if isinstance(match["summary_json"], dict) else {}
    raw = match["raw_extraction_json"] if isinstance(match["raw_extraction_json"], dict) else {}
    raw_contract = raw.get("contract") if isinstance(raw.get("contract"), dict) else {}
    payload = build_notification_payload(
        {
            **dict(match),
            "descripcion_corta": summary.get("descripcion_corta"),
            "modalidad": summary.get("modalidad") or raw_contract.get("modalidad"),
            "fortalezas": breakdown.get("fortalezas") or [],
            "riesgo_principal": breakdown.get("riesgo_principal"),
            "app_base_url": _app_base_url(repo, str(match["tenant_id"])),
        }
    )

    preferences = repo.conn.execute(
        """
        SELECT DISTINCT ON (u.id,p.channel)
          u.id AS user_id,p.channel,p.min_verdict
        FROM tenant_members tm
        JOIN users u ON u.id = tm.user_id
        JOIN notification_preferences p ON p.user_id = u.id AND p.tenant_id = tm.tenant_id
        WHERE tm.tenant_id = %s
          AND p.enabled = true
          AND (p.business_line_id IS NULL OR p.business_line_id = %s)
        ORDER BY u.id,p.channel,p.updated_at DESC
        """,
        (match["tenant_id"], match["business_line_id"]),
    ).fetchall()

    count = 0
    for pref in preferences:
        if not verdict_meets_threshold(match["verdict"], pref["min_verdict"]):
            continue
        if pref["channel"] == "telegram":
            recipients = repo.conn.execute(
                """
                SELECT tr.id
                FROM telegram_integrations ti
                JOIN telegram_recipients tr ON tr.integration_id = ti.id AND tr.enabled = true
                WHERE ti.tenant_id = %s AND ti.enabled = true
                ORDER BY tr.created_at,tr.id
                """,
                (match["tenant_id"],),
            ).fetchall()
            for recipient in recipients:
                count += _insert_notification(
                    repo,
                    user_id=str(pref["user_id"]),
                    match_id=match_id,
                    channel="telegram",
                    reason=reason,
                    payload=payload,
                    telegram_recipient_id=str(recipient["id"]),
                )
        else:
            count += _insert_notification(
                repo,
                user_id=str(pref["user_id"]),
                match_id=match_id,
                channel=str(pref["channel"]),
                reason=reason,
                payload=payload,
            )
    return count


def _insert_notification(
    repo: JobRepository,
    *,
    user_id: str,
    match_id: int,
    channel: str,
    reason: str,
    payload: dict,
    telegram_recipient_id: str | None = None,
) -> int:
    inserted = repo.conn.execute(
        """
        INSERT INTO notifications (
          user_id,match_id,channel,reason,payload,telegram_recipient_id
        )
        SELECT %s,%s,%s,%s,%s::jsonb,%s
        WHERE NOT EXISTS (
          SELECT 1 FROM notifications
          WHERE user_id=%s AND match_id=%s AND channel=%s AND reason=%s
            AND telegram_recipient_id IS NOT DISTINCT FROM %s::uuid
        )
        RETURNING id
        """,
        (
            user_id, match_id, channel, reason, json.dumps(payload, ensure_ascii=False), telegram_recipient_id,
            user_id, match_id, channel, reason, telegram_recipient_id,
        ),
    ).fetchone()
    if not inserted:
        return 0
    repo.enqueue(
        "send_notification",
        {"notification_id": inserted["id"]},
        queue_name="notify",
        dedup_key=f"send_notification:{inserted['id']}",
        priority=5,
    )
    return 1


def _telegram_destination(settings: Settings, repo: JobRepository, notification) -> tuple[str, str | None]:
    recipient_id = notification["telegram_recipient_id"]
    if not recipient_id:
        raise ValueError("Telegram notification has no recipient")
    row = repo.conn.execute(
        """
        SELECT tr.chat_id,ti.id AS integration_id
        FROM telegram_recipients tr
        JOIN telegram_integrations ti ON ti.id = tr.integration_id
        WHERE tr.id = %s AND tr.enabled = true AND ti.enabled = true
        """,
        (recipient_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Telegram destination is disabled or missing")
    token = None
    if settings.settings_encryption_key:
        decrypted = repo.conn.execute(
            "SELECT pgp_sym_decrypt(token_ciphertext,%s) AS token FROM telegram_integrations WHERE id=%s",
            (settings.settings_encryption_key, row["integration_id"]),
        ).fetchone()
        token = str(decrypted["token"]) if decrypted and decrypted["token"] else None
    return str(row["chat_id"]), token


def _app_base_url(repo: JobRepository, tenant_id: str) -> str:
    # Reserved for a tenant-specific public URL later; workers use their configured base today.
    del repo, tenant_id
    return os.getenv("APP_BASE_URL", "http://localhost:3001")


def _exceeds_daily_limit(repo: JobRepository, notification) -> bool:
    pref = repo.conn.execute(
        """
        SELECT max_alerts_per_day FROM notification_preferences
        WHERE user_id = %s AND channel = %s LIMIT 1
        """,
        (notification["user_id"], notification["channel"]),
    ).fetchone()
    max_alerts = int(pref["max_alerts_per_day"]) if pref else 0
    if max_alerts <= 0:
        return False
    sent_today = repo.conn.execute(
        """
        SELECT count(*) AS total FROM notifications
        WHERE user_id = %s AND channel = %s AND status = 'sent' AND created_at >= %s
        """,
        (
            notification["user_id"],
            notification["channel"],
            datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
        ),
    ).fetchone()
    return int(sent_today["total"]) >= max_alerts
