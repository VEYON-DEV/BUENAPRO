# Ejecutar lectura de bases PROD4 desde el Mac

El descargador local utiliza el navegador normal para abrir la ficha y hacer clic
en el enlace Alfresco realmente visible. No usa proxy, stealth, cookies exportadas,
credenciales SEACE ni solucionadores CAPTCHA. Si no aparece el enlace o el portal
deniega acceso, no inventa una descarga alternativa.

## Flujo

1. Túnel SSH temporal `127.0.0.1` al PostgreSQL existente de la VM; no abre puertos.
2. Inventario de bienes/servicios tecnológicos actuales, bases PDF oficiales y
   perfil empresarial relevante, excluyendo documentos ya extraídos.
3. Selección de bases integradas más recientes antes de bases administrativas.
4. Chromium con interfaz descarga el enlace de esa ficha; valida `%PDF`, tamaño
   configurado y `pdfinfo` antes de llamar al modelo.
5. `extract_prod4_document_job` y `GeminiExtractor` existentes leen el documento:
   resumen, requisitos/facets y evidencia. No hay resumen escrito por el agente.
6. Guarda en PostgreSQL remoto, en una transacción, y encola
   `route_prod4_profiles`; los workers de producción hacen matching y explicación
   con el pipeline y límites empresariales existentes.
7. Tras commit y lectura de verificación de ID, SHA y resumen actuales, elimina
   únicamente el PDF temporal de esta ejecución. No elimina la fuente SEACE.

No copia toda la BD. La llave SSH permanece en `credentials/` ignorado y Gemini
usa la llave existente local en `.env.local`. Los parámetros de modelo y límites
PROD4 se leen del contenedor remoto; valores sensibles nunca se imprimen.

## Requisitos

Desde la raíz del proyecto: Python 3.12 del `.venv`, dependencias del worker,
Playwright/Chromium y Poppler (`pdfinfo`). Playwright es una dependencia opcional
del ejecutor local; los workers de producción no necesitan Chromium.

```sh
.venv/bin/python -m pip install -e 'workers/seace[local-prod4]'
.venv/bin/python -m playwright install chromium
```

Inventario sin descargas, Gemini ni escrituras de negocio:

```sh
.venv/bin/python -m buenapro_worker.local_prod4 \
  --ssh-host ubuntu@168.129.177.199 \
  --ssh-key credentials/ssh-key-2026-07-14.key
```

Una oportunidad, o lote controlado sustituyendo `--process-id` por `--limit 9`:

```sh
.venv/bin/python -m buenapro_worker.local_prod4 \
  --ssh-host ubuntu@168.129.177.199 \
  --ssh-key credentials/ssh-key-2026-07-14.key \
  --execute --process-id 1242359 --limit 1 --max-cost-usd 1
```

El valor por defecto es una sola oportunidad y detenerse ante el primer error.
Respeta el límite diario remoto de extracciones (actualmente 10): no fuerza el
lote completo. El presupuesto es exclusivo de extracción, estimado con reserva
conservadora de retries, no garantía de facturación; matching conserva su propio
presupuesto diario. Ver los costos reales persistidos en las extracciones.

Mac encendido y red conectada son necesarios. No hay daemon ni autoarranque aún:
se ejecuta el comando cuando se quiera procesar pendientes. Un servidor peruano
podría ejecutar el mismo módulo solo después de comprobar su acceso real.

## Recuperación

Un documento fallido conserva su directorio exacto dentro de `tmp/prod4-local/`,
ignorado por Git; no se borra sin verificar persistencia. Reejecutar vuelve a
inventariar la BD y omite extracciones existentes; un documento oficial nuevo
puede volver a procesarse. No imprime excepciones potencialmente sensibles.

`verified` confirma extracción durable, no garantiza matching terminado ni que
una empresa cumpla. Una extracción incompleta mantiene revisión humana y afinidad
preliminar; no se fabrica puntaje final. Las bases/TDR no son el cronograma oficial.

## Evidencia real del 2026-10-02

Primero se validó el expediente BCRP `1242359`: extracción remota `1`, 13 facets,
elegible para matching, costo de extracción USD `0.014170`; SHA verificado después
del commit y PDF temporal eliminado. El inventario inicial dio 30 documentos
pendientes elegibles, no 64; la diferencia incluye filtros de perfil relevante,
documentos PDF oficiales y disponibilidad actual.

Validación automatizada: `test_local_prod4.py` + `test_prod4_document_matching.py`,
20 pruebas pasando. Verificar contadores del lote y puntajes finales por separado.

Lote controlado: los documentos `1252648` (aprox. 42 MiB) y `1252548` (aprox.
28 MiB) se descargaron pero no se enviaron al modelo: exceden el límite remoto
de 20 MiB. Se conservan en sus directorios privados locales; el estado remoto es
`pdf_exceeds_analysis_limit`. Revisar explícitamente política de tamaño y coste
antes de reintentar; no elevar el límite ni declarar esos TDR analizados.

Resultado del lote del 2026-10-02: 12 oportunidades intentadas, 10 extracciones
durables verificadas y temporales eliminados, 2 PDFs grandes conservados sin
enviar a Gemini. Coste real total de extracción: USD `0.123770`, sin incluir
matching. Se alcanzó el límite configurado de 10 extracciones diarias; quedan
18 documentos elegibles pendientes y los 2 grandes para revisión de política.
El documento `1254310` quedó con `requires_human_review=true`; no generó puntaje
final automático. Los otros 9 encolaron el matching normal de producción.
La lectura final de PostgreSQL confirmó 27 matching persistidos (3 perfiles por
cada uno de esos 9 documentos), con resumen personalizado, score, verdict y gaps.
