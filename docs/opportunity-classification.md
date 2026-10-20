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


## Clasificación comercial para el filtro interno (20/10/2026)

Estado: un selector de seis categorías implementado localmente en web/API; la
clasificación LLM1 está preparada con opt-in para lecturas nuevas en PROD2 y PROD6.
Validación, publicación y activación se registran por separado en tasks/. No modifica los seis
ejes canónicos anteriores. «Licencias», «desarrollo» o «industrial» son
clasificaciones comerciales, no modalidades jurídicas de contratación.

### Ejes internos de la lectura LLM1

| Eje comercial | Valores del catálogo v1 |
| --- | --- |
| Rubro (`primary_area`, con `secondary_areas` cuando haya entregables mixtos) | `software_ti`, `inteligencia_artificial`, `automatizacion_procesos`, `industrial_electrica`, `infraestructura_cloud`, `otro`, `sin_clasificar` |
| Qué trabajo se compra (`work_types`, uno o varios) | `licencias_suscripciones`, `desarrollo_software`, `implementacion_integracion`, `suministro_equipos`, `instalacion_montaje`, `soporte_mantenimiento`, `consultoria`, `capacitacion` |

Las especialidades son etiquetas controladas y versionadas: por ejemplo `chatbot`,
`agentes_ia`, `rpa`, `plc_scada`, `instrumentacion`, `tableros_electricos`,
`robotica`, `equipamiento_electromecanico`. No inferir `agentes_ia` de cualquier
chatbot ni `plc_scada` de una cascada o de software para HPLC. La lista vive en `workers/seace/src/buenapro_worker/extraction/commercial_taxonomy.v1.json`;
Python y web importan el mismo catálogo, sin strings inventados por el proveedor.

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
   vigentes. En la misma lectura LLM1 nueva que extrae requisitos, devolver
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

Contrato implementado, con versión `commercial_taxonomy_v1`:

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

**Persistencia:** las extracciones actuales conservan `raw_extraction_json`
y `summary_json`; `derive_summary` conserva el bloque válido explícitamente y
omite extras históricos inválidos sin romper la lectura original. La consulta
SQL selecciona primero la extracción vigente elegible más reciente, valida el
bloque y filtra antes de contar/paginar. No rescata etiquetas de una extracción
anterior si la actual no las tiene. Se usan las columnas JSONB existentes, sin
migración ni backfill automático. Un futuro índice debe justificarse por medición
y aprobarse/publicarse aparte. No se clasifica una página de resultados en React.

### Orden del siguiente lote solicitado

El usuario pidió procesar IA/agentes/chatbots, automatización e industrial, y
pidió definir este filtro antes de esa ejecución. El contrato comercial ya está
implementado localmente; queda preparar selección deduplicada desde el mapeo, incluyendo
candidatos relevantes fuera de las 32 coincidencias industriales. Conservar
ambiguos y revisar vigencia/modalidad; no usar una lista de títulos como prueba
de elegibilidad. El procesamiento posterior reutiliza decisiones y lecturas
previas y recorre JEV → ficha/documentos → LLM1 → evaluación de DIN y explicaciones
según el alcance aplicable, con recibos y presupuesto propios. La autorización
del lote no modifica el plan metadata-only ya cerrado ni autoriza repetir pagos
inciertos. Esta entrega no ejecuta el lote, modelos, migraciones ni workers.


### Filtro simple aprobado: Categoría

El usuario sustituyó la selección visible de varios ejes por un único selector
**Filtros → Categoría** en ambas fuentes. Catálogo compartido:
`workers/seace/src/buenapro_worker/extraction/commercial_categories.v1.json`,
versión `commercial_categories_v1`. La clasificación detallada sigue siendo
interna; esta proyección no cambia prompt/schema de una lectura ya pagada.

| Categoría | Regla sobre etiquetas válidas de LLM1 |
| --- | --- |
| Licencias y suscripciones | Trabajo licencias/suscripciones |
| Desarrollo de software | Trabajo desarrollo de software |
| IA y automatización | Rubro IA o automatización de procesos, o especialidad chatbot/agentes IA/RPA |
| Industrial | Rubro industrial/eléctrico, o PLC/SCADA, instrumentación, tableros, robótica o equipamiento electromecánico |
| Cloud e infraestructura | Rubro infraestructura/cloud |
| Soporte y mantenimiento | Trabajo soporte/mantenimiento |

Una oportunidad puede pertenecer a varias categorías. Todas no aplica restricción
comercial e incluye históricos sin etiquetas. No deducir categorías desde nombre,
keyword, afinidad o puntaje. La proyección no necesita otra llamada LLM ni
relecturas; no rellena históricos automáticamente.

`commercial_category` acepta un valor en UI y CSV/repeticiones OR en API. Se aplica
antes de count/paginación, conservando perfil, permisos y scores. Listas/detalles y
feed añaden `commercial_categories: string[]` derivado de la clasificación vigente
válida; no se confía en una lista arbitraria guardada en resumen. Catálogo, SQL,
normalizador TypeScript y proyección Python deben permanecer equivalentes.

Los parámetros anteriores `commercial_area`, `commercial_work`,
`commercial_specialty` y `commercial_status` permanecen compatibles en API y se
combinan con AND frente a categoría. No aparecen como cuatro controles en UI.
Una URL antigua conserva sus valores bajo Selección anterior; elegir una categoría
o Todas elimina esos criterios previos del formulario. Quitar categoría conserva
perfil y filtros ajenos. Búsqueda/tipo/vistas rápidas/fuente/paginación preservan
la categoría. Los valores desconocidos se rechazan con 400, no se ignoran.

Las APIs conservan `commercial_classification` sin alterar su forma, raw
prioritario, selección de extracción elegible actual y ausencia de fallback a
etiquetas viejas. Una cita comercial V5 no verificable mantiene revisión antes
del conteo/filtro. Evidencia documental no equivale a elegibilidad o QA humano.

Para una **carpeta nueva** de lectura seleccionada PROD2, el CLI acepta:

```text
--llm1-commercial-taxonomy commercial_taxonomy_v1
```

`auto` es el valor por defecto: conserva la carpeta congelada y no activa la
clasificación en una nueva. `none` mantiene la lectura sin etiquetas comerciales.
El flag queda en manifest/contexto e identidades de preparación, cola, prompt,
schema y caché; cambiarlo en una carpeta existente se rechaza. No agregarlo a las
corridas históricas. El servicio PROD2 admite el flag en su `base_argv` explícito y
lo conserva en el plan/epoch congelado. Gemini general ofrece el adaptador mediante
`policy_context`.

El lector dedicado **PROD6 Guarded** también acepta
`--llm1-commercial-taxonomy commercial_taxonomy_v1`. La ausencia del flag conserva
prompt/schema/identidad y recibos legacy. El opt-in requiere un namespace nuevo de
recibos: un marcador local inmutable congela política tras obtener el lock PG y
bajo el lock de recibos. Una carpeta con recibos/guard legacy no se convierte a la
nueva política. Los trabajos nuevos llevan el binding en payload; un trabajo IO
con política incompatible se bloquea antes de conexión/fuente, y una lectura
pendiente incompatible no se adopta para generar etiquetas.

La reutilización PG de lecturas actuales/comerciales/legacy ocurre antes de otra
lectura. Un trabajo ya intentado, o con otro intento registrado para ese documento
en el mismo runtime, solo permite recuperación local: SHA original, modelo,
clase/MIME, prompt/hash/schema y política deben acreditar la respuesta durable
exacta. Carpeta vacía, recibo de otra identidad, solicitud sin respuesta o consumo
incierto no habilitan descarga ni inferencia nueva. No se transforma incertidumbre
en costo cero ni se repaga para conseguir categorías.

Ambos normalizadores proyectan las seis categorías con el JSON compartido cuando
hay clasificación válida. La lista derivada no altera raw, hash de pago, score,
modalidad o requisitos. Los filtros web admiten documentos de ambas fuentes que
tengan el bloque válido; esta preparación no ejecuta trabajos ni rellena históricos.

Esta implementación no llama JEV/Gemini, no etiqueta automáticamente históricos,
no ejecuta el lote IA/industrial y no despliega web ni reactiva lectores. Esas
operaciones conservan sus tareas y verificaciones propias.

### Primera implementación: validación local y rendimiento

Código web/catálogo `20dea69`, lector seleccionado `c827802` y cortocircuito SQL
`2a0f646`. Las pruebas incluyen catálogo/evidencias/identidad inmutable, ausencia
de opt-in histórico, filtro antes de paginar y paridad SQL real de 35 fixtures por
fuente. El CASE exterior evita descomprimir y revalidar repetidamente extracciones
sin bloque comercial: en la muestra read-only, el conteo combinado pasó de
7979 a 445 ms, con los mismos resultados y cero subplanes comerciales ejecutados
en históricos sin etiquetas. Son mediciones puntuales, no un SLA de la página.

QA de interfaz: 18 capturas locales con datos existentes (dos fuentes, tres
resoluciones y tres estados), navegación/búsqueda/tipo/paginación/fuente y limpieza
selectiva de criterios; revisión visual representativa del principal. Sin tags
fabricados para llenar resultados, escrituras de negocio, PDFs, modelos o cambios
productivos. Recibo compacto de alcance, pruebas, evidencias y pendientes:
`docs/commercial-classification-20261020.json`.


### Simplificación y lectores preparados

La entrega posterior `c1f1802` sustituye los cuatro grupos visibles por Categoría,
sin modificar la clasificación LLM1 pagada. Pruebas de catálogo y SQL acreditan
las seis reglas compartidas, combinaciones internas OR, compatibilidad de filtros
anteriores y aislamiento de perfil/procedencia antes de paginar. Los lectores se
preparan y prueban offline; publicación y ejecución conservan pasos propios.
Cierre y evidencias: `docs/simple-categories-20261020.json`.
