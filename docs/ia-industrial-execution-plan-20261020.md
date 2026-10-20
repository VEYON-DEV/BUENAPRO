# Plan de ejecución: IA e industrial para DIN

Plan preparado el 20/10/2026, hora de Perú. No ejecutado: no se consultó SEACE,
PostgreSQL/Redis ni proveedores durante esta preparación. El código de categorías
y lectores está en Git; este documento no despliega ni activa servicios.

## Alcance de esta primera etapa

- **IA y automatización:** IA explícita, chatbots, agentes y automatización de
  procesos/RPA; licencias o cloud solo cuando el objeto sea de este rubro.
- **Industrial:** tableros, electricidad/control, PLC/SCADA, instrumentación,
  equipos electromecánicos, instalaciones y mantenimiento industrial.
- **Empresa evaluada: DIN.** La afinidad JEV conjunta existente no acredita
  capacidad DIN y no permite sumarle experiencia o profesionales de VEYON.
- Primero, aprovechar el mapeo PROD2 cerrado: publicación 04–20/10/2026,
  Bien/Servicio y años de convocatoria 2004–2026. No repetir su barrido de
  8.418 combinaciones ni ampliar la ventana silenciosamente. Si se ejecuta otro
  día, revisar vigencia al corte real; publicaciones posteriores necesitan un
  delta separado, no editar este mapeo cerrado.
- Contratos menores es una segunda fase restringida; no encender el radar de
  todas las líneas de la empresa para conseguir solo estos dos rubros.

## Selección inicial sobre el mapeo cerrado

El universo tiene 1.736 IDs únicos, 1.434 nuevos frente a los ledgers consultados y
302 históricos sin baseline comparable. Los 302 requieren revisión de hashes y
lecturas; no son 302 documentos que haya que volver a pagar.

Inventario léxico preliminar sobre los 1.736 IDs, con clases excluyentes:

| Grupo | IDs | Uso en el plan |
| --- | ---: | --- |
| Núcleo propuesto | **85** | 83 industriales/eléctricos, un chatbot IA y una automatización de proceso |
| Ambiguos | **94** | Confirmar dominio y objeto antes de incluir; no enviar directamente a modelos |
| Coincidencias léxicas pendientes de revisar | **228** | Revisión local para rescatar candidatos sin ampliar a todos los rubros |
| Ruido de substrings conocido exclusivo | **14** | Separado; no es rechazo JEV ni decisión semántica |
| Sin señal explícita en el título disponible | **1.315** | Fuera de prioridad inicial; no prueba ausencia del rubro en las bases |

Los dos casos explícitos son **1047579**, suscripción chatbot IA AIVO/PaaS de
RENIEC, y **1047199**, automatización de acondicionamiento catastral. No se halló
un título que acreditara explícitamente desarrollo de agentes; no convertir
ERP/BPM/SGD en IA por inferencia. El núcleo industrial incluye servicios y activos
eléctricos/electromecánicos amplios: es propuesta para filtro, no capacidad DIN
acreditada. Antes de congelar el lote ejecutable, resolver las coincidencias
pendientes y los ambiguos pertinentes mediante metadatos disponibles y ficha.

**63 del núcleo y 88 ambiguos** están fuera de los 32 IDs anteriores de keywords
industriales. El recibo conserva todos los IDs, razones y vínculo con 153 páginas
caché verificadas: `tmp/ia-industrial-plan-20261020/sector-inventory.private.json`,
SHA-256 `59c2a2e933f8ded567aadc3cf97b95cff173df979c7545ebe0555b4ac5224129`.
Los flags de ruido y subgrupos técnicos se solapan y no deben sumarse a esta tabla.

La selección usa el texto del objeto cacheado tal como fue devuelto por el
listado y conserva todas las asociaciones de búsqueda. No reemplaza las bases.
Son candidatos para revisión, no un censo semántico ni oportunidades vigentes.
No limitar industrias a los 32 IDs de keywords industriales: existen títulos
pertinentes fuera de esas consultas. No considerar «ia» dentro de una palabra,
«LLM» en ILLMU, «RPA» en carpas/CORPAC, HPLC como PLC o CASCADA como SCADA.
Las coincidencias ambiguas quedan separadas, sin rechazo definitivo automático.

## Recorrido previsto

| Etapa | Acción | Condición para avanzar |
| --- | --- | --- |
| 1. Congelar selección | Lista única por convocatoria/procedimiento con hash, título completo y prueba de origen del mapeo | Report/handoff/cachés válidos; sin identidades contradictorias |
| 2. Reutilizar | Revisar en PG decisiones JEV, lecturas, matches y reservas anteriores | Reutilizar por identidad/hash; conservar rechazos y revisiones; inciertos quedan bloqueados |
| 3. Afinidad JEV | Solo decisiones nuevas necesarias, sobre descripción y contexto congelados | Mantener el gate actual Jev>=60; no confundir afinidad con elegibilidad o sector confirmado |
| 4. Ficha oficial | Comprobar identidad, fechas, estado, etapas y documentos antes de pagar lectura | Abierto y rubro sustentado; cerrado se conserva sin lectura nueva, desconocido se retiene para revisión |
| 5. Lectura LLM1 | Elegir las bases/TDR/EETT pertinentes; embeddings V5 y OCR local parcial | SHA y documentos actuales verificados; clasificación comercial con citas y páginas; sin fallback PDF automático |
| 6. Evaluación DIN | Reglas y JEV para puntaje, Flash-Lite para explicación concreta | Fuente suficientemente leída; experiencia, monto, rubro y profesionales atribuibles a DIN |
| 7. Verificar publicación | Readback de fichas, cronogramas, referencias, categorías, puntajes y costos | Sin duplicados; revisiones y costos inciertos explícitos; originales temporales cerrados limpiados |

Abrir una ficha PROD2 puede requerir una búsqueda JSF acotada para reencontrar su
fila/acción nativa. Reutilizar el mapeo evita otro barrido global, pero no promete
cero búsquedas de apoyo. No persistir tickets, cookies o ViewState como enlaces.
Los originales nuevos de PROD2 siguen temporales; se conservan UUID, SHA, citas,
cronogramas y referencias oficiales en PostgreSQL, sin originales nuevos en R2.

La lectura LLM1 nueva devuelve etiquetas documentadas; las seis categorías se
proyectan localmente sin una llamada adicional. Una lectura histórica válida no
se repite solo para obtener una etiqueta: puede seguir disponible en Todas.

## Piloto y continuación

1. Seleccionar hasta **10 candidatos únicos**: primero los IA explícitos que
   sigan abiertos y después industriales claros. Registrar suplentes sin pagar.
2. Máximo **10 documentos nuevos únicos** y **10 solicitudes LLM1 nuevas**.
   Una solicitud puede agrupar varios documentos: son contadores distintos. El
   tope por documento requiere un guard adicional aún pendiente; el flag
   `--max-llm1-calls` no lo implementa. No afirmar diez fichas completas si el
   límite documental se alcanza antes.
3. Un coordinador PG; hasta cuatro tareas HTTP gratuitas con pacing global de
   dos segundos y dos consumidores/modelos. OCR local acotado a dos documentos.
   No lanzar diez modelos de golpe ni coordinadores paralelos del mismo lote.
4. Límites de llamadas propuestos del piloto: 10 candidatos, 10 admitidos,
   30 JEV combinando afinidad/selector, 10 LLM1 y 100 batches de embeddings.
   Son límites finitos; los tamaños de archivo y páginas conservan los guards V5.
5. Tras verificar el piloto, continuar con tandas de hasta diez del mismo scope
   congelado, checkpoints y recibos por etapa. Reutilizar resultados ya pagados;
   no copiar ni reconstruir carpetas antiguas para forzar reintentos.

**No usar `--all-results` en el piloto:** actualmente elimina los topes de
candidatos/admitidos/JEV/LLM1 y embeddings. Tampoco existe un flag general de presupuesto USD
que permita prometer un corte monetario exacto. El límite de embeddings es de
requests/batches y no equivale a documentos ni a un importe fijo.

## Costo y tiempo

Usar como base **histórica**, no cotización ni factura: la muestra anterior de
diez documentos LLM1 costó US$0,125169 y treinta evaluaciones DIN ya leídas
costaron US$0,121862954. Con diez documentos de tamaño comparable y diez
evaluaciones, la referencia aritmética es **US$0,166 antes de extras**. No es una
tarifa por solicitud actual: una solicitud V5 puede agrupar varios documentos. JEV de
preselección/selector, embeddings, tamaños mayores, tokens de clasificación,
fallbacks permitidos y revisiones pueden cambiar ese resultado.

Propuesta: una reserva orientativa de **US$1 para el piloto**, con corte de
métricas antes de ampliar. No es un límite monetario implementado ni autorización
para comprar créditos o modificar gasto. Si se exige un techo duro, implementar
antes reserva USD atómica por etapa, con admisión por costo máximo y bloqueo ante
uso incierto, además de los topes de llamadas existentes.

No extrapolar el tiempo de 2h44 del mapeo al tiempo de lectura. Cronometrar el
piloto separando HTTP, descarga, OCR, embeddings, LLM1 y matching. Después estimar
el resto con el número real de documentos nuevos y las latencias observadas; sin
conocer tamaños/escaneos no se promete un tiempo total cerrado.

Matching sobre datos listos puede planificarse en tandas de 50 y reservar
10–20 minutos por tanda como margen operativo; no incluye la ingesta ni garantiza
ese tiempo. Los antecedentes de 30/50 casos ya leídos no representan PDFs nuevos.

Fuentes locales: `docs/prod2-full-processing-cost-estimate-20261020.json`,
`docs/prod2-pilot10-20261005.md` y
`docs/dinaut-two-dot-seace-30-operations.md`. Los recibos anteriores y los costos
históricos inciertos permanecen intactos.

## Preparación técnica necesaria antes de ejecutar

1. **Importación del handoff a lectura seleccionada.** El CLI actual no tiene
   `--handoff` ni `--candidate-id`; el mapping no puede pasarse como
   `--embedding-resume-from`. Añadir un modo explícito que valide el mapping y
   selección por SHA, emita solo IDs elegidos y conserve fila/query/observación/
   hashes/identidad, con carpeta y manifest nuevos. No falsificar una corrida
   seleccionada ni volver a recorrer todas las keywords.
2. **Scope nuevo de matching DIN.** Los wrappers anteriores pertenecen a cohortes
   fijas de tres puntos o dos puntos (33/50/30), con carpetas y resultados pagados.
   Crear scope/freeze propio para estos IDs, sin restricción artificial a dos o
   tres puntos, sin reemplazar matches existentes y con readiness/CAS/provenance.
3. **Gate económico y de incertidumbre antes de leer.** `enrich` hoy registra el
   estado temporal y continúa a documentos; añadir salida explícita para cerrado
   o apertura no confirmada antes de previews/descarga/selector/embeddings/LLM1.
   Añadir admisión por documento único para el máximo de diez documentos nuevos,
   separado de los contadores existentes de solicitudes LLM1/embeddings.
   Revisar históricos antes de afinidad en el modo por IDs para evitar pagos
   innecesarios: actualmente la guarda histórica se ejecuta después de JEV.
   Completar STOP del lote ante cualquier proveedor con envío incierto: la parada
   global actual cubre embeddings enviados y 403/429 de fuente; otras etapas
   conservan reservas por identidad pero no todas paran automáticamente el lote.
4. **Preflight de ejecución.** Git remoto limpio y hashes runtime registrados;
   un único propietario PG; estado Redis/colas propias e históricas; perfil y
   evidencias DIN actuales; tarifas/configuración/cuota válidas para el lote;
   acceso legítimo a ficha/documentos. Un 403/429 previo requiere evidencia nueva
   de recuperación; no repetir pruebas rutinarias ni inferencias de diagnóstico.
5. **Scope de PROD6 antes de activarlo.** Su clasificación comercial nueva ocurre
   después de leer. Su discovery actual usa todas las líneas activas del perfil;
   el flag de taxonomía no restringe rubros. Preparar un allowlist inmutable de
   líneas/segmentos y un gate sectorial antes de enqueue/descarga/LLM1, sin editar
   el perfil global. Bien/Servicio y estado Vigente deben quedar explícitos en
   el alcance autorizado; no prometer cobertura industrial con segmentos ajenos.

Estos son requisitos del plan, no funciones ya implementadas por este documento.
La especificación privada del lote se guarda como planning_only, no como un
manifest ejecutable ni como autorización automática de pago.

## Reglas del resultado y del cierre

- Puntaje DIN por rubro, experiencia pertinente, experiencia económica y perfiles
  profesionales. RUC/RNP, declaraciones y formatos administrativos no reducen
  el puntaje de capacidad. Una brecha económica pequeña se cuantifica y explica;
  no se transforma en cumplimiento automático ni negociación garantizada.
- Lo que diga Por leer no entra a matching. El estado leído tampoco basta:
  exigir readiness de requisitos/cobertura/procedencia. Revisiones parciales y
  puntajes provisionales permanecen explícitos.
- Primer envío incierto o error de acceso/cuota/servicio: detener nuevas
  solicitudes, conservar las respuestas en vuelo y los pendientes. No convertir
  incertidumbre en costo cero ni relanzar por cambiar carpeta.
- Cerrar con conteos por ID: descartado por sector, bajo JEV, cerrado, reutilizado,
  leído nuevo, en revisión e incierto; reconciliar PG/Redis y recibos sin sumar
  cohortes históricas dos veces. Verificar etapas/referencias y categorías en API.
- Limpiar solo temporales propios cerrados tras verificar persistencia. Conservar
  entradas de trabajos pendientes o inciertos y todos los hashes/recibos necesarios.
