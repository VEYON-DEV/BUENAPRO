# Categoría de PROD2 y cobertura de la selección DIN

El 20/10/2026 se reprodujo la interacción productiva con la sesión DIN y la fuente
PROD2. El botón **Aplicar filtros** sí enviaba `commercial_category=industrial` y
el servidor devolvía únicamente filas de esa categoría (136 en el corte inicial).
Antes de enviar, el selector podía mostrar un borrador Industrial mientras la URL
y la lista continuaban sin categoría. Los formularios de búsqueda y categoría
mantenían estados distintos; no se encontró un fallo de clasificación SQL.

Buscador SEACE aplica ahora la categoría al elegirla, después de actualizar el
control y retirar los filtros comerciales legacy. Conserva fuente, perfil,
búsqueda y tipo, reinicia página y muestra **Aplicando…** durante el envío.
Contratos menores conserva aplicación manual para combinar sus controles.
Una segunda prueba reprodujo una carrera: pulsar Buscar 50 ms después del
autoenvío podía cancelar la navegación y perder la categoría. El toolbar
comparte ahora el borrador con los campos ocultos de Buscar y los enlaces de
tipo/puntaje; conserva también la consulta nueva y retira los criterios legacy.
La regresión reproduce el primer GET demorado 600 ms y verifica la URL final.
El aspecto Glass, las seis categorías y los criterios documentales se conservan.

## Las 157 seleccionadas

La afinidad DIN ya calculada no sustituye la clasificación comercial. Solo 13
seleccionadas tenían etiquetas válidas al inicio. Se congelaron los 157 títulos
actuales, se reutilizaron 14 recibos conocidos y se enviaron 143 solicitudes nuevas
JEV, sin repetir lecturas, afinidad, descargas o llamadas históricas inciertas.

El readback PostgreSQL fresco con el SQL exacto de la web verificó:

- 157 publicaciones e identidades vigentes, con 157 recibos/etiquetas válidos.
- 144 proyecciones aplicadas y 13 intactas; cero pendientes u obsoletas.
- 61 con categorías concretas, 21 Otros y 75 Por confirmar.
- 156 etiquetas de título y 1 clasificación documental prioritaria.
- Categorías de esta selección: 39 Industrial, 27 Soporte, 3 Cloud, 2 Licencias y 1 IA;
  las etiquetas pueden coexistir y no deben sumarse como filas únicas.
- Gasto nuevo US$0.005883528; los 14 recibos reutilizados suman US$0.000571410
  informativos. El costo conocido del scope es US$0.006454938, sin pagos inciertos
  nuevos; no sumar el costo histórico de nuevo como gasto incremental.

La selección inicial de 154 industrial / 3 IA se basó en títulos y no acredita una
clasificación semántica. Los 75 casos ambiguos no se forzaron a Industrial ni se
reenviaron para obtener otra respuesta. La clasificación por título tampoco
acredita requisitos, vigencia, LLM2 o elegibilidad.

Plan: `ea7102a9a05831b38c90174c1f885c91fe9f3ff21fd7c3b418d34b59d2275933`.
Resultados públicos: [readback compacto](prod2-selected-title-category-results-20261020.json).
Recibos privados: `tmp/prod2-selected-title-categories-20261020/`.

## Validación y publicación

Código web: `f2d55386243d1bd7d62b4431c2b779a2430dd53c`, subido al remoto antes
de construir desde checkout limpio. Imagen activa:
`buenapro-web:category-search-f2d5538`,
`sha256:1b3cadfec82d47c97cb594aae5d7d329b250e9f32056251a76d9ad9c999f1d74`.
Activación verificada a las 21:35:43 UTC: 606 hashes exactos, solo web recreada,
los 10 contenedores restantes, configuración y entorno intactos. Sin migraciones
ni activación de workers por el despliegue.

QA final local y productivo: tres capturas por entorno (1600/1024/390 px),
revisadas contra Glass, sin overflow ni errores de página. Elegir y quitar
categoría, reemplazar legacy y conservar perfil, tipo, búsqueda y página verificados.
APIs productivas: 164 Industrial, 329 Licencias y 18 IA; cada fila devuelta coincide
con su categoría. Una prueba productiva adicional confirma que Buscar 50 ms
después del autoenvío conserva Industrial y el texto nuevo. Evidencia final:
`qa-local-final/`, `qa-production-final/` y
`live-search-race-final.private.json` dentro de la carpeta de QA de esta tarea.

Regresión: 37 pruebas Node (incluye seis flujos Chromium/React reales), 9 pruebas
SQL sobre PostgreSQL desechable, TypeScript y arquitectura de 16 dominios aprobados.
Los flujos cubren elegir, reemplazar, limpiar, retirar criterios legacy y mantener
menores manuales; perfil/fuente/búsqueda/tipo/paginación preservados.

El diagnóstico de rendimiento midió una página en ~4.95 s de PostgreSQL. La variante
local de consulta `OFFSET0` solo mejoró~7 % con filas/orden idénticos y no se publicó.
La optimización de keywords queda separada: no se cambiaron índices, esquema ni
reglas para corregir la interacción.

Evidencias propias: `tmp/prod2-category-filter-fix-20261020/`,
`tmp/prod2-category-performance-20261020/` y
`tmp/commercial-filter-submit-20261020/`. Se conservan recibos y entradas de
lecturas pendientes. La última purga retiró 81.226.662 bytes de caché Next propia,
después de cerrar el servidor QA. Se conservaron capturas, recibos de pago y
entradas pendientes; los cortes anteriores de limpieza se registran por separado
en `cleanup.private.json`.
