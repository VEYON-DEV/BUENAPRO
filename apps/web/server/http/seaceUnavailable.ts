import { NextResponse } from "next/server";

/**
 * Respuestas cuando la salida a SEACE no está disponible (pausa, turno ocupado, proxy caído).
 * Se usa un 503 propio en vez del 502 que Cloudflare reemplaza con su página.
 */

const HEADERS = { "Cache-Control": "no-store", "Retry-After": "600" };

function escapeAttribute(value: string) {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

/** Página para el visor o la pestaña del usuario: ofrece abrir el documento directamente en SEACE. */
export function seaceUnavailablePage(originalHref: string) {
  const html = `<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Vista previa no disponible</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f4f2;color:#191c20;font:15px/1.5 system-ui,sans-serif}main{max-width:360px;padding:24px;text-align:center}h1{font-size:17px;margin:0 0 8px}p{margin:0 0 18px;color:#555}a{display:inline-block;background:#191c20;color:#fff;border-radius:8px;padding:9px 14px;font-weight:700;text-decoration:none}</style></head>
<body><main><h1>Vista previa no disponible</h1><p>SEACE no respondió en este momento. Puedes abrir el documento directamente en SEACE.</p><a href="${escapeAttribute(originalHref)}" target="_blank" rel="noopener noreferrer">Abrir en SEACE</a></main></body></html>`;
  return new NextResponse(html, { status: 503, headers: { ...HEADERS, "Content-Type": "text/html; charset=utf-8" } });
}

/** Respuesta JSON para las rutas que consume la interfaz con `apiFetch`, que muestra `error`. */
export function seaceUnavailableJson(message: string) {
  return NextResponse.json({ code: "SEACE_UNAVAILABLE", error: message }, { status: 503, headers: HEADERS });
}
