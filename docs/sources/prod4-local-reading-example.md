# PROD4: descarga y lectura local comprobadas

Prueba del 2026-10-01 desde el Mac del usuario. No prueba de acceso desde servidor peruano.

## Resultado real

- Expediente `1242359`, `CP SER-SM-14-2026-BCRPLIM-1`, BCRP.
- Ficha API y portal: HTTP 200 desde el Mac.
- Descarga directa con curl normal: HTTP 403, 83 bytes; esa respuesta no es un PDF.
- Chromium con interfaz, apertura de la ficha y clic en su enlace documental visible: HTTP 200 y descarga real, sin login, CAPTCHA, proxy ni cabeceras inventadas.
- Archivo: `CP142026__Bases Suscripciones PAM_20260818_155004_108.pdf`.
- Tamaño: 1 150 692 bytes. SHA256: `fbac24396c0fc5fa4c650d69acdf2f54e36db6117cd247a06cef0eb9f46c0267`.
- `pdfinfo`: 91 páginas, PDF 1.7, no cifrado.
- `pdftotext -layout`: 289 502 caracteres; sección `4. TÉRMINOS DE REFERENCIA`, página 28, revisada también visualmente.
- OCDS `/process?ocid=ocds-dgv273-seacev3-1242359` y `/record/ocds-dgv273-seacev3-1242359?format=json`: HTTP 404. No enlace documental alternativo en esas respuestas.

En este caso el TDR está incluido dentro de las bases administrativas; no hace falta que el archivo se llame TDR.
La metadata interna del PDF conserva un título de plantilla de bienes: usar ficha y contenido, no ese título, para clasificar este servicio.

## Automatización reutilizable

Script: `scripts/probe_prod4_browser.py`. Python con Playwright/Chromium ya instalados y Poppler (`pdfinfo`, `pdftotext`) disponibles.
Inspeccionar primero el DOM y usar solo el enlace oficial observado; `--download-href` exige un único enlace visible.

```sh
python3 scripts/probe_prod4_browser.py \
  --headed \
  --url 'https://prod4.seace.gob.pe/openegocio/#/ficha/idProceso/1242359' \
  --download-href 'https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode=3e125876-d6bb-4c68-be6e-86dc61c00f2e' \
  --extract-text \
  --out /tmp/buenapro-prod4-example
```

Salida: `official-document.pdf`, `official-document.txt`, `report.json` y capturas.
La descarga y el helper de validación/extracción se ejecutaron y verificaron sobre el PDF real.
Un exit code 0 del diagnóstico no demuestra éxito: revisar `download.failure`, `is_pdf`, `extraction.pages`, texto y respuestas de red.
En escaneados, texto vacío/escaso requiere OCR o lectura visual mediante el pipeline; no producir un resumen vacío como éxito.

Evidencia visual: `docs/sources/evidence/prod4-local-20261001/tdr-page28.png`.
PDF y texto de esta prueba permanecen en `/tmp/buenapro-prod4-local.IYDPE8/click/`.

## Qué demuestra y qué falta

Demuestra descarga autorizada por flujo normal de navegador y lectura automatizable desde este Mac para un documento.
No demuestra que todos los concursos funcionen ni que funcione desde la VM; allí el portal dio 403 antes de cargar.
No identifica la regla exacta que explica la diferencia entre curl y navegador.

No se llamó a Gemini, no se asignó puntaje, no se subieron resultados a producción y no se modificó PostgreSQL.
Pendiente integrar cola autenticada, Gemini del worker existente, subida idempotente y borrado de temporales exclusivamente después de confirmar persistencia remota.
No borrar este PDF como si estuviera subido: todavía no lo está.
