# Verificación de guardadas SEACE y Gantt

Corte: 16/10/2026, 12:30–12:33 UTC (07:30–07:33 Lima). Pedido: solo verificar el comportamiento existente; sin guardar/quitar oportunidades ni modificar producción.

## Resultado

- **Solo guardadas:** PROD2 consulta `saved_opportunities`, restringida al tenant de la sesión, sin afinidad ni límite de filas. Guardar usa PUT y quitar DELETE; el control refresca la ruta tras confirmar el servidor. El cronograma consulta PostgreSQL al abrirlo o pulsar Actualizar.
- **Etapas registradas completas:** los 1.126 procedimientos almacenados tienen 7.451 etapas. El normalizador real preservó las 7.451, con nombre y ambos límites interpretables; ningún intervalo tenía días invertidos usando las coordenadas de Lima del Gantt. Esto acredita la representación del dato almacenado, no la vigencia ni exactitud frente a una nueva consulta SEACE.
- **Límite funcional:** el servicio elimina la oportunidad completa cuando vence su cierre conocido de ofertas, aunque esté guardada y queden etapas posteriores. Navegar a una ventana histórica no la recupera. Actualmente no se cumple una lectura literal de «todas las guardadas con todas sus etapas» durante todo el proceso.
- **Ventana:** desktop dibuja las etapas que intersectan los 14/30/60 días seleccionados; las externas permanecen en la respuesta y requieren mover el periodo. En móvil, el desplegable ofrece todas las etapas de cada oportunidad incluida. Una etapa incompleta se conserva sin inventar una barra; no se encontraron casos así en este corte.
- **DIN:** cero guardadas y cero filas en `GET /api/cronograma?source=prod2`, coherentes entre PostgreSQL y la API autenticada. No se creó una guardada para probar; el ciclo guardar→Gantt→quitar no se repitió hoy en producción.

## Validación y alcance

36 pruebas focales aprobadas: bookmarks/aislamiento/autenticación, consulta/normalización, coordenadas/solapamientos y fechas/precisión. Auditoría independiente de componentes y normalizador integrada por el principal; sus pruebas se solapan y no se suman de nuevo.

Cuatro archivos publicados son idénticos por SHA-256 a los auditados: `procurementTimeline.ts`, `prod2Analysis.ts`, `gantt.ts` y `TimelineBoard.tsx`. No cambió código runtime ni UI; no se necesita despliegue.

El helper de corpus corrigió su propio comparador para usar días de Lima, igual que el Gantt, en lugar de comparar lexicográficamente fechas de distinta precisión. No era una anomalía del producto.

Evidencia en `tmp/seace-gantt-verification-20261016/`: `readback.private.json` y `stages.private.json`. Sesión efímera solo en memoria; PG READ ONLY. Cero escrituras de negocio, descargas originales, llamadas SEACE/modelos, reinicios o gastos.

Limpieza: 0 bytes de descargas/renders generados o pendientes. Helpers y recibos compactos conservados para trazabilidad; entradas y recibos activos de PROD6 intactos.
