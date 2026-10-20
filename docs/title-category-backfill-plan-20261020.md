# Clasificación preliminar por título — plan del 20/10/2026

Estado posterior: implementación y ejecución autorizadas por el usuario y publicadas el 20/10/2026. Ver [cierre de ejecución](./prod2-title-categories-execution-20261020.md). El resto conserva la estimación y planificación anteriores al pedido de ejecución.

Clasificar las licitaciones actuales con JEV usando únicamente su título/objeto completo es suficiente para una columna de navegación **Categoría**, marcada como preliminar. La lectura documental posterior prevalece cuando existe. Esta tarea prepara el plan y la estimación; no ejecuta JEV ni publica etiquetas.

## Alcance actual solicitado: solo PROD2

Confirmado por el usuario después de aclarar el total de la BD. Snapshot PG READ ONLY del 20/10 a las 13:10:21 de Lima: **1.135 registros PROD2 guardados**, de los cuales 1.134 tienen ficha enriquecida. Hay **1.018 títulos distintos**, 117 repetidos reutilizables y cero títulos vacíos. Para esta cotización se usa solo el título completo con el mismo prompt de seis categorías más claridad y `jev-1.13.0`.

Proxy local del JSON completo: **798.328 tokens**, US$0,033529776 calculados; margen heurístico superior US$0,0612402. Cotización orientativa **US$0,03–0,07 para todo PROD2**, con reserva sugerida US$0,10 que todavía no es un límite implementado. El precio real depende del uso reportado por el proveedor. No se ejecutaron inferencias ni clasificación pagada. Se conserva únicamente el snapshot compacto y recibo de esta preparación, cero bytes liberados.

## Alcance y costo

PostgreSQL productivo, transacción REPEATABLE READ READ ONLY del 20/10/2026 a las 12:24:51 de Lima: **1.134 filas SEACE enriquecidas**, exactamente el conjunto base del Buscador sin filtros. No significa que todas estén vigentes. Hay **1.017 títulos únicos** al compactar espacios, 117 filas repetidas reutilizables y ninguna categoría documental derivable con el catálogo actual. Los 1.736 IDs del mapeo son otro alcance: no todos están enriquecidos ni visibles en esta lista.

Se conserva `jev-1.13.0`. TypeSafe publica US$0,042 por millón de tokens de entrada y salida gratuita: https://docs.typesafe.ai/models (consultado el 20/10/2026). La estimación cuenta el JSON compacto completo, incluyendo instrucciones, criterios y título: 797.557 tokens proxy `cl100k_base`, entre 742 y 935 por título, para **US$0,033497394** calculados. Ese tokenizer no es el del proveedor: rango orientativo con margen **US$0,025–0,062**; presupuesto propuesto redondeado **US$0,10** para este inventario. Este monto aún no es un tope implementado ni cotización de factura. No incluye impuestos ni infraestructura; no hay PDF, OCR, embeddings, lectura LLM1 ni recomendaciones.

Para comparar, 1.736 títulos sin reutilización costarían aproximadamente US$0,057 con el mismo tamaño medio y US$0,104 con el margen superior; 10.000, US$0,329 y US$0,602. Son extrapolaciones del prompt, no nuevos inventarios ni autorización para pagarlos. Si se incluyen contratos menores se congela primero su propio inventario almacenado; no hace falta consultar SEACE para clasificar títulos ya guardados.

## Aclaración del alcance total de la BD

Consulta humana posterior, PG fresco del 20/10 a las 13:06:59 de Lima: **8.822 oportunidades canónicas** contando históricos y el legado PROD4. Las tablas de las dos fuentes actuales almacenan **1.135 filas PROD2 + 2.124 contratos menores = 3.259 UUID distintos**, con **2.875 títulos distintos** y 384 reutilizables. Son filas almacenadas, no vigencia comprobada. Las 1.134 del cálculo inicial son el subconjunto PROD2 con ficha enriquecida visible en el Buscador.

Los 7.024 resultados históricos, 106 filas PROD4 y 1.736 IDs del registro de mapeo no se suman sin deduplicar identidades. Varias fuentes/aliases pueden compartir un UUID y los históricos reutilizan la identidad de contratos menores. La reutilización por título evita pagos repetidos, sin fusionar licitaciones distintas.

Extrapolando el tamaño medio del prompt ya medido a los 2.875 títulos de PROD2+menores: alrededor de **US$0,10–0,20**, incluyendo margen. No se midió la tokenización de esos títulos nuevos ni el costo de todo el archivo canónico de 8.822. Antes de ejecutar el alcance ampliado se necesita inventario por ID/título/hash congelado; este conteo agregado no ejecuta ni autoriza inferencias. Recibo compacto retenido, cero bytes liberados.

## Categorías y decisión

Reutilizar las seis categorías versionadas: **Licencias y suscripciones**, **Desarrollo de software**, **IA y automatización**, **Industrial**, **Cloud e infraestructura**, **Soporte y mantenimiento**. Una licitación puede mostrar dos etiquetas en la misma columna; por ejemplo, suscripción ChatGPT = Licencias + IA, mantenimiento SCADA = Industrial + Soporte. Automatización industrial sin IA expresa no se etiqueta como IA; licencia municipal no es licencia de software. Desarrollo exige creación de código: instalar/configurar un producto no basta. Para esta clasificación por título, Soporte y mantenimiento se limita deliberadamente a tecnología/industrial; el eje documental por sí solo no impone ese límite de área. **Otros** y **Por confirmar** distinguen objeto ajeno de título insuficiente y no fabrican etiquetas.

Una solicitud por título único: `state = {"titulo": "<objeto completo>"}` y seis preguntas `noul`, una por categoría, más una pregunta de claridad. Sin perfil DIN, contratos de la empresa, texto del PDF ni puntaje. Las probabilidades técnicas de JEV sirven para enviar respuestas ambiguas a revisión; no se muestran como probabilidad de ganar. Los umbrales requieren un pequeño piloto validado antes del lote; no se promete precisión por confianza del modelo.

## Script previsto

1. `prepare`: snapshot de IDs/títulos/hashes sin cambios productivos, catálogo/prompt/modelo congelados, títulos vacíos pendientes y categorías documentales reutilizadas.
2. `dry-run`: deduplicación por título completo con espacios compactados; conservar números, marcas, tildes y negaciones. Estimar el prompt completo y mostrar IDs/cantidad/costo antes de ejecutar.
3. `execute`: piloto acotado y revisión de casos límite, luego concurrencia/pacing finitos conforme al límite real de la organización. Reserva y recibo durables por SHA de título + catálogo + prompt + versión del modelo; repetir el comando reutiliza respuestas conocidas. Envío incierto se conserva sin reintento automático. Un límite monetario explícito debe implementarse antes de utilizarlo como garantía.
4. `apply`: solo respuestas válidas con título actual idéntico al hash congelado; títulos cambiados vuelven a preparación, sin sobrescribir. Guardar fuente/ID, categorías/estado, origen `title_jev`, carácter preliminar, versiones, recibo, uso y costo. Una respuesta de título idéntico se comparte sin pagar por cada tenant/convocatoria.
5. UI/API: una columna **Categoría**, filtro por las mismas opciones y etiqueta discreta **Por título**. Usar clasificación documental válida primero, título preliminar vigente después, y Por confirmar como salida explícita. Aplicar filtros antes de contar/paginar. La implementación y publicación se hacen después del plan, con QA visual y Git; no se cambian por esta estimación.

Contrato separado propuesto `title_commercial_classification_v1`, almacenado como clasificación derivada por fuente/ID/hash. No rellenar `commercial_taxonomy_v1`, páginas ni citas inventadas; no escribir `raw_extraction_json`, permisos, notas ni matching. Los filtros actuales consultan solo evidencia documental, por lo que guardar resultados por título requiere cableado explícito para que aparezcan. No tocar planes, bindings ni procesos del lote DIN activo.

## Validación y pendientes

Principal: lectura de esquema/API y conteo PG fresco sin mutaciones, inventario/hash, deduplicación y aritmética offline. Revisión independiente confirma IDs/deduplicación y el cálculo; se integra su ajuste de desarrollo frente a configuración y se recalcula el payload completo sin modelos. Plantilla y recibos privados en `tmp/title-category-plan-20261020/`. El conteo es una foto, no un total fijo mientras el lector sigue enriqueciendo casos. Antes de ejecutar hay que actualizar/congelar el alcance definitivo, implementar el ledger/STOP, validar el piloto y conectar la columna/filtro. **No hay ejecución pagada autorizada por este plan.**

Limpieza: cero bytes liberados; se conservan el inventario compacto de títulos y la plantilla para esta preparación. No se crearon PDFs, renders ni copias de originales; entradas y recibos del lote DIN permanecen intactos.
