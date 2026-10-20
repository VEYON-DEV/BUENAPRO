# Categorías preliminares PROD2 por título — ejecución20/10/2026

El usuario autorizó implementar, ejecutar y publicar la clasificación por título de los registros PROD2 almacenados. El alcance congelado contiene1.135 convocatorias y1.018 títulos distintos;117 filas reutilizan respuestas. El mapeo anterior y contratos menores quedan fuera.

Se conserva JEV1.13.0 con seis preguntas de categoría y una de claridad. El script no leePDF, perfiles, embeddings ni puntajes. Las etiquetas se guardan con contrato propio`title_commercial_classification_v1`; la clasificación documental válida prevalece. Los títulos ambiguos se muestran como«Por confirmar» y objetos fuera del catálogo como«Otros».

La migración0046 es aditiva y opt-in. El ledgerPG reserva antes del envío y la respuesta original se guarda antes de interpretar/aplicar; una reserva o envío incierto nunca autoriza otro pago. El comandoapply comprueba el título exacto actual. Los filtros usan las categorías efectivas antes del conteo y paginado.

Estado: implementación local validada; piloto, aplicación y publicación pendientes. Presupuesto orientativoUS$0.10, sin garantía de factura; proxy local del manifiesto792.220tokens/US$0.033273240. No se promete precisión semántica por confianza del modelo.

Validación inicial:29pruebasPython incluidasPGscratch,195Nodeaprobadas/2omitidas, arquitectura16dominios y detectorImpeccable sin hallazgos. Capturas, build y verificación productiva pendientes. Se conservan manifiesto/recibos privados en`tmp/prod2-title-categories-20261020/`; entradas ajenas intactas.
