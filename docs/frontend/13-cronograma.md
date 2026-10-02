# Cronograma de oportunidades

Ruta: `/cronograma?source=prod6|prod4&start=YYYY-MM-DD&days=14|30|60&q=`.

## Objetivo y diseño

Priorizar los próximos cierres sin perder las demás etapas oficiales. Una sola superficie de trabajo amplia: identidad fija a la izquierda, días arriba, etapas en carriles que no se solapan y línea de Hoy. Mantiene Inter, paleta, materialidad y controles de `04-oportunidades.png` y de System Components Kit; el Gantt reemplaza expresamente la tabla/inspector por petición del usuario. No hay imagen aprobada específica de Gantt ni una segunda vista paralela de Oportunidades.

Desktop/laptop: barras seleccionables con nombre completo, fechas y enlace al detalle. Scroll interno horizontal/vertical, sin overflow de página. 14/30/60 días, Hoy y periodos anterior/siguiente. Móvil: agenda por oportunidad y todas las etapas desplegables, sin controles de periodo que no aplicarían a la agenda. Fuente y búsqueda conservan estado en URL.

## Datos y reglas

- `GET /api/cronograma?source=prod6|prod4&q=`: autenticado, tenant de sesión, perfiles activos, una consulta en lote por fuente, sin límite de filas ni llamadas LLM.
- Sin evaluación documental vigente: afinidad 2 o 3 puntos. Con evaluación vigente: solo `ambar` o `verde`, aunque el fit preliminar sea alto. Evaluaciones rojas/grises excluidas.
- PROD4 solo radar tecnológico actual; PROD6 contratos vigentes en los rubros del tenant. Presentación/cotización vencida excluida. Cierre desconocido se conserva sin afirmar que esté abierto.
- Todos los eventos oficiales normalizados por `procurementSchedule.ts`. Menores incluyen consultas/cotización y publicación cuando `fecPublica` existe en la respuesta original; mayores muestran todas las etapas publicadas, no un conjunto fijo inventado.
- Fecha sin hora conserva precisión de día. Urgencias con hora y urgencias por día sin hora se comunican por separado. Registro no sustituye al cierre de ofertas.
- Las barras reflejan posición temporal, no ejecución jurídica. Color de afinidad/puntaje separado del color de etapa. Resumen amarillo/verde no es una garantía de adjudicación.
- Actualizar vuelve a consultar PostgreSQL; no ejecuta scraping ni LLM. El worker existente alimenta la fuente de verdad. Los estados temporales se recalculan cada minuto.
- Empty/error recuperables, teclado Enter para etapas, labels en controles, foco global visible. Cambiar fuente invalida respuestas pendientes.

## Verificación

`scripts/qa_timeline.py`: lectura de datos reales a través del túnel local, sin mutaciones. Capturas `docs/new-style/qa/gantt-prod{4,6}-{desktop,laptop,mobile}-20261002.png`. Pruebas puras para fechas/carriles y backend para elegibilidad, precisión, aislamiento y autenticación.
