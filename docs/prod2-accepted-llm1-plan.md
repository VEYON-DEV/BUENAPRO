# Flujo acordado: Jev >= 60 y finalizar en LLM1

**Nuevo diseño acordado (08/10/2026):** el usuario eligió embeddings para recuperar secciones antes de LLM1. [Arquitectura y contratos](prod2-embeddings-flow-20261008.md): indexar texto completo del conjunto seleccionado, búsqueda semántica/literal por campo, ampliar contexto, verificar evidencia y limpiar temporales. Diseño/configuración listos; implementación y piloto pendientes. El runner actual y los resultados históricos conservan sus políticas.

**Actualización vigente del runner seleccionado (07/10/2026):** las carpetas nuevas conservan referencias oficiales y procesan documentos temporalmente, sin uploads permanentes R2. Limpieza automática tras readback PG y reutilización exacta de lecturas pagadas; históricos congelados preservados. [Flujo completo, imagen y comando](prod2-official-links-flow-20261007.md). Las secciones posteriores documentan la evolución y los pilotos anteriores.

Implementado y probado el06/10/2026 en un piloto acotado. Sustituye la fase metadata que guardaba todas las coincidencias como oportunidades. La corrida anterior fue cancelada y su carpeta eliminada; su entrada pública está retirada. `scripts/run_prod2_accepted.py` procesa máximo10 aceptadas y termina en LLM1. Resultado y evidencia por convocatoria: [piloto nuevo](prod2-accepted10-20261006.md). No está activo un barrido general ni scheduler.

Actualización posterior del 06/10/2026: el usuario pidió incluir todos los adjuntos sueltos y dejar ZIP/RAR para después. El script local usa ahora `prod2_direct_documents_v1`: PDF/DOC/DOCX sin selección por nombre, `.doc` convertido a PDF, comprimidos solo con enlaces registrados. [Implementación y QA del nuevo alcance](prod2-loose-documents-20261006.md). Este cambio se validó sin una nueva ingesta ni llamadas Gemini/Jev; el piloto productivo anterior conserva sus resultados históricos.

## Decisiones del usuario

- Umbral único: aceptar únicamente `affinity_index >= 60`. No hay banda40–59 ni selección automática adicional de ambiguos.
- Antes de Jev, candidatos mínimos en una bandeja temporal y deduplicación por IDs oficiales y versión de descripción/perfil/modelo/política.
- Rechazados no se promueven a oportunidades ni se hidratan/descargan con este flujo. Conservar una caché técnica mínima de decisiones para no repetir pagos.
- Al aceptar, guardar oportunidad y resultado Jev; después navegar a ficha, verificar identidad y guardar datos oficiales, cronograma e inventario/enlaces documentales.
- Descargar y verificar documentos pertinentes para requisitos, ejecutar Gemini LLM1 y guardar extracción, resumen, requisitos y evidencia. Finalizar ahí: sin LLM2, matching ni notificaciones nuevas.

El umbral mide afinidad comercial, no habilitación, inscripción ni probabilidad de ganar. Cambios de perfil/texto/modelo/política producen una versión nueva; otra keyword por sí sola no cambia la evaluación.

La reentrada HTTP a una ficha sin captura local conserva la query original (keyword, ejercicio, objeto y fechas). Después de iniciar la sesión de búsqueda habitual, `get_detail` busca primero la página capturada y, solo si no encuentra la identidad, las páginas -1, +1, -2 y +2 dentro del total verificado: como máximo cinco páginas de esa vecindad. No registra otras filas como candidatos ni cambia keywords. Convocatoria, procedimiento y descripción deben coincidir exactamente antes de abrir la ficha; una descripción cambiada exige una nueva versión Jev y detiene esta reentrada. Los errores nativos de contador, duplicados, protocolo y límites HTTP se propagan; no se usan como motivo para probar páginas adicionales. Las fichas ya capturadas conservan su replay sin búsqueda. Validado con páginas simuladas y contratos HTTP offline, sin demostrar todavía la recuperación productiva de 1006799 (06/10/2026).

## Redis: recomendación para la bandeja y distribución

Usar Redis para candidatos temporales y distribución a consumidores con confirmación; datos aceptados, checkpoints, reservas antes del pago y caché de decisiones pagadas permanecen en PostgreSQL. La BD final no muestra rechazados como oportunidades. Un rechazo puede conservar ID/hash/resultado/uso en una caché separada, sin ficha ni PDF.

La cola no debe perder trabajos pendientes por una caducidad arbitraria. Confirmar un trabajo después del commit y conservar una forma duradera de reconstruir pendientes si Redis se reinicia o pierde datos. Reservar el proveedor antes de llamar; una reserva sin respuesta exige conciliación y no vuelve a pagar automáticamente.

El piloto integra `RedisStage`: stage idempotente, reserva recuperable, ack tras persistencia y eliminación del payload confirmado, con dedup mínima retenida. PG conserva decisiones/reservas Jev independientes de oportunidades (`0038`), y las reservas de LLM1 existentes. Los manifests privados permiten reconstruir staging. Redis productivo tiene AOF y `noeviction`; se conecta por túnel a su IP Docker observada sin publicar puertos. Cinco contratos Redis reales validaron estas operaciones; no se simuló caída del servidor Redis. Los workers antiguos usan `worker_jobs` y permanecen detenidos.

El piloto usa1ejecutor secuencial, con pausa de2segundos entre requests y un advisory lock global. La propuesta de1 buscador,1 Jev,2 fichas/documentos y2 LLM1 (6 procesos) requiere una implementación y prueba adicionales, sesiones independientes y límite HTTP compartido. Discovery del piloto se acota a primeras páginas y al límite de candidatos; no certifica cobertura de los166keywords ni todos los ejercicios. El barrido general necesita checkpoints/paginación/particiones completos antes de desplegar consumidores paralelos.

## Enlaces oficiales

`persist_detail` conserva UUID, nombre, etapa, tipo, publicación y `source_url` oficial de cada documento. El enlace estable es al resolver oficial de SEACE; no debe presentarse como URL directa permanente al PDF. La descarga resuelve en ese momento la URL final, que puede contener ticket de Alfresco. Tickets, cookies y ViewState no se publican ni persisten como enlaces compartidos. Guardar SHA/original/derivado y evidencia al analizar, con caché por revisión para no repetir LLM1.

La VM actual rechaza acceso directo a SEACE con403. El colector local realiza consultas/descargas; los previews auxiliares privados permiten lectura desde BuenaPro. Esta limitación no se resolvió durante la revisión.

## Subcontratación: revisión completada

El schema `ParticipationV1` y la instrucción común de Gemini ya incluyen `participation.subcontratacion` y `participation.consorcio`. Estados: `permitted`, `prohibited`, `conditional`, `not_identified`; campos: `clause`, `page`, `conditions`. La ausencia de evidencia literal/página se normaliza a `not_identified`; condiciones explícitas convierten un permiso en `conditional`.

El recorrido PROD2 `extract_document` incluye esa instrucción tanto para bienes como servicios. `persist_completed_llm1` conecta `persist_extraction` y `persist_participation` en una transacción: JSON crudo/resumen/facets y columnas canónicas `subcontracting_status`/`consortium_status`. Comprueba reserva done, UUID, SHA original/derivado, modelo/prompt/schema, inventario actual y procedencia antes de proyectar. Rechaza evidencia histórica o cronología ambigua que sustituya una vigente. Los10 registros del piloto quedaron vinculados a la extracción/SHA exactos; no se ejecutó LLM2.

Validación de revisión:96 pruebas existentes de participación/persistencia/repositorio PROD2 pasan; se inspeccionaron las10 respuestas LLM1 guardadas del piloto sin nuevas llamadas:7 reportan subcontratación prohibida con cláusula y página,3 no identificada. Son resultados del extractor, no una nueva verificación legal independiente de los PDFs ni una evaluación de admisibilidad del convenio. La normalización comprueba texto no vacío y página positiva; todavía no contrasta la cita contra el contenido real de esa página ni estructura porcentajes/condiciones como números.

## Word y varios documentos: auditoría anterior al cambio de alcance

Revisión de código y 96 pruebas focalizadas aprobadas el 06/10/2026, sin consultas al portal, llamadas a modelos ni escrituras productivas. LLM1 recibe un PDF de análisis; no recibe directamente el archivo Word.

La tabla siguiente describe el selector anterior. El nuevo ejecutor de adjuntos sueltos lo sustituyó; ZIP/RAR quedan fuera del nuevo análisis por petición del usuario. Los helpers anteriores se conservan para históricos y otros recorridos.

| Caso | Comportamiento implementado |
| --- | --- |
| Word `.docx` seleccionado | Se convierte a PDF con LibreOffice y LLM1 analiza el derivado. Se conserva el original y su procedencia. |
| Word antiguo `.doc` | Puede descargarse y registrarse, pero no hay conversión implementada para LLM1; requiere revisión. |
| Dos o más documentos separados | Se selecciona un documento principal: bases integradas, luego TDR/EETT, luego bases genéricas; dentro de ese nivel, la publicación más reciente. No se combinan automáticamente todos los adjuntos. Un empate entre documentos complementarios requiere revisión; una copia PDF/Word del mismo nombre y fecha permite elegir el PDF. |
| ZIP/RAR con bases y un TDR/EETT complementario inequívoco | Se combinan en un PDF con manifiesto de páginas por archivo; LLM1 lee ambos. |
| ZIP/RAR con bases integradas | Se seleccionan las bases integradas; no se agregan automáticamente documentos anteriores. |
| ZIP/RAR con más de un complemento TDR/EETT | Se detiene para revisión por ambigüedad. Los miembros `.doc` todavía no participan en la selección. |

El inventario y los enlaces se guardan antes de seleccionar el documento principal. Si la selección es ambigua, la etapa queda en revisión antes del bucle de descargas. Cuando es inequívoca, el script intenta descargar todos los archivos soportados del inventario, pero solo analiza el PDF seleccionado o combinado. Descargar varios adjuntos no equivale a leerlos todos con LLM1.

La lectura de múltiples adjuntos sueltos, la conversión `.doc` y la procedencia por archivo/página ya están implementadas en el nuevo alcance. Pendientes: abrir archivos comprimidos cuando se autorice y validar de manera independiente la autoridad/versión documental y las citas. La lectura LLM no acredita por sí sola requisitos vigentes verificados. La extracción automática de ganador y monto adjudicado también continúa pendiente: [estudio de adjudicaciones](prod2-awards-study-20261006.md).

## Validación realizada y siguiente alcance

10 aceptadas (5 bienes/5 servicios),1 rechazo Jev53,25,10 cronogramas/inventarios completos,21 documentos descargados y10 extracciones con113facets.6 registros tienen inscripción abierta y4 están cerrados/adjudicados/desiertos.4 Jev/4 LLM1 nuevos; las demás respuestas válidas se reutilizaron. Coste nuevo estimado USD0,033417716; repetición0 llamadas/0 coste nuevo y conteos sin duplicados.772 tests offline pasan; contrato real PostgreSQL aislado y5 Redis reales aprobados; API productiva:10 fichas y10 previews PDF200 con SHA exacto. Las integraciones opt-in no corren en la suite por defecto.

Pendiente para la carga general: incorporar en el nuevo ejecutor todos los ejercicios observados, páginas y particiones ante499, reconciliación del portal, scheduling y consumidores por etapa. El usuario autorizó únicamente este piloto de10, no reanudar el barrido anterior. La VM conserva403 hacia SEACE; colector local y BD productiva compartida. Imagen estática y resultados completos en el reporte del piloto.
