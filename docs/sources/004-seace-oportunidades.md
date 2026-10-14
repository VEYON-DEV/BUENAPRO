# 004 - SEACE Oportunidades de Negocio (`prod4/openegocio`)

## Estado

```yaml
status: verified
owner: pendiente
country: PE
last_verified_at: 2026-09-22
verified_by: Codex + subagente Luna xhigh
adapter_key: seace_prod4_opportunities
```

## Alcance

Este portal publica oportunidades de negocio vigentes de SEACE. Es una vista
operativa y ligera para descubrir procesos cuyo periodo de participación o
presentación todavía está abierto. Incluye bienes, servicios, obras y
consultorías de obra, junto con ítems CUBSO, cronograma y bases.

No es una fuente histórica: no reemplaza a OCDS para adjudicaciones, contratos
o backfill. Tampoco es una plataforma con concursos distintos de SEACE. Un
registro de PROD4 debe enriquecerse y reconciliarse con el mismo procedimiento
en OCDS y, solo cuando falte información operativa, con PROD2.

## URLs oficiales

| Uso | URL | Verificada |
| --- | --- | --- |
| Portal | `https://prod4.seace.gob.pe/openegocio/#/georeferenciacion/` | sí |
| Backend público | `https://prod4.seace.gob.pe:8086/api/oportunidades` | sí |
| Página Gob.pe | `https://www.gob.pe/703-acceder-a-oportunidades-de-negocio-con-el-estado` | sí |
| Descarga de documentos | `https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode={uuid}` | sí, requiere cabeceras de navegador/referer |
| Términos/licencia específicos del API | no publicados en la SPA | pendiente de revisión legal |

## Acceso y permisos

```yaml
access_mode: public
authentication: none_for_read_endpoints
index_allowed: pending_legal_review
store_metadata_allowed: pending_legal_review
store_documents_allowed: pending_legal_review
redistribute_allowed: pending_legal_review
ai_processing_allowed: pending_legal_review
legal_reviewed_at: pending
```

Los endpoints de lectura probados no exigieron cookie, token ni CAPTCHA. El
endpoint genérico `/findAll` sí respondió `401`; no debe intentarse evadir esa
protección. La SPA también contiene funciones para cuentas y suscripciones,
pero quedan fuera del adaptador público.

## Capacidades

| Capacidad | Disponible | Mecanismo |
| --- | --- | --- |
| Descubrimiento | sí | listado completo o filtros por objeto, departamento y texto |
| Detalle | sí | ficha por `idProcedimiento` |
| Documentos | sí | UUID Alfresco devuelto en lista y detalle |
| Cronograma | sí | `listaCronograma` de la ficha |
| Ítems/CUBSO | sí | filas del listado, `listaItems` y catálogo por segmento |
| Modificaciones | parcial | volver a consultar y versionar por hash; no hay feed de cambios |
| Preguntas/respuestas | no observado | usar PROD2 cuando sea imprescindible |
| Adjudicaciones | no | usar OCDS |
| Contratos | no | usar OCDS/portal de contratos |
| Planes futuros | no | usar PAC y anuncios futuros de PROD2 |
| Backfill histórico | no | usar OCDS/Datos Abiertos |

## Catálogos observados en la SPA

### Objeto

| Código | Objeto |
| --- | --- |
| `62` | Bienes |
| `63` | Consultoría de obras |
| `64` | Obras |
| `65` | Servicios |
| `0` | Todos |

### Tipo de proceso

| Código | Tipo mostrado |
| --- | --- |
| `271` | Adjudicación Simplificada |
| `82` | Licitación Pública |
| `75` | Concurso Público |
| `384` | Subasta Inversa Electrónica |
| `511` | Procedimiento Especial de Contratación |
| `0` | Todos |

El filtro por tipo no es confiable hoy: varios registros devuelven
`codTipoProceso: null`. El proceso de prueba era un Concurso Público, pero la
ruta con código `75` devolvió cero resultados. Conviene filtrar localmente por
`detTipoProceso` después de obtener los datos.

Los departamentos usan códigos `01` a `25`; `15` corresponde a Lima y `0` a
todos. Aunque algunos registros traen un `ubigeo` de cuatro dígitos como
`1500`, los filtros y `countDepObj` esperan `15`; `1500` devolvió cero.

## Endpoints probados

Base: `https://prod4.seace.gob.pe:8086/api/oportunidades`

| Método | Endpoint | Propósito | Auth | Resultado del 2026-09-22 |
| --- | --- | --- | --- | --- |
| GET | `/count` | cantidad de filas vigentes | no | `200`, `numProceso=3130` |
| GET | `/codObjeto/{codObjeto}` | listado por objeto | no | `200` |
| GET | `/codObjeto/codDepartamento/sintesisProceso/codTipoProceso/{objeto}/{departamento}/{texto}/{tipo}` | búsqueda combinada | no | `200` |
| GET | `/countDepObj/{objeto}/{departamento}` | conteo por objeto/departamento | no | `200`; servicios/Lima=`572` |
| GET | `/countDepObj/idPersonaTotal/{objeto}/{departamento}/{idPersona}` | conteo personalizado | desconocida | fuera del adaptador público |
| GET | `/fichaProceso/idProceso/{idProcedimiento}` | detalle, cronograma, entidad, ítems y documentos | no | `200` |
| GET | `/listaSegmentoCubso` | segmentos CUBSO con conteos | no | `200` |
| GET | `/listaProcesosCubso/codigoSegmento/{segmento}` | oportunidades por segmento CUBSO | no | `200`; segmento `43` |
| GET | `/listado/codigoObjeto/{objeto}/{termino}` | sugerencias de familia/clase CUBSO | no | `200`; servicios/`SEGUROS` |
| GET | `/georeferenciacion/codDepartamento/{departamento}` | datos para el mapa | no | `500` para `01`, `15`, `16` y `25`; no usar |
| GET | `/codObjeto/codDepartamento/idPersona/{objeto}/{departamento}/{idPersona}` | resultados personalizados | desconocida | `200` vacío sin identidad; fuera de alcance |
| GET | `/findAll` | listado interno | sí | `401 unauthorized`; no usar |

No se observó paginación en los listados públicos: el servidor devuelve un
arreglo completo. El adaptador debe usar una sola consulta por objeto y hacer
deduplicación local, sin multiplicar consultas por departamento o palabra.

## Ejemplos

### Listar servicios vigentes

```bash
curl --fail-with-body \
  'https://prod4.seace.gob.pe:8086/api/oportunidades/codObjeto/65'
```

### Buscar seguros en servicios de Lima

El literal `0` significa «sin filtro» para texto, tipo y departamento.

```bash
curl --fail-with-body \
  'https://prod4.seace.gob.pe:8086/api/oportunidades/codObjeto/codDepartamento/sintesisProceso/codTipoProceso/65/15/SEGUROS/0'
```

### Recuperar una ficha

```bash
curl --fail-with-body \
  'https://prod4.seace.gob.pe:8086/api/oportunidades/fichaProceso/idProceso/1249681'
```

Respuesta abreviada y sanitizada:

```json
{
  "nidExpediente": 1249681,
  "idConvocatoriaPub": 1042254,
  "numeroProcedimiento": "1175880",
  "nomenclatura": "CP SER-SM-6-2026-ATU-1",
  "tipoObjeto": "Servicio",
  "fechaPublicacion": "11/09/2026 20:30",
  "listaCronograma": [
    {
      "nombreEtapa": "PRESENTACIÓN DE PROPUESTAS",
      "fechaInicio": "14/10/2026",
      "horaInicio": "00:01",
      "horaFin": "23:59"
    }
  ],
  "listaDocumentos": [
    {
      "tipoDocumento": "Bases Administrativas",
      "extension": "pdf",
      "codigoAlfresco": "77dcd97b-9e96-4f75-901a-92143f60cf23"
    }
  ]
}
```

## Identificadores y deduplicación

| Entidad | Campo nativo | Estabilidad | Uso canónico |
| --- | --- | --- | --- |
| Procedimiento | `idProcedimiento` / `nidExpediente` | alta | `source_record.external_id` y enlace a OCDS |
| Convocatoria pública | `idConvocatoriaPub` | alta | alias para navegar PROD2 |
| Número interno | `numeroProcedimiento` | media/alta | alias de conciliación, no PK |
| Nomenclatura | `nomenclatura` | alta pero puede repetirse en reinicios | clave secundaria |
| Ítem | `idProcedimiento + nroItem` | media/alta | `external_lot_id`; `idItem` suele ser nulo |
| Documento | `codigoAlfresco` | alta | `external_document_id` y URL de descarga |
| Comprador | `entidadConvocante.idOrganismo` | alta dentro de SEACE | mapear a organización; enriquecer RUC desde OCDS |

El listado tiene una fila por ítem, no una fila por procedimiento. El
2026-09-22 devolvió `3130` filas pero solo `2594` valores distintos de
`idProcedimiento`. Nunca insertar cada fila como una oportunidad diferente.

Regla de unión recomendada:

1. `PROD4.idProcedimiento == OCDS.tender.id` y el OCID termina en el mismo ID.
2. `PROD4.idConvocatoriaPub` coincide con el identificador usado por la ficha
   de PROD2.
3. Confirmar `nomenclatura`, comprador y año; conservar procedencia por campo.
4. Los ítems se insertan debajo de la oportunidad, usando `nroItem`.

## Cotejo real entre las tres superficies

La convocatoria vigente elegida al azar para la prueba fue:

| Campo | PROD4 | PROD2 | OCDS |
| --- | --- | --- | --- |
| Procedimiento | `1249681` | `nidProceso=1249681` en la fila | `tender.id=1249681` |
| Convocatoria | `idConvocatoriaPub=1042254` | detalle usa `nidProceso=1042254` | incluida en el record compilado |
| Nomenclatura | `CP SER-SM-6-2026-ATU-1` | igual | igual |
| Comprador | ATU Lima y Callao | igual | igual, `PE-CONSUCODE-204448` |
| Publicación | `11/09/2026 20:30` | igual | `2026-09-11T20:30:00-05:00` |
| Documento | UUID `77dcd...cf23` | ficha/bases | misma URL en `tender.documents` |

OCDS se comprobó en:

```text
https://contratacionesabiertas.oece.gob.pe/api/v1/record/ocds-dgv273-seacev3-1249681?format=json
```

Resultado: es el mismo concurso en las tres plataformas. PROD4 aporta un
descubrimiento público mucho más barato que automatizar PROD2; OCDS aporta el
record histórico/normalizado; PROD2 queda como fallback de detalle cuando
OCDS/PROD4 no contengan un dato necesario.

## Mapeo canónico

| Campo canónico | Campo fuente | Regla | Confianza |
| --- | --- | --- | --- |
| `title` | `nomenclatura` | conservar texto | alta |
| `description` | `sintesisProceso` / `descripcionObjeto` | preferir detalle | alta |
| `object_type` | `codObjeto` / `tipoObjeto` | `62/63/64/65` | alta |
| `procedure_type` | `detTipoProceso` | no confiar en `codTipoProceso` nulo | media/alta |
| `stage` | fechas y `listaCronograma[].estadoRegistro` | calcular contra zona `America/Lima` | media |
| `participation_access` | cronograma y `descTipoSeleccion` | electrónica cuando corresponda | alta |
| `buyer` | `entidadConvocante` | unir con OCDS para RUC | alta |
| `published_at` | `fechaConvocatoria` / `fechaPublicacion` | parsear `dd/MM/yyyy HH:mm` | alta |
| `closes_at` | `fecFinParticipantes` / etapa de propuestas | guardar ambos conceptos por separado | alta |
| `amount` | `valorReferencial` | `---` se almacena como nulo | media |
| `currency` | `monedaProceso` | normalizar Soles a `PEN` | alta |

## Estados y lifecycle

PROD4 no expone un estado global estable. Sus listados representan la ventana
vigente y contienen fechas de registro, presentación y etapas en la ficha. El
worker debe:

1. calcular la etapa actual a partir de `listaCronograma` en `America/Lima`;
2. versionar cualquier cambio de fecha o documento;
3. marcar como salida de la ventana PROD4 cuando deje de aparecer, sin asumir
   cancelación ni adjudicación;
4. consultar OCDS para conocer adjudicación/contrato y PROD2 solo como fallback.

## Documentos

La ficha devuelve nombre, tipo, extensión, fecha, etapa, tamaño declarado y
`codigoAlfresco`. La descarga de prueba respondió `403` con un cliente HTTP
vacío y `200 application/pdf` al enviar `User-Agent` de navegador y un
`Referer` oficial. El PDF probado pesó `57,230,336` bytes.

No debe usarse un servicio de CAPTCHA. Basta una descarga HTTP conservadora con
cabeceras normales, validación del MIME real, límite de tamaño, hash SHA-256,
backoff y enlace al original. La licencia de almacenamiento/redistribución del
PDF sigue pendiente de revisión legal.

## Histórico

El endpoint está diseñado para oportunidades vigentes y no acepta año ni
paginación histórica. Para los últimos tres años se debe usar OCDS y Datos
Abiertos; PROD4 solo sirve para el polling actual.

## Rate limits, cache y errores

- No se observaron cabeceras de rate limit ni `ETag`/`Last-Modified`.
- El servidor responde `Cache-Control: no-cache, no-store`.
- `/findAll` responde `401`; no usarlo.
- Un `idProcedimiento` inexistente devuelve `500` Hibernate en lugar de `404`;
  tratarlo como registro no disponible y no reintentar indefinidamente.
- `/georeferenciacion/...` respondió `500 SQLGrammarException`; usar el listado
  filtrado normal y no reintentar agresivamente.
- CORS permite el origen oficial `https://prod4.seace.gob.pe`; un origen
  arbitrario respondió `403`. La ingesta debe ejecutarse server-side, no desde
  el navegador de BuenaPro.
- No hay paginación; una consulta por objeto puede superar mil filas.
- Frecuencia inicial recomendada: cada 30 minutos, con jitter, timeout, backoff
  exponencial y un máximo de una consulta simultánea por fuente.
- Aplicar circuit breaker ante cambios de esquema o tres errores consecutivos.

## Pruebas ejecutadas

| Prueba | Resultado | Evidencia | Fecha |
| --- | --- | --- | --- |
| Healthcheck | OK | `/count` → `200`, 3130 filas | 2026-09-22 |
| Bienes | OK | objeto `62` → 1220 filas, 954 procesos distintos | 2026-09-22 |
| Servicios | OK | objeto `65` → 1057 filas, 818 procesos distintos | 2026-09-22 |
| Obras | OK | objeto `64` → 510 filas | 2026-09-22 |
| Consultoría de obras | OK | objeto `63` → 343 filas | 2026-09-22 |
| Filtro Lima/seguros | OK | objeto `65`, dpto. `15`, texto `SEGUROS` → 104 filas | 2026-09-22 |
| Detalle | OK | ficha `1249681`, cronograma completo | 2026-09-22 |
| Documento | OK con cabeceras | PDF válido de 57,230,336 bytes | 2026-09-22 |
| CUBSO | OK | segmentos, segmento `43` y sugerencias `SEGUROS` | 2026-09-22 |
| Cruce PROD2 | OK | fila exacta e IDs `1249681`/`1042254` | 2026-09-22 |
| Cruce OCDS | OK | record `ocds-dgv273-seacev3-1249681` | 2026-09-22 |
| Paginación | no aplica | el API devuelve arreglos completos | 2026-09-22 |
| Mapa | fallo de fuente | `500` en varios departamentos | 2026-09-22 |

## Limitaciones y riesgos

- API no documentada formalmente y susceptible a cambios.
- Sin paginación, historial ni feed incremental.
- Una oportunidad se repite por cada ítem.
- El código de tipo de proceso aparece nulo en registros reales.
- En `/listaProcesosCubso/...`, `codObjeto` puede contener en realidad un ID de
  ítem; clasificar con el listado principal o la ficha.
- `listaItems[].montoAdjundicado` puede contener un identificador de ítem aun
  sin adjudicación; no interpretarlo como importe.
- El endpoint del mapa está roto al momento de la prueba.
- Montos reservados aparecen como `---` y deben convertirse a nulo.
- La ausencia posterior de PROD4 no prueba cancelación/adjudicación.
- Fechas vienen sin offset; interpretar en `America/Lima`.

## Implementación

```yaml
adapter_path: workers/seace/src/buenapro_worker/prod4/client.py
polling_path: workers/seace/src/buenapro_worker/jobs/poll_prod4.py
tests_path: workers/seace/tests/test_prod4_poll.py
polling_frequency: 30m
backfill_status: not_applicable_use_ocds
production_enabled: true
```

El worker consulta los listados nativos `62` (bienes) y `65` (servicios), y
cruza los IDs de proceso con los segmentos CUBSO configurados (`43,81`). El
segmento `81` también contiene ingeniería y consultoría de obra; por eso solo
se aceptan servicios con familias CUBSO TI explícitas y configurables mediante
`PROD4_SERVICE_CUBSO_PREFIXES` (por defecto `811115`–`811125`). Los prefijos
se complementan con una señal textual TI en título o ítem mediante
`PROD4_TECHNOLOGY_TERMS`. Esto elimina falsos positivos observados, como
equipos biomédicos, seguridad física y verificación documentaria. Ambas listas
pueden ajustarse sin tocar la lógica del worker. Una coincidencia en un solo
ítem vuelve relevante al procedimiento completo; se guardan todos sus ítems
bajo un único `idProcedimiento`.

Las fichas se recuperan solo para procesos nuevos o cuyo listado cambió, hasta
`PROD4_DETAIL_LIMIT` por ciclo. El valor inicial de `100` permite hidratar la
primera ventana tecnológica y las consultas a ficha tienen una pausa de
120 ms. No se descarga el PDF ni se lanza Gemini masivamente. La fecha de
`fecFinParticipantes` se guarda como cierre del registro; el cierre de
propuestas procede exclusivamente de `listaCronograma`.

Un snapshot solo se aplica si sus cuatro listados responden con forma válida.
Si la cantidad seleccionada cae por debajo de la mitad de la ventana previa,
se rechaza por seguridad. La ausencia posterior marca `missing_since`; no
modifica por sí sola la etapa canónica ni demuestra adjudicación, cancelación o
contrato. El histórico/adjudicación seguirá viniendo de OCDS. El primer polling
de prueba obtuvo 78 candidatos CUBSO; el filtro textual afinado dejó 72
procesos en el radar de producción (24 bienes y 48 servicios) el 2026-09-29.
La última corrida fue idempotente: 0 fichas y 0 nuevas salidas de ventana.

Comando manual de comprobación tras aplicar la migración `0029`:

```bash
python -m buenapro_worker.cli prod4-poll-once
```

## Próxima acción

Activar el worker en producción después de aplicar `0029` y verificar la API
web. El enlace exacto a OCDS queda pendiente para IDs que todavía no tengan
record publicado; no se deduplica por nombre o monto. `prod2` sigue siendo
fallback selectivo, no parte del polling masivo.
