# PROD2: lotes por negocio del convenio VEYON + Dinaut

Implementado el 06/10/2026 en `scripts/run_prod2_selected.py`. Configuración versionada: `docs/sources/prod2-business-batches.v1.json`. El primer lote real autorizado de30keywords terminó:100candidatas únicas/10aceptadas,7keywords completas+software parcial+22pendientes y90candidatas Redis por procesar. PG/Redis/API productivos verificados. El enriquecimiento quedó incompleto:7selecciones pendientes,3fichas con UUID reutilizado entre eventos y0lecturas nuevas LLM1. Coste registrado estimadoUS$0.003020934. [Resultados por keyword y convocatoria](./prod2-digitales30-lote01-20261006.md). Ejecución HTTP desde laptop, PostgreSQL/Redis por túnel privado; sin despliegue ni reinicio de workers de laVM.

## Arranque con 30 keywords y seguimiento persistente

El preset `digitales30` selecciona exactamente 30 keywords existentes del convenio, sin ampliar aliases de otros lotes. Se conserva una empresa conjunta para Jev y LLM1.

| Lote | Cantidad | Keywords del arranque |
| --- | ---: | --- |
| Desarrollo | 12 | software; desarrollo de software; desarrollo de aplicaciones; sistemas de información; sistema de información; aplicaciones web; aplicación web; plataformas digitales; aplicativo; integración de sistemas; mantenimiento de software; erp |
| Licencias/cloud | 8 | licencias de software; suscripción de software; licencias informáticas; microsoft; licencias microsoft; soluciones cloud; seguridad informática; herramientas colaborativas |
| IA/datos | 6 | inteligencia artificial; agentes de ia; agentes ia; chatbot; analítica de datos; automatización de procesos |
| Soporte | 4 | soporte técnico; mantenimiento de sistemas; aws; ciberseguridad |

Configuración exacta en `docs/sources/prod2-keyword-presets.v1.json`, vinculada por hash al inventario y los lotes. No se infiere relevancia por términos cortos como `IA` ni se fusionan variantes cuya equivalencia en el buscador no está acreditada.

### Qué significa una keyword usada

PostgreSQL guarda una tarea por `sweep_id + keyword + objeto + año de convocatoria + fechas de publicación + versión SEACE`. Sus estados son `pending`, `running`, `partial`, `blocked` y `complete`. Conserva intentos, contadores, errores, fecha de observación original y evidencia verificable. `used` indica que se intentó ejecutar; no equivale a completar todas las páginas. Una keyword queda completa solo cuando todas sus consultas registradas están completas. Un ámbito vacío requiere dos sesiones independientes y prueba exacta de cero resultados.

La migración `0040_prod2_keyword_tracking.sql` guarda exclusivamente el control de consultas. Los candidatos permanecen en Redis hasta Jev >=60; una decisión rechazada conserva solo su caché mínima. Así se registra el barrido sin llenar oportunidades con resultados irrelevantes.

La evidencia histórica se presenta por separado. La revisión read-only del 06/10/2026 encontró `software` en producción: una pasada completa de **Bien/2026, publicaciones 01–05/10/2026**, y muestras aceptadas de Bien/Servicio en 06/04–06/10/2026. Las muestras no acreditan un barrido completo. No se encontró evidencia de uso de las otras 29 en las tablas consultadas; no significa que nunca se usaron. No se convierte ese histórico en tareas completas del nuevo barrido.

### Recuperación del primer arranque real

El lote `prod2-digitales30-lote01` comenzó el06/10/2026 a las16:10Lima con caps100candidatos/10aceptadas/120Jev/10LLM1. Aplicó el tracking y guardó pruebas de ámbitos, pero sufrió `OperationalError` antes de descubrir candidatos o llamar a modelos. No quedó un diagnóstico de conexión más específico; no se atribuye una causa de red confirmada.

El primer reintento respetó el lock y salió sin fuente/pagos. Se identificó una única sesión huérfana: backend491 abierto21:11:04UTC, época creada21:11:08UTC y últimoCOMMIT/idle21:13:28UTC, coincidentes con el intento inicial y ambos PIDs locales ya muertos. Se cerró solo esa identidad con condiciones SQL sobre timestamps/estado/lock. El reintento2 (PID54572,16:32Lima) conservó carpeta, manifest, cachés y caps; PG confirmó el nuevo coordinador identificado y consultas activas. No se liberan ni terminan sesiones de propietarios desconocidos.

El túnel incorpora keepalive SSH; el coordinador usa application_name del lote, timeout de conexión y keepalive TCP, y evita ocultar el fallo original con un unlock sobre una conexión rota. La siembra usa executemany para reducir viajes de red.58pruebas runner/runtime y75focales más4contratos PG shadow aprobaron esos ajustes. Cerrar esta recuperación no acredita cobertura completa ni resultados LLM1; el lote se verifica por separado.

### Continuar sin repetir candidatos ni pagos

El `sweep_id` identifica una época de observación con fechas congeladas. Se reutiliza para continuar y se cambia para refrescar la fuente. PostgreSQL bloquea cada consulta mientras tiene dueño; un lock del coordinador evita dos ejecuciones generales simultáneas antes de contactar al portal.

Los archivos privados de páginas, particiones, reconciliaciones y pruebas de cero se comparten entre carpetas del mismo barrido. Un replay completo conserva la fecha original y no vuelve a consultar SEACE. Si falta o cambia evidencia completada, el script bloquea antes de bootstrap/probes; requiere restaurar el caché o una nueva época. El selector de años observado también se congela en el caché de la época. Una nueva época vuelve a observarlo.

Redis comparte la cola y deduplicación de convocatorias entre los lotes del mismo barrido y entradas semánticas. Cada carpeta nueva dispone de su propio presupuesto de candidatos nuevos. Ejemplo: cap100 sobre300 resultados → lote1 agrega100; lote2 reemite páginas privadas, descarta esos100 por ID y agrega los siguientes100; lote3 continúa. La carpeta original conserva su cap al reanudarla, salvo el override operativo explícito `--all-results` descrito abajo. Cambiar perfil comercial, modelo o política de documentos abre otra cola porque cambia el trabajo semántico.

La cola compartida conserva pendientes de los lotes anteriores, incluso si el siguiente lote selecciona otra keyword del mismo barrido. Procesa esos pendientes primero y conserva su contexto de búsqueda original. La configuración del perfil conjunto se mantiene congelada por corrida. Los hashes globales de Jev/selector/LLM1 siguen independientes del nombre del lote y evitan cargos repetidos para entradas idénticas. No se reintentan automáticamente pagos inciertos.

### Reutilizar descartes en próximos barridos

Antes de llamar a Jev, `paid_jev` consulta `prod2_jev_decision_cache` en PostgreSQL. Allí se conservan también las decisiones menores de 60: puntuación, respuesta, identidad de la solicitud y datos de uso/coste original. Una coincidencia completada reutiliza el resultado, registra `jev_cached=true` y coste incremental cero; si sigue por debajo de 60, termina sin ficha, descargas ni LLM1. La decisión permanece aunque se vacíe Redis o se cambie de keyword, carpeta, fechas o época de barrido.

La clave incluye descripción normalizada, perfil comercial conjunto, modelo, preguntas/instrucciones y versión de política. No depende del ID de convocatoria: dos convocatorias con la misma entrada pueden compartir una decisión. Si cambia una de esas entradas, se trata como una evaluación nueva; así un descarte viejo no bloquea una descripción corregida ni una capacidad comercial nueva. Las reservas inciertas bloquean la generación hasta reconciliarse.

La comprobación productiva del 08/10/2026 UTC (07/10 por la noche en Lima) encontró los 67 miembros rechazados de industrial20 respaldados por 54 solicitudes distintas completadas, sin decisiones ausentes, inválidas ni miembros rechazados promovidos. Fue una lectura de PostgreSQL, sin nuevas llamadas a SEACE/modelos ni escrituras de negocio. Evidencia: [auditoría de la caché de descartes](./prod2-rejected-jev-cache-audit-20261008.json).

### Barrer todas las coincidencias del lote

Desde el 07/10/2026, `--all-results` quita los cuatro topes de volumen: candidatas, aceptadas, llamadas Jev y llamadas LLM1. Se recorren las consultas y páginas de las keywords seleccionadas dentro de la ventana congelada. Cada convocatoria única pasa por Jev; solo Jev >=60 promueve a PostgreSQL y continúa hacia ficha, cronograma, enlaces oficiales, selección documental y LLM1. Duplicados por ID o entradas pagadas idénticas reutilizan sus resultados.

Para continuar un piloto anterior, conservar la misma carpeta y **todas sus flags originales**, añadiendo únicamente `--all-results`. El manifiesto y los hashes pagados no cambian: el modo y los límites efectivos `null` quedan en un recibo `execution-policy-*.private.json` vinculado al SHA256 del manifiesto. `--dry-run` y el reporte muestran `execution_policy.effective_limits`, que prevalece sobre los topes históricos congelados del piloto. Una keyword completa, con caché verificable, se reproduce sin nuevas llamadas al origen; una parcial continúa desde su evidencia retenida.

Se mantienen concurrencia, pacing, límites de tamaño de archivos y paradas ante errores/bloqueos de la fuente. Los recortes del origen (por ejemplo, >=499 resultados dentro de un solo día sin partición resoluble) quedan como cobertura incompleta. El script solo devuelve cierre técnico completo si la cobertura está completa, la cola/reservas están vacías, todas las aceptadas tienen LLM1 completada, todos los resultados tienen estado técnico terminal y no hay pagos inciertos. La revisión humana semántica sigue siendo independiente de ese cierre técnico.

Los outcomes antiguos de error se conservan y se omiten al continuar; este modo no autoriza repetir llamadas inciertas ni resolver automáticamente los pendientes anteriores. No borra cachés, decisiones o recibos para forzar un reintento.

En laptop, mantener la tapa abierta durante el lote. `caffeinate -i` evita suspensión por inactividad mientras vive el proceso, pero no evita suspensión por cierre de tapa. El diagnóstico del corte industrial20 y sus recibos retenidos están en `prod2-industrial20-resume-20261007.json`.

Reanudación real autorizada: las 20 keywords de eléctrica/instrumentación continúan en `tmp/prod2-industrial20-electrica-instrumentacion-lote01-20261007/`, misma ventana 07/04–07/10/2026 y época. Se conservaron diez aceptadas anteriores, seis lecturas completas y cuatro pendientes; 80 recibos pagados y manifiesto original se verificaron sin cambios. El arranque del modo ilimitado se confirmó en PostgreSQL/Redis y proceso local; no acredita aún completar las 20 keywords. Validación principal: 274 pruebas aprobadas, dos opt-in externas omitidas; prueba offline de 151 convocatorias únicas y 12 aceptadas, sin duplicar pagos al repetir keywords/páginas. Detalle de pruebas/limpieza en `prod2-all-results-validation-20261007.json`.

Redis debe conservar sus claves, sin eviction ni TTL, con persistencia y respaldo; los archivos privados también deben conservarse al trasladar a la VM. Redis conserva temporalmente descripciones pendientes y un ID mínimo tras ack; PostgreSQL es la fuente de verdad del progreso y las oportunidades aceptadas. Un watermark privado compartido se actualiza tras cada candidato nuevo; si Redis retrocede frente a ese registro, bloquea incluso cuando el primer lote comenzó con cero IDs. Los estados privados de coordinación y los recibos inmutables de modelos se almacenan por separado.

### Fechas, vigentes y adjudicadas

El intervalo inicial es **06/04/2026–06/10/2026**, inclusivo, para Bien y Servicio. La fecha filtra publicación, no vigencia, y el año nativo corresponde a convocatoria: `auto` usa todos los ejercicios del selector observado, no solo el año de publicación. No es un censo de todo PROD2: cubre coincidencias con esas 30 señales.

Después de Jev >=60, la ficha guarda cronograma, estados observados y enlaces públicos. Se registra `temporal_state`, fecha de evaluación y `confirmed_open` con razones; fecha reciente no basta. Se distinguen abiertas para nuevo participante, adjudicadas, cerradas, próximas, acceso condicionado y desconocidas. Las ambiguas quedan para revisión. Las adjudicadas pueden conservarse, pero este cambio no incorpora extracción del ganador/precio de adjudicación. El monto de referencia no debe presentarse como precio ganador.

Los límites siguen siendo explícitos: por defecto100 candidatos nuevos,10 aceptadas y10 llamadas LLM1 por carpeta. Alcanzar un cap deja consultas parciales/pendientes; seleccionar30 no acredita que todas fueron barridas. La cobertura de búsqueda y el procesamiento hasta LLM1 son indicadores distintos. Un403/429 en búsqueda, ficha o documentos detiene nuevos accesos fuente y nuevas llamadas pagadas; las respuestas pagadas en vuelo conservan sus recibos.

### Comandos del arranque

Plan offline, sin fuente/BD/Redis/modelos:

```sh
.venv/bin/python scripts/run_prod2_selected.py --folder tmp/prod2-digitales30-lote01 --keyword-preset digitales30 --sweep-id inicial-digital-20261006 --from-date 2026-04-06 --to-date 2026-10-06 --years auto --dry-run
```

Consultar avance e histórico en la BD configurada, solo lectura y sin portal/modelos; funciona también antes de aplicar0040 y muestra `tracking_schema_ready: false`:

```sh
.venv/bin/python scripts/run_prod2_selected.py --folder tmp/prod2-digitales30-lote01 --keyword-preset digitales30 --sweep-id inicial-digital-20261006 --keyword-status
```

Antes de la primera corrida real, preparar0039 y0040 mediante sus modos dedicados. Estos comandos aplican solo la migración indicada, verifican contrato y registran versión; no ejecutan toda la cadena DDL legacy ni llaman a SEACE/modelos:

```sh
.venv/bin/python scripts/run_prod2_selected.py --folder tmp/prod2-schema --prepare-selector-cache
.venv/bin/python scripts/run_prod2_selected.py --folder tmp/prod2-schema --prepare-keyword-tracking
```

Luego el comando de plan sin `--dry-run` ejecuta el primer lote hasta LLM1. Para avanzar, usar otra carpeta (`...-lote02`) con el mismo `sweep_id`, fechas y preset; para recuperar un crash, usar la carpeta original y flags idénticos. `--keyword-cache-folder` debe apuntar al mismo directorio privado persistente. Para la siguiente observación diaria o mantenimiento de siete días, usar otro `sweep_id` y ventana explícita. Una época nueva permite verificar cambios reales, aunque keywords e IDs ya fueran conocidos; los cachés semánticos siguen protegiendo los pagos.

La fase de preparación validó el código y consultó histórico en modo read-only, sin migraciones ni ingesta. Tras autorización posterior del usuario se aplicaron0039/0040: ledger correcto y tablas nuevas vacías antes del arranque. Redis verificó PONG, política `noeviction`, AOF activo y último snapshot correcto. La cobertura/tiempo reales se miden en el primer lote; el inicio no acredita que las30 estén barridas.

Para mantener la vigencia después de la carga inicial, los siete días de publicaciones nuevas deben complementarse con refresco de las fichas abiertas guardadas anteriormente: una convocatoria vieja puede cambiar su cronograma o adjudicarse sin entrar otra vez en ese intervalo de publicación. Ese refresco/planificador permanece pendiente; este cambio entrega el tracking y la ejecución explícita por lotes.

### Validación del 06/10/2026

El principal ejecutó 1.258 pruebas worker aprobadas y16 opt-in omitidas, cuatro contratos PostgreSQL reales con rollback de schemas privados y dos contratos Redis reales con limpieza exclusiva de claves QA. El recorrido cap2/7IDs/4lotes avanza y entrega cada ID una sola vez; borrado Redis con watermark previo bloquea. Validó también pruebas de evidencia incompleta/corrupta/zero proof/split, timestamps originales, locks,403/429 incluidos helper de ficha y permisos de proveedor, compile y diff-check.

El dry-run confirmó30 keywords, distribución12/8/6/4, Bien/Servicio, ventana06/04–06/10/2026 y años auto. `--keyword-status` se ejecutó de verdad contra producción bajo `SET TRANSACTION READ ONLY` y rollback:0040 ausente,30 keywords no registradas en el nuevo tracking, histórico disponible solo para `software`, cero accesos SEACE/proveedores/escrituras de datos. Evidencia privada: `tmp/prod2-digitales30-plan-20261006.private.json` y `tmp/prod2-digitales30-status-20261006.private.json`.

## Inventario y primera etapa

Se conservan exactamente las 166 keywords del snapshot actual: Dinaut declara 146 únicas, VEYON 36 y ambas coinciden literalmente en 16. Las variantes como `agentes ia`/`agentes de ia` o `sistema de información`/`sistemas de información` se mantienen. Que una keyword figure solo en una empresa no demuestra exclusividad comercial.

La primera etapa recomendada son los cuatro lotes digitales del convenio:

| ID del lote | Negocio | Keywords primarias | Ejemplos |
| --- | --- | ---: | --- |
| `software_desarrollo` | Desarrollo e integración de software | 25 | software, aplicativos, API, aplicaciones web, sistemas de información |
| `licencias_cloud` | Licencias, suscripciones y cloud | 17 | licencia, suscripción, Microsoft, cloud, SaaS, antivirus |
| `ia_datos` | IA, datos y automatización digital | 20 | inteligencia artificial, chatbot, agentes IA, ciencia de datos, RPA |
| `soporte_integraciones` | Soporte, infraestructura e integraciones | 11 | soporte técnico, mantenimiento de sistemas, AWS, nube, servidor |
| **Total digital** | | **73** | |

Son lotes de búsqueda bajo un único perfil comercial conjunto. No son cuatro perfiles diferentes ni una clasificación confirmada del objeto contratado. Las líneas fuente son DINAUT/VEYON en desarrollo e IA, DINAUT en licencias y VEYON en soporte; las descripciones comerciales pueden declarar capacidades adicionales. Se conserva la procedencia literal de cada keyword sin inferir experiencia ni habilitación.

## Segunda etapa industrial

Los ocho lotes industriales de Dinaut quedan separados de los digitales:

| ID del lote | Negocio | Keywords primarias |
| --- | --- | ---: |
| `control_industrial` | Automatización y control industrial | 16 |
| `capacitacion_industrial` | Capacitación industrial | 7 |
| `puesta_marcha` | Comisionamiento y puesta en marcha | 11 |
| `digitalizacion_industrial` | IIoT, MES y digitalización industrial | 11 |
| `ingenieria_electrica` | Ingeniería eléctrica y tableros | 14 |
| `instrumentacion` | Instrumentación y medición | 12 |
| `redes_industriales` | Redes y comunicación industrial | 12 |
| `robotica` | Robótica y optimización de procesos | 10 |
| **Total industrial** | | **93** |

## Una keyword, varias asociaciones

Cada keyword tiene un lote primario estable y conserva relaciones con las otras líneas que la declaran. En una corrida conjunta se busca una sola vez. En un lote aislado se incluyen las señales relacionadas para conservar su alcance:

| Lote aislado | Primarias | Keywords de búsqueda aislada |
| --- | ---: | ---: |
| Licencias | 17 | 18: incluye también software |
| IA/datos | 20 | 23: incluye software, informática y AWS |
| Soporte/integraciones | 11 | 16: incluye señales declaradas que tienen otro dueño primario |
| Capacitación industrial | 7 | 16: incluye PLC/SCADA y términos de redes |

Los otros lotes mantienen el mismo número. Unir todos los digitales produce 73 consultas distintas por año/objeto, no la suma de sus ocurrencias relacionadas; unir todos los lotes produce 166. Las señales amplias configuradas —por ejemplo IA, datos, soporte, implementación— se conservan al final de cada lote. No se usan para aceptar por sí solas una oportunidad.

## Comandos

Los siguientes comandos son planes offline: no conectan a SEACE, PostgreSQL, Redis ni modelos.

Todos los digitales:

```sh
.venv/bin/python scripts/run_prod2_selected.py --folder tmp/prod2-digitales-20261006 --business-scope digitales --years auto --dry-run
```

Dos lotes específicos:

```sh
.venv/bin/python scripts/run_prod2_selected.py --folder tmp/prod2-software-licencias-20261006 --business-batch software_desarrollo --business-batch licencias_cloud --years auto --dry-run
```

Todos los negocios, digitales e industriales:

```sh
.venv/bin/python scripts/run_prod2_selected.py --folder tmp/prod2-negocios-20261006 --business-scope todas --years auto --dry-run
```

Para industriales solamente, usar `--business-scope industriales`. No mezclar estos selectores con `--keyword`, `--lane` o `--all-keywords`. Los selectores antiguos mantienen su comportamiento y su manifest. Los IDs repetidos se unen; el orden de ejecución proviene de la configuración versionada.

Los límites por defecto siguen siendo piloto: 100 candidatos únicos, diez aceptadas y diez llamadas LLM1. Seleccionar todos los lotes **no levanta esos límites** y no garantiza que todos sean consultados antes de alcanzar el cap. Para un barrido completo se necesitan límites explícitos y revisar la cobertura final; el número de keywords no es un conteo de convocatorias.

Una ejecución real requiere quitar `--dry-run`, preparar primero la migración 0039 y las dependencias/configuración indicadas en `prod2-loose-documents-20261006.md`. La carpeta congela la selección y los límites; cambiar lote/fechas/configuración exige una carpeta nueva. No se creó un scheduler ni se inició la corrida real con estos comandos.

## Ejecución y ahorro de tokens

Un coordinador controla la búsqueda; dos procesadores comparten la cola por defecto. Los lotes no requieren procesos fijos por empresa. El flujo mantiene Bien/Servicio, seis meses, paginación/particiones, pacing HTTP global y Jev >=60 antes de promover una oportunidad.

Redis deduplica la convocatoria dentro de la corrida antes de evaluar. PostgreSQL conserva decisiones y reservas globales entre carpetas/lotes: la identidad pagada sigue usando los mismos datos semánticos, perfil conjunto, documentos y versiones. El nombre del lote no invalida una evaluación. Cambios reales de descripción, perfil, documentos o políticas pueden requerir una nueva lectura; las reservas inciertas quedan para conciliación.

La primera observación Redis conserva su keyword y lote de búsqueda. Los snapshots por consulta permiten contrastar observaciones adicionales. El informe final incluye por lote consultas esperadas/completadas, cobertura, procesadas por primera búsqueda, aceptadas y LLM1 terminadas. Un candidato encontrado por dos lotes se cuenta una vez en esos totales; eso no prueba que su negocio sea el lote que lo encontró primero. Una lista incompleta o con consultas repetidas no declara cobertura completa.

El agrupamiento organiza y evita repeticiones; por sí solo no permite calcular cuántas licitaciones habrá ni su coste. Hace falta medir candidatos únicos, aceptación y usage. No se vuelve a pagar por cambiar únicamente el lote cuando los inputs semánticos se conservan.

## Validación

- Suite worker: 1.205 pruebas aprobadas, 11 opt-in omitidas.
- Revisión independiente y comprobación del principal de las 4.095 combinaciones no vacías: unión exacta de señales relacionadas, sin duplicados y con dueño de búsqueda válido.
- Contratos de flags/manifest/reporte: scopes 73/93/166, aliases aislados, variantes lingüísticas intactas, configuración obsoleta/incompleta rechazada y cobertura parcial explícita.
- Dry-run digital ejecutado offline; sin carga, llamadas pagadas ni cambios de negocio en producción.
