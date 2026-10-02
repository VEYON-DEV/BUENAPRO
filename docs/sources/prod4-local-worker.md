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
Por defecto respeta el límite diario remoto de extracciones (actualmente 10).
Para un lote expresamente autorizado, `--daily-limit 30` amplía solo esa ejecución;
no modifica configuración ni límites del worker remoto. El presupuesto es exclusivo de extracción, estimado con reserva
conservadora de retries, no garantía de facturación; matching conserva su propio
presupuesto diario. Los costos persistidos son estimaciones de llamadas exitosas,
no facturas: el estimador actual no suma thinking ni intentos descartados/reintentos.

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
elegible para matching, costo estimado registrado de extracción USD `0.014170`; SHA verificado después
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
enviar a Gemini. Coste estimado registrado total de extracción: USD `0.123770`, sin incluir
matching. Se alcanzó el límite configurado de 10 extracciones diarias; quedan
18 documentos elegibles pendientes y los 2 grandes para revisión de política.
El documento `1254310` quedó con `requires_human_review=true`; no generó puntaje
final automático. Los otros 9 encolaron el matching normal de producción.
La lectura final de PostgreSQL confirmó 27 matching persistidos (3 perfiles por
cada uno de esos 9 documentos), con resumen personalizado, score, verdict y gaps.

## Continuación autorizada y PDFs grandes

El 2026-10-02 el usuario solicitó terminar el lote. Se habilitó una ampliación
puntual de cantidad manteniendo el presupuesto de extracción. Para los PDFs grandes:

```sh
.venv/bin/python -m buenapro_worker.local_prod4 \
  --ssh-host ubuntu@168.129.177.199 \
  --ssh-key credentials/ssh-key-2026-07-14.key \
  --execute --process-id 1252648 --limit 1 --daily-limit 30 \
  --max-pdf-bytes 50000000 --max-cost-usd 0.4
```

La ampliación de tamaño también es exclusivamente local. PDFs mayores a 18 MiB
utilizan Files API: se sube el original completo, se reutiliza en conteo y retries,
y se elimina el archivo temporal remoto al finalizar. No se recortan páginas,
no se reduce calidad y SHA corresponde al original. Hasta 100 MB locales, un
original mayor a 50 MB se separa en partes contiguas de hasta 45 MB, sin omitir
páginas ni recomprimir imágenes. Todas las partes se envían juntas para una sola
extracción, indicando offsets de página; SHA sigue siendo el del original.
Google documenta PDFs de hasta 50 MB y 1000 páginas:
[documentación oficial](https://ai.google.dev/gemini-api/docs/document-processing).

El modelo usado en los primeros diez fue `gemini-3.1-flash-lite`: 420280 tokens
de entrada y 12466 de salida; USD 0.123770 de extracción y USD 0.0400785 de matching
registrados, total estimado USD 0.1638485. Unos USD 0.012377 por PDF de extracción,
no USD 0.124 por PDF. Tarifas contrastadas con
[Google](https://ai.google.dev/gemini-api/docs/pricing).

## Lote completo verificado

El 2026-10-02 quedaron 30 extracciones actuales, ninguna pendiente de revisión,
y 88 evaluaciones actuales vinculadas a esas versiones. En el proceso 1250487
solo un perfil alcanza afinidad mínima; los otros dos no se evalúan. Todos los
jobs de matching terminaron. El inventario no debe confundirse con todo SEACE:
solo incluye documentos oficiales elegibles de bienes/servicios tecnológicos.

Se corrigió el esquema de bases de bienes para exigir `goods.items`, sin inventar
productos: cuatro extracciones se repitieron con versión v2 y se conservaron las
versiones anteriores. Los tres PDF grandes se procesaron completos; el de
57 969 912 bytes/128 páginas se dividió en páginas 1–97 y 98–128. Renderizados de
las páginas 1, 97, 98 y 128 coinciden byte a byte con el original.

Estimaciones registradas: USD 0.431640 en las 30 extracciones actuales;
USD 0.483215 incluyendo las cuatro versiones sustituidas; USD 0.12983675 en
las 88 evaluaciones actuales. No es una factura ni un ledger de todos los
intentos: excluye thinking, llamadas descartadas y evaluaciones sustituidas.
Los temporales locales verificados fueron eliminados, no los PDF de SEACE.
Validación: 103 pruebas del worker pasando.

Para enrutar un lote autorizado ya extraído hacia los workers normales:

```sh
.venv/bin/python -m buenapro_worker.local_prod4 \
  --ssh-host ubuntu@168.129.177.199 \
  --ssh-key credentials/ssh-key-2026-07-14.key \
  --execute --route-matches --limit 30 --profile-daily-limit 40
```

La ampliación del límite por perfil solo afecta esta ejecución; las reglas
explícitas de cada empresa prevalecen. No se modifica el límite remoto de 10.
`--retry-review --process-id ID` permite reintentar un caso de revisión concreto;
no borra historia ni desactiva las validaciones.
