from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from buenapro_worker.notifications.channels import send_email, send_telegram
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.settings import Settings


logger = logging.getLogger(__name__)


def verdict_meets_threshold(verdict: str | None, minimum: str | None) -> bool:
    rank = {"gris": 0, "rojo": 0, "ambar": 1, "verde": 2}
    current_rank = rank.get(str(verdict or "").lower(), 0)
    minimum_rank = rank.get(str(minimum or "verde").lower(), 2)
    return current_rank >= minimum_rank


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
        repo.conn.execute(
            "UPDATE notifications SET status = 'suppressed' WHERE id = %s",
            (notification_id,),
        )
        return "suppressed"

    payload = notification["payload"] or {}
    subject = payload.get("subject") or "Nueva oportunidad BuenaPro"
    body = payload.get("body") or str(payload)

    channel = notification["channel"]
    if channel == "email":
        send_email(settings, notification["email"], subject, body)
    elif channel == "telegram":
        chat_id = payload.get("chat_id")
        if not chat_id:
            raise ValueError("Telegram notification requires payload.chat_id")
        send_telegram(settings, str(chat_id), body)
    elif channel == "in_app":
        pass
    else:
        raise ValueError(f"Unsupported channel: {channel}")

    repo.conn.execute(
        """
        UPDATE notifications
        SET status = 'sent', sent_at = now()
        WHERE id = %s
        """,
        (notification_id,),
    )
    logger.info("notification_sent", extra={"notification_id": notification_id, "channel": channel})
    return "sent"


def enqueue_match_notifications(repo: JobRepository, *, match_id: int, reason: str) -> int:
    rows = repo.conn.execute(
        """
        SELECT DISTINCT ON (u.id,p.channel)
          u.id AS user_id,p.channel,p.min_verdict,m.verdict,m.score,
          c.id_contrato,c.codigo,c.descripcion,c.fec_fin_cotizacion
        FROM matches m
        JOIN company_profiles cp ON cp.id = m.profile_id
        JOIN seace_contracts c ON c.id_contrato=m.id_contrato
        JOIN tenant_members tm ON tm.tenant_id = cp.tenant_id
        JOIN users u ON u.id = tm.user_id
        JOIN notification_preferences p ON p.user_id = u.id
          AND p.enabled = true
          AND p.business_line_id IS NULL
        WHERE m.id = %s
        ORDER BY u.id,p.channel,p.updated_at DESC
        """,
        (match_id,),
    ).fetchall()
    count = 0
    for row in rows:
        if not verdict_meets_threshold(row["verdict"], row["min_verdict"]):
            continue
        score = int(round(float(row["score"] or 0)))
        payload = {
            "subject": f"BuenaPro: {score}% de afinidad",
            "body": f"{row['codigo']}: {row['descripcion']}",
            "id_contrato": row["id_contrato"],
            "codigo": row["codigo"],
            "score": score,
            "verdict": row["verdict"],
        }
        inserted = repo.conn.execute(
            """
            INSERT INTO notifications (user_id,match_id,channel,reason,payload)
            SELECT %s,%s,%s,%s,%s::jsonb
            WHERE NOT EXISTS (
              SELECT 1 FROM notifications
              WHERE user_id=%s AND match_id=%s AND channel=%s AND reason=%s
            )
            RETURNING id
            """,
            (
                row["user_id"],
                match_id,
                row["channel"],
                reason,
                json.dumps(payload),
                row["user_id"],
                match_id,
                row["channel"],
                reason,
            ),
        ).fetchone()
        if inserted:
            repo.enqueue(
                "send_notification",
                {"notification_id": inserted["id"]},
                queue_name="notify",
                dedup_key=f"send_notification:{inserted['id']}",
                priority=5,
            )
            count += 1
    return count


def _exceeds_daily_limit(repo: JobRepository, notification) -> bool:
    pref = repo.conn.execute(
        """
        SELECT max_alerts_per_day
        FROM notification_preferences
        WHERE user_id = %s AND channel = %s
        LIMIT 1
        """,
        (notification["user_id"], notification["channel"]),
    ).fetchone()
    max_alerts = int(pref["max_alerts_per_day"]) if pref else 5
    sent_today = repo.conn.execute(
        """
        SELECT count(*) AS total
        FROM notifications
        WHERE user_id = %s
          AND channel = %s
          AND status = 'sent'
          AND created_at >= %s
        """,
        (
            notification["user_id"],
            notification["channel"],
            datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
        ),
    ).fetchone()
    return int(sent_today["total"]) >= max_alerts
