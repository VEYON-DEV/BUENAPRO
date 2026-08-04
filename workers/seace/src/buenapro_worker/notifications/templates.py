from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


LIMA = ZoneInfo("America/Lima")
MODALITY_LABELS = {
    "virtual": "Virtual",
    "presencial": "Presencial",
    "mixta": "Mixta",
    "no_indicada": "No indicada",
}


def _modality_label(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in MODALITY_LABELS:
        return MODALITY_LABELS[text]
    has_virtual = any(part in text for part in ("virtual", "remot", "distancia"))
    has_presential = any(part in text for part in ("presencial", "in situ", "campo"))
    if "mixt" in text or (has_virtual and has_presential):
        return "Mixta"
    if has_virtual:
        return "Virtual"
    if has_presential:
        return "Presencial"
    return "No indicada"


def _clip(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    head = text[: limit - 1].rsplit(" ", 1)[0]
    return f"{head or text[: limit - 1]}…"


def _deadline(value: object) -> str:
    if not value:
        return "No indicada"
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LIMA)
    return dt.astimezone(LIMA).strftime("%d/%m/%Y, %I:%M %p").replace("AM", "a. m.").replace("PM", "p. m.")


def build_notification_payload(data: dict) -> dict:
    score = int(data.get("score") or 0)
    verdict = str(data.get("verdict") or "gris")
    verdict_label = {
        "verde": "Sí conviene",
        "ambar": "Vale la pena revisarla",
        "gris": "Requiere revisión",
        "rojo": "No conviene",
    }.get(verdict, "Evaluada")
    verdict_icon = {"verde": "🟢", "ambar": "🟡", "gris": "⚪", "rojo": "🔴"}.get(verdict, "🔎")
    modality = _modality_label(data.get("modalidad"))
    summary = _clip(data.get("descripcion_corta") or data.get("descripcion"), 220)
    strengths = [_clip(item, 100) for item in (data.get("fortalezas") or []) if item][:2]
    risk = _clip(data.get("riesgo_principal"), 130)
    url = f"{str(data.get('app_base_url') or '').rstrip('/')}/oportunidad/{data['id_contrato']}"

    lines = [
        f"{verdict_icon} BuenaPro · {verdict_label}",
        "",
        f"{score}/100 · {_clip(data.get('codigo'), 64)}",
        _clip(data.get("descripcion_corta") or data.get("descripcion"), 120),
        "",
        "Qué necesitan:",
        summary or "Revisa el detalle de la oportunidad.",
        "",
        f"📍 Modalidad: {modality}",
        f"Entidad: {_clip(data.get('entidad_nombre') or 'No indicada', 100)}",
        f"⏰ Cierre: {_deadline(data.get('fec_fin_cotizacion'))}",
    ]
    if strengths:
        lines.extend(["", "A favor:", *[f"• {item}" for item in strengths]])
    if risk:
        lines.extend(["", "Revisar:", risk])
    lines.extend(["", f"Ver oportunidad: {url}"])

    return {
        "subject": f"BuenaPro: {score}/100 · {verdict_label}",
        "body": "\n".join(lines),
        "id_contrato": data["id_contrato"],
        "codigo": data.get("codigo"),
        "title": _clip(data.get("descripcion"), 140),
        "descripcion": _clip(data.get("descripcion"), 240),
        "entidad_nombre": _clip(data.get("entidad_nombre"), 120),
        "score": score,
        "verdict": verdict,
        "summary": summary,
        "modality": modality,
        "strengths": strengths,
        "main_risk": risk or None,
        "deadline": data.get("fec_fin_cotizacion").isoformat() if isinstance(data.get("fec_fin_cotizacion"), datetime) else data.get("fec_fin_cotizacion"),
        "url": url,
    }
