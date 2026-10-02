# Consorcio y subcontratación en LLM1

Las cuatro clases documentales (`tdr`, `eett`, `bases_service`, `bases_good`)
extraen `participation.consorcio` y `participation.subcontratacion`. Cada término
contiene `status`, `clause`, `page` y `conditions`.

| Estado | Evidencia requerida |
| --- | --- |
| `permitted` | Autorización explícita aplicable a la convocatoria, sin condiciones. |
| `prohibited` | Prohibición explícita aplicable a la convocatoria. |
| `conditional` | Autorización con condiciones explícitas, conservadas en `conditions`. |
| `not_identified` | No se identificó una cláusula aplicable con cita literal y página válida. |

`clause` conserva la cita literal y `page` el número de página original del PDF,
contado desde 1. Un anexo genérico de promesa de consorcio, un formato o una
mención normativa genérica no acreditan autorización para esta convocatoria.
La ausencia de una cláusula no se interpreta como permiso ni como prohibición.
No se deducen permisos a partir de normas externas.

La normalización `derive_participation(raw)` recibe el JSON completo de LLM1 y
devuelve ambos términos, incluso para extracciones históricas que no contienen
`participation`. Una clasificación sin cita no vacía y página entera positiva
se degrada a `not_identified`; páginas booleanas, cadenas, cero y decimales son
inválidas. Se conserva la evidencia parcial para revisión, pero no autoriza
participación. `permitted` con condiciones explícitas pasa a `conditional`.
El normalizador no verifica semánticamente una cita contra el PDF: esa revisión
requiere el documento original.

Las versiones vigentes son `tdr_extraction_v3` / `tdr_extraction_schema_v3`,
`eett_extraction_v2` / `eett_extraction_schema_v2`, `bases_extraction_v2` /
`bases_extraction_schema_v2` y `bases_goods_extraction_v3` /
`bases_goods_extraction_schema_v3`. Se conservan los archivos base de prompt;
la instrucción común de participación se añade en `GeminiExtractor`.
El cambio de versión evita reutilizar extracciones anteriores sin estos términos.

## Persistencia y alcance

Migración `0033_participation_terms.sql`: `opportunities.consortium_status` y
`subcontracting_status` usan los cuatro estados con default `not_identified`.
`participation_terms_json` guarda ambos términos y procedencia (fuente, ID de
extracción y SHA del documento). Las filas históricas no se reclasifican por
regex ni por menciones parciales.

PROD6 persiste durante `derive_summary` desde la extracción vigente y vinculada
al contrato; PROD4 persiste en la misma transacción de extracción documental.
Un cambio de bases oficiales PROD4 invalida estos permisos junto con la
extracción anterior. Los servicios API de detalle menor/SEACE exponen los tres
campos; este cambio no añade columnas visuales ni modifica el layout.

No se ejecuta LLM2 ni se reanalizan PDFs antiguos por aplicar la migración.
Los permisos se completarán en futuras lecturas LLM1; un backfill documental
se debe ejecutar expresamente, con presupuesto y acceso al PDF original.
