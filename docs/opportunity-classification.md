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
| Resultado histórico PROD6 | `seace_prod6` | `opportunity` | `minor_purchase` | `informational`; reutiliza el UUID del contrato vigente si coincide `id_contrato` |
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


## Clasificación comercial para el filtro interno (diseño del 20/10/2026)

Estado: contrato propuesto tras revisar código y el mapeo completo; todavía no
implementa un campo nuevo en LLM1, persistencia, endpoints ni pantalla. No modifica
los seis ejes canónicos anteriores. «Licencias», «desarrollo» o «industrial» son
clasificaciones comerciales, no modalidades jurídicas de contratación.

### Dos ejes que se pueden combinar

| Eje comercial | Valores iniciales propuestos |
| --- | --- |
| Rubro (`primary_area`, con `secondary_areas` cuando haya entregables mixtos) | `software_ti`, `inteligencia_artificial`, `automatizacion_procesos`, `industrial_electrica`, `infraestructura_cloud`, `otro`, `sin_clasificar` |
| Qué trabajo se compra (`work_types`, uno o varios) | `licencias_suscripciones`, `desarrollo_software`, `implementacion_integracion`, `suministro_equipos`, `instalacion_montaje`, `soporte_mantenimiento`, `consultoria`, `capacitacion` |

Las especialidades son etiquetas controladas y versionadas: por ejemplo `chatbot`,
`agentes_ia`, `rpa`, `plc_scada`, `instrumentacion`, `tableros_electricos`,
`robotica`, `equipamiento_electromecanico`. No inferir `agentes_ia` de cualquier
chatbot ni `plc_scada` de una cascada o de software para HPLC. La lista necesita
catálogo y validación, no strings inventados por el proveedor.

Un rubro principal describe el entregable dominante. Los rubros secundarios se
justifican por otros entregables efectivamente contratados; no por el nombre de
la entidad, la profesión del usuario o una palabra incidental. No basta que un
equipo médico mencione «automatizado» para convertirlo en un proyecto de RPA.
`sin_clasificar` significa información insuficiente; `otro` significa objeto
identificado fuera del catálogo. Ninguno equivale a rechazo comercial ni puntaje 0.

| Compra concreta | Rubro | Trabajo | Especialidad |
| --- | --- | --- | --- |
| Suscripción al chatbot AIVO para RENIEC | Inteligencia artificial | Licencias/suscripciones | Chatbot; no afirmar desarrollo de agentes |
| Desarrollo a medida de un asistente con herramientas y flujos documentados | Inteligencia artificial | Desarrollo e integración, según las bases | Agentes IA solo si se acredita ese alcance |
| Consultoría para automatizar el acondicionamiento del catastro fiscal | Automatización de procesos | Consultoría | No inferir tecnología RPA sin evidencia |
| Mantenimiento de tableros eléctricos | Industrial/eléctrica | Soporte/mantenimiento | Tableros eléctricos |
| Licencias para una plataforma SCADA | Industrial/eléctrica, con software TI secundario si corresponde | Licencias/suscripciones | PLC/SCADA |
| Compra de sistema automatizado de análisis de laboratorio | Clasificar el equipo y alcance realmente suministrado | Suministro de equipos | No equiparar a desarrollo de software o automatización de procesos |

Los ejemplos hipotéticos no agregan oportunidades al mapeo. Los casos RENIEC y
catastro se identificaron solo por el listado; aún requieren fuentes vigentes.

### En qué etapa se obtiene y cómo se conserva

1. **Discovery:** keywords y coincidencias de título ayudan a encontrar IDs. No
   son la clasificación comercial final y no deben ser el filtro único: el mapeo
   contiene industriales capturadas por keywords digitales generales.
2. **JEV por descripción:** la implementación actual devuelve `informatica`,
   `industrial_electrica`, `otro` o `ambiguo`, afinidad comercial y suficiencia.
   Esa salida es preliminar y ya tiene su propio hash de pago; no cambiarla ni
   repetir decisiones guardadas solo para introducir etiquetas nuevas.
3. **Ficha y LLM1:** comprobar participación/fechas y seleccionar documentos
   vigentes. En la misma futura lectura LLM1 que extrae requisitos, devolver
   `commercial_classification` con rubro, trabajos y especialidades. Se analiza
   el objeto y los entregables efectivamente comprados; las keywords sirven de
   contexto y no ordenan la respuesta. La salida compacta añade tokens, pero no
   necesita una segunda llamada dedicada de clasificación para casos nuevos.
4. **Normalización local:** validar catálogo, coherencia, referencias y versión.
   Persistir una proyección consultable independiente del puntaje. Las reglas de
   evaluación continúan comparando requisitos, experiencia, montos y profesionales;
   las etiquetas no acreditan capacidades ni alteran automáticamente una nota.
5. **Filtro:** aplicar clasificación en el servidor antes de contar y paginar,
   con el mismo contrato para PROD2 y contratos menores. OR entre valores del
   mismo eje y AND entre ejes: «IA + Licencias» encuentra suscripciones de IA,
   «Industrial + Mantenimiento» encuentra mantenimiento industrial. Los no
   clasificados siguen consultables mediante su propio estado, sin borrarse.

Contrato mínimo propuesto, con versión `commercial_taxonomy_v1`:

- `primary_area`, `secondary_areas`, `work_types`, `specialties`.
- `support_status`: `preliminary`, `evidence_backed` o `needs_review`.
- Evidencias por etiqueta: cita breve, documento/hash y página original; para
  clasificación preliminar por título conservar ID y hash del listado en lugar
  de inventar página. `evidence_backed` no acredita QA humano, vigencia o elegibilidad.
- Procedencia de la clasificación y versión de taxonomía; al normalizar vincular
  también extracción, modelo/prompt y hash de entrada existentes. Mantener
  separado resultado del modelo y corrección humana con autor/fecha/motivo.

**Cobertura:** con V5 recuperado no asumir lectura de cada página. Asegurar que
objeto, alcance, bienes y entregables estén representados en el contexto
recuperado; falta de cobertura o contradicciones dejan `needs_review`. Citar
páginas originales, no páginas del recorte. Integrar el nuevo bloque en todos
los esquemas/políticas de entrada activos de TDR, EETT y bases de bienes/servicios;
no basta agregar un campo al schema general que el esquema reducido luego omita.
En particular, PROD2 V5 usa `QualificationTdrExtractionV1`, una clase separada de
`TdrExtractionV2`. El facet `business_line` es giro exigido al postor; tampoco
reutilizar `classification.rubro` de documentos de empresa como rubro de compra.

**Históricos:** conservar extracciones, recibos y pagos inciertos. Una etiqueta
nueva no autoriza volver a leer PDFs o repetir un modelo ya pagado. Cuando haya
evidencia estructurada suficiente, una proyección local o revisión humana puede
completar clasificación con procedencia distinta. De lo contrario, queda sin
clasificar. Versionar prompt/schema/política solamente para lecturas nuevas y
no mutar manifiestos, bindings ni claves de caché de corridas congeladas.

**Persistencia prevista:** las extracciones actuales conservan `raw_extraction_json`
y `summary_json`; `derive_summary` reconstruye el resumen y deberá conservar el
nuevo bloque explícitamente. Para filtrar antes de paginar, preparar un índice
comercial consultable por oportunidad/extracción vigente, con migración y
backfill local independientes; no basta clasificar en React los resultados de
una página. Esta sección no aplica migración ni decide un despliegue.

### Orden del siguiente lote solicitado

El usuario pidió procesar IA/agentes/chatbots, automatización e industrial, y
pidió definir este filtro antes de esa ejecución. Primero implementar/validar el
contrato comercial y preparar selección deduplicada desde el mapeo, incluyendo
candidatos relevantes fuera de las 32 coincidencias industriales. Conservar
ambiguos y revisar vigencia/modalidad; no usar una lista de títulos como prueba
de elegibilidad. El procesamiento posterior reutiliza decisiones y lecturas
previas y recorre JEV → ficha/documentos → LLM1 → evaluación de DIN y explicaciones
según el alcance aplicable, con recibos y presupuesto propios. La autorización
del lote no modifica el plan metadata-only ya cerrado ni autoriza repetir pagos
inciertos. Este diseño no ejecuta el lote, modelos, migraciones ni workers.
