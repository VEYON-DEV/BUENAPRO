# Clasificación transversal de registros de compra

No usar un único `tipo` o `tag` para mezclar procedencia, modalidad y etapa. El
registro canónico `opportunities` separa seis ejes; `opportunity_sources`
conserva el identificador nativo de cada plataforma. El título, la entidad o
el monto **no** son claves únicas ni bastan para fusionar registros.

| Eje | Pregunta | Valores iniciales útiles |
| --- | --- | --- |
| `record_kind` | ¿Qué representa la fila? | `opportunity`, `early_notice`, `market_sounding`, `purchase_order`, `catalog_reference`, `other` |
| `object_type` | ¿Qué se compra? | `good`, `service`, `general_consulting`, `work`, `works_consulting`, `other`, `unknown` |
| `procurement_method` | ¿Por qué mecanismo se compra? | `minor_purchase`, `selection_procedure`, `electronic_catalog`, `other`, `unknown` |
| `lifecycle_stage` | ¿En qué etapa está? | `planned`, `pre_notice`, `open`, `evaluation`, `awarded`, `contracted`, `closed`, `unknown` |
| `participation_access` | ¿Quién puede intervenir? | `public`, `registration_required`, `catalog_member`, `invitation_only`, `tenant_private`, `unknown` |
| `actionability` | ¿Puede actuar el proveedor ahora? | `actionable`, `informational`, `unknown` |

La plataforma se guarda como `source_key` de `opportunity_sources`, no como
modalidad. Ejemplos de mapeo para los futuros adaptadores (pendientes de
comprobar por endpoint y caso real):

| Registro | Fuente | Clase | Modalidad | Acción |
| --- | --- | --- | --- | --- |
| Contrato menor PROD6 | `seace_prod6` | `opportunity` | `minor_purchase` | `unknown` hasta verificar ventana y elegibilidad |
| Procedimiento de selección PROD4 | `seace_prod4` | `opportunity` | `selection_procedure` | Depende de cronograma y reglas de participación |
| Anuncio de contratación futura PROD2 | `seace_prod2` | `early_notice` | `selection_procedure` o `unknown` | `informational`; no confundir con convocatoria |
| Difusión de requerimiento / sondeo PROD2 | `seace_prod2` | `market_sounding` | `unknown` | Verificar si admite manifestar interés o cotizar; no equiparar a postulación |
| Ficha de catálogo Perú Compras | `peru_compras` | `catalog_reference` | `electronic_catalog` | `informational` como ficha; la incorporación al catálogo es otro flujo |
| Orden de compra Perú Compras | `peru_compras` | `purchase_order` | `electronic_catalog` | `informational` como orden emitida |

`minor_purchase` describe el régimen de contratación menor, no el objeto
`good`/`service` ni un límite monetario fijo. No inferirlo solo del monto: el
valor de la UIT y las reglas aplicables dependen del año y del procedimiento.
Tampoco convertir `lifecycle_stage=open` en `actionability=actionable`: verificar
plazo, registro requerido, invitación y canal oficial antes de mostrar
«Postular».

Los adaptadores deben conservar en `native_values` los códigos y estados
originales, además del payload crudo en su almacenamiento propio. Una fuente
nueva genera una referencia única `(source_key, id_kind, external_id)`; el
enlace a una oportunidad existente solo se realiza con identificadores
cruzados oficiales o revisión humana, nunca por similitud textual automática.
