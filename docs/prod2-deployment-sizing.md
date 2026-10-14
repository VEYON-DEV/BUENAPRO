# VM y concurrencia para el colector PROD2

## Actualización del script y DellR440 — 06/10/2026

`scripts/run_prod2_selected.py` implementa un coordinador con1barridor y2workers de procesamiento por defecto, dos plazas de modelos y pacing HTTP compartido. Se puede variar el paralelismo sin asignar un proceso por keyword. Es HTTP nativo; no requiere navegador ni GPU. El [flujo, comandos y costes](prod2-loose-documents-20261006.md#nuevo-script-preparado-selector-comprimidos-y-workers) incluyen ZIP/RAR, selección textual Jev y transporte FilesAPI para PDF grandes. No se inició la carga nueva ni se desplegó este flujo.

Para alojar solamente este colector, estimación inicial:4CPU lógicas,8GBRAM y80GBSSD libre;16GBRAM da más margen para dos conversiones/PDF grandes. Si web/PostgreSQL/Redis también viven ahí, partir de8CPU lógicas,16GBRAM y160GBSSD, medir y ajustar. No se conoce la CPU/RAM/disco concreto del DellR440 del usuario: el modelo comercial por sí solo no determina su capacidad. El caché y los originales tienen que vigilarse/respaldarse; esos tamaños de disco no representan almacenamiento permanente de100.000expedientes.

Validar `--preflight` desde la propia IP del Dell antes de pagos. Su acceso no queda acreditado por la prueba exitosa de la laptop ni por su ubicación. El preflight confirma el formulario/selector; una búsqueda/ficha/descarga normal siguen siendo necesarias para comprobar el recorrido completo desde esa máquina. En Linux requiere Python/dependencias del proyecto, Poppler, LibreOffice, libarchive/bsdtar y acceso a PostgreSQL/Redis/R2/Jev/Gemini.

Las mediciones y propuestas anteriores se conservan como antecedentes; no describen un despliegue nuevo.

La prioridad de infraestructura es validar acceso a PROD2 desde la salida de red de la nueva VM. La VM actual en Chile recibe 403 pese a que DNS, TLS y controles de conectividad funcionan. Una política de acceso/protección relacionada con origen es la explicación más consistente; país, IP/ASN y atributos de cliente no están aislados. [Diagnóstico y límites](sources/prod2-network-diagnostic.md). Más CPU/RAM no acredita resolver ese rechazo.

## Recursos actuales medidos

El 05/10/2026 a las 19:04 de Lima, la VM tenía dos CPU lógicas, 11,65 GiB de RAM total, 9,51 GiB disponibles y carga media de un minuto 0,044. El disco tenía 96,7 GiB totales y 30,3 GiB libres. [Snapshot de capacidad](sources/evidence/prod2-capacity-snapshot-20261005.json).

Docker reportó aproximadamente 98 MiB para worker-io, 60 MiB para worker-llm, 32 MiB para worker-match y 34 MiB para worker-notify en esa captura. Son consumos bajo la carga presente, no máximos ni evidencia de capacidad para veinte workers activos. No se ejecutó un benchmark, procesamiento documental u OCR durante la medición.

## Solicitud de VM

Estos tamaños son estimaciones iniciales de ingeniería. Se supone inferencia remota por API, una sesión HTTP/JSF PROD2, pocos documentos pesados simultáneos y cache temporal acotada. La [prueba completa desde la laptop](sources/prod2-http-flow-20261005.md) no usó navegador; no se necesitan veinte navegadores ni OCR intensivo concurrente para ese flujo. Los tamaños conservan margen para procesamiento documental y no son un benchmark.

| Uso | Mínimo de piloto | Recomendado para solicitar |
|---|---|---|
| Colector y descargas; PostgreSQL/modelos permanecen en la VM actual | 2 vCPU, 4 GB RAM, 40 GB SSD | 4 vCPU, 8 GB RAM, 80 GB SSD |
| Toda BuenaPro: web, PostgreSQL, colector y objetivo de 10–20 procesos livianos | 4 vCPU, 8 GB RAM, 80 GB SSD, con concurrencia acotada | 8 vCPU, 16 GB RAM, 160 GB SSD |

Para este caso, empezar con el colector separado de 4 vCPU/8 GB y conservar persistencia/modelos en producción reduce la migración y permite verificar acceso antes de mover el sistema completo. La decisión entre ambos tamaños depende de qué servicios se ubiquen realmente en la nueva VM.

Mientras se valida esa VM, la laptop puede alojar el colector y enviar discovery/checkpoints al PostgreSQL actual por túnel SSH. La recomendación conserva una sola BD duradera y traslada después el proceso de captura con su manifest y frontera, abriendo una sesión HTTP/JSF nueva. El [plan de arranque local y traslado](prod2-keyword-discovery-plan.md#arranque-desde-la-laptop-y-traslado-del-colector) exige primero un piloto idempotente, suspensión recuperable y exclusión entre colectores. El cliente HTTP acotado ya fue probado; paginación general y persistencia siguen pendientes. Esta planificación no ejecutó ingesta ni creó una BD local.

Pedir Ubuntu 24.04 LTS, preferentemente x86_64 para simplificar dependencias de conversión documental, SSH, IPv4 pública fija y posibilidad de ampliar CPU/RAM/disco. El transporte HTTP probado no necesita Chromium/Playwright. La arquitectura ARM de la VM existente no se identificó como causa del 403. No se necesita GPU mientras Jev y Gemini se consuman como APIs remotas.

El proveedor debe confirmar ubicación y salida de red en Perú y permitir una prueba previa desde la IP asignada. La clasificación GeoIP peruana por sí sola no acredita acceso: comprobar formulario, búsqueda positiva, ficha desde resultado, cronograma y todos los documentos mediante el flujo normal. Un GET/200 vacío no basta. Un datacenter peruano también podría ser rechazado por una regla de IP/ASN/cliente.

Texto para pedir cotización del colector:

> Solicito una VM en Perú con salida pública peruana, 4 vCPU, 8 GB de RAM, 80 GB SSD, Ubuntu 24.04 LTS x86_64, IPv4 pública fija y acceso SSH. Debe permitir ampliar recursos. Antes de contratar necesito validar desde esa IP el acceso público normal a PROD2/SEACE: búsqueda, ficha y descarga documental. PostgreSQL y procesamiento por APIs estarán en otro servidor.

Para una sola VM con todos los servicios y margen para 10–20 procesos livianos, sustituir por 8 vCPU, 16 GB RAM y 160 GB SSD. No se realizó solicitud, contratación, despliegue o cambio de proveedor.

## Workers y concurrencia inicial

Las 166 keywords o 664 combinaciones iniciales son trabajos en cola. No necesitan un proceso o navegador por keyword. El [runner actual](../workers/seace/src/buenapro_worker/queue/runner.py) ejecuta un handler síncrono por proceso; el [Compose](../infra/docker/docker-compose.yml) tiene un consumidor por cola io/llm/match/notify. Los parámetros de concurrencia de Settings no implementan un pool en ese runner. PROD2/Jev siguen siendo propuesta, no un despliegue activo.

| Etapa propuesta | Trabajos simultáneos iniciales |
|---|---:|
| Scheduler | 1 |
| Buscador/ficha PROD2 | 1 sesión JSF, secuencial |
| Jev remoto | 2; ampliar a 4 después de medir cuotas y cola |
| Descargas HTTP independientes y autorizadas | 2, con límite global por fuente |
| Conversión/parseo pesado de documentos | 1 |
| LLM1/LLM2 remotos | 2 plazas compartidas por prioridad |
| Reglas/persistencia/notificación | 1–2 livianos |

Se pueden definir más consumidores, manteniendo límites globales sobre navegación, documentos pesados y solicitudes a modelos. Tener veinte procesos registrados no obliga a mantener veinte trabajos pesados simultáneos. Los modelos consumen tiempo de red y cuotas; el navegador, conversión, archivos y PostgreSQL usan recursos locales.

El runner retiene una conexión PostgreSQL mientras ejecuta y muchos handlers abren otra. Presupuestar aproximadamente dos por worker ocupado, además de web/scheduler, y revisar transacciones largas/pool y lease/heartbeat antes de ampliar. Más procesos también aumentan conexiones y memoria. PostgreSQL advierte que la [memoria por operaciones concurrentes](https://www.postgresql.org/docs/current/runtime-config-resource.html) puede multiplicarse entre sesiones.

## Gate de capacidad

El piloto debe medir memoria máxima, CPU, disco temporal, conexiones PostgreSQL, duración por página/documento, edad de cola, cuotas de APIs y tasa de errores. Verificar que el recorrido completo termina dentro de la cadencia objetivo antes de prometer capacidad diaria. Aumentar solo la etapa cuya cola acumule retraso y cuya fuente/API admita más concurrencia.

Los tamaños y reparto propuestos no se aplicaron a producción. El snapshot actual acredita ausencia de saturación en esa medición; no sustituye una prueba de carga de la ingesta inicial y de PDFs grandes.
