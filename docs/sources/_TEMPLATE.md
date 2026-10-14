# NNN - Nombre de la fuente

## Estado

```yaml
status: research|verified|implemented|backfilled|production
owner: pendiente
country: PE
last_verified_at: pendiente
verified_by: pendiente
adapter_key: pendiente
```

## Alcance

Describir que publica, que no publica y para que tipo de proveedor resulta relevante.

## URLs oficiales

| Uso | URL | Verificada |
| --- | --- | --- |
| Portal | pendiente | no |
| Documentacion | pendiente | no |
| Terminos | pendiente | no |
| Privacidad | pendiente | no |
| Robots | pendiente | no |

## Acceso y permisos

```yaml
access_mode: public|registered|authenticated|invitation_only
authentication: none|api_key|oauth|session|other
index_allowed: pending
store_metadata_allowed: pending
store_documents_allowed: pending
redistribute_allowed: pending
ai_processing_allowed: pending
legal_reviewed_at: pending
```

Documentar condiciones, licencia, restricciones, retencion y cualquier permiso especial. No guardar secretos.

## Capacidades

| Capacidad | Disponible | Mecanismo |
| --- | --- | --- |
| Descubrimiento | pendiente | pendiente |
| Detalle | pendiente | pendiente |
| Documentos | pendiente | pendiente |
| Modificaciones | pendiente | pendiente |
| Preguntas/respuestas | pendiente | pendiente |
| Adjudicaciones | pendiente | pendiente |
| Contratos | pendiente | pendiente |
| Planes futuros | pendiente | pendiente |
| Backfill historico | pendiente | pendiente |

## Endpoints probados

| Metodo | Endpoint | Proposito | Auth | Resultado | Fecha |
| --- | --- | --- | --- | --- | --- |
| GET | pendiente | pendiente | pendiente | pendiente | pendiente |

## Parametros y paginacion

Documentar parametros obligatorios, filtros, page size, cursores, orden, limites de fecha y condicion de terminacion.

## Ejemplos sanitizados

### Request

```bash
# Nunca incluir tokens, cookies ni credenciales reales.
curl --fail-with-body 'https://example.invalid'
```

### Response

```json
{}
```

Los fixtures completos sanitizados deben vivir en el directorio de pruebas del adaptador, no necesariamente en esta guia.

## Identificadores

| Entidad | Campo nativo | Estabilidad | Uso canonico |
| --- | --- | --- | --- |
| Oportunidad | pendiente | pendiente | external_id |
| Lote | pendiente | pendiente | external_lot_id |
| Documento | pendiente | pendiente | external_document_id |
| Comprador | pendiente | pendiente | organization identifier |

## Mapeo canonico

| Campo canonico | Campo fuente | Regla | Confianza |
| --- | --- | --- | --- |
| title | pendiente | pendiente | pendiente |
| description | pendiente | pendiente | pendiente |
| object_type | pendiente | pendiente | pendiente |
| procedure_type | pendiente | pendiente | pendiente |
| stage | pendiente | pendiente | pendiente |
| participation_access | pendiente | pendiente | pendiente |
| buyer | pendiente | pendiente | pendiente |
| published_at | pendiente | pendiente | pendiente |
| closes_at | pendiente | pendiente | pendiente |
| amount | pendiente | pendiente | pendiente |
| currency | pendiente | pendiente | pendiente |

## Estados y lifecycle

Enumerar estados nativos, transiciones observadas y su correspondencia canonica. Indicar como se detectan prorrogas, cancelaciones, adjudicaciones y reaperturas.

## Documentos

Documentar categorias, MIME, nombres, versionado, descarga, expiracion de URLs, permisos de almacenamiento y estrategia de extraccion.

## Historico

Indicar primer anio disponible, particionamiento, cantidad esperada, estrategia de checkpoint y limitaciones.

## Rate limits, cache y errores

Documentar limites conocidos, `Retry-After`, `ETag`, `Last-Modified`, timeouts, errores frecuentes y politica de backoff.

## Pruebas ejecutadas

| Prueba | Resultado | Evidencia | Fecha |
| --- | --- | --- | --- |
| Healthcheck | pendiente | pendiente | pendiente |
| Primera pagina | pendiente | pendiente | pendiente |
| Siguiente pagina | pendiente | pendiente | pendiente |
| Pagina vacia | pendiente | pendiente | pendiente |
| Detalle | pendiente | pendiente | pendiente |
| Documentos | pendiente | pendiente | pendiente |
| Lifecycle | pendiente | pendiente | pendiente |
| Idempotencia | pendiente | pendiente | pendiente |

## Limitaciones y riesgos

Enumerar cobertura incompleta, campos ambiguos, datos personales, anti-bot, dependencia de sesion, documentos faltantes y riesgos de cambio de schema.

## Implementacion

```yaml
adapter_path: pending
fixtures_path: pending
contract_tests_path: pending
polling_frequency: pending
backfill_status: pending
production_enabled: false
```

## Proxima accion

Definir una accion concreta y verificable.
