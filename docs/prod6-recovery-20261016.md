# Recuperación de Contratos menores — 16/10/2026

El usuario pidió volver a encender la ingesta. El servicio dedicado había
terminado el 14/10 a las 13:35:28 UTC (08:35:28 de Lima), con salida 2 por un
`HTTPStatusError` al refrescar el contrato 100984. El código anterior conservó
la clase de error, pero no el estado HTTP: no se le atribuye retrospectivamente
un 429, 403 o 5xx. El despliegue posterior del frontend no apagó el worker.

El diagnóstico de solo lectura encontró tres trabajos propios pendientes,
ninguna reserva en vuelo y ningún coordinador. Las 17 lecturas previas tienen
sus tres recibos ligados; no hay guard abierto ni envíos sin respuesta. El costo
local conocido de esas lecturas es US$0.072873, contado una sola vez por resultado.
Los GET actuales de ficha e inventario devolvieron 200. La API de menores sigue
disponible y devuelve 53 puntajes guardados para DIN.

La primera recuperación obtuvo 210 registros, cuatro candidatos y completó seis
fichas/tres descargas antes de otro error HTTP de descarga (archivo 407693).
Su comprobación posterior devolvió 200 sin leer el documento. Una segunda
recuperación diagnosticada terminó por HTTP durante descubrimiento. Ninguna
de estas dos recuperaciones llegó a enviar una lectura nueva: siguen existiendo
los mismos 17 resultados durables. Estos errores históricos tampoco conservaron
el código HTTP. No se siguió relanzando el contenedor sin diagnóstico.

## Cambio limitado al transporte gratuito

El runtime espera al siguiente ciclo de 1800 segundos tras agotar los reintentos
existentes de búsquedas con HTTP 408/425/429/500/502/503/504, timeouts, errores de
red o protocolo remoto. Un trabajo paralelo de ficha/descarga con uno de esos
fallos vuelve a `pending` con `run_after` futuro y propiedad comprobada. Los
demás trabajos pueden avanzar. El estado diferencia `source_backoff`,
`sweep_deferred` y `sweep_drained`; `--once` devuelve 2 si queda trabajo diferido.

Los errores permanentes de acceso en búsquedas/fichas, integridad, protocolo
local y errores no clasificados siguen deteniendo el servicio. Los fallos de `extract_tdr` y
`derive_summary` también conservan la parada, aunque su excepción sea HTTP.
El guard de pagos y las respuestas guardadas no cambian. El diagnóstico nuevo
conserva tipo/estado HTTP seguros, sin URL, body ni credenciales.

El primer release diagnosticó un 403 concreto al descargar el archivo 398091
del contrato 100017. Las descargas con HTTP 401/403/404/410 quedan ahora retenidas
como `pending`, con `run_after=infinity` y un bloqueo de documento ligado al
contrato/archivo/perfil/namespace. No se reintentan ni se evitan controles de
acceso. La misma identidad bloquea nuevas copias de descarga/lectura en ciclos
posteriores, mientras los demás contratos siguen avanzando. `sweep_blocked`
distingue estos pendientes manuales de una cola drenada; `--once` devuelve 2.

El servicio conserva el mismo alcance configurado, carpeta de recibos, lock,
perfil de ingesta y paralelismo 3 HTTP/2 IO/1 lectura. Activar este barrido no
activa evaluaciones LLM2 ni notificaciones. DIN continúa disponible como perfil
de lectura del listado compartido; sus interruptores de análisis automático y
de ingesta propia no se modifican como efecto lateral.

## Resultado y limitación operativa

La imagen `buenapro-worker-prod6:http-recovery-f2767e8`, digest
`sha256:516b923ba6c75ee1d4c4b06b729239597e0bffe17fc8f2effeb8bb4c5fdca640`,
se construyó desde Git limpio del commit
`f2767e8ecc825acc44b7eb5e859be6904f6ee155`. Sus cinco hashes de runtime y etiqueta
OCI coinciden; solo cambió `prod6/runtime.py` respecto al código anterior. Pasaron
92 pruebas offline y la configuración/los ocho servicios ajenos se conservaron.

La VM también recibió 403 en consultas de fichas e inventarios, no solo en una
descarga. El gateway respondió `Forbidden` sin declarar la causa. No se acreditó
que espaciar las peticiones lo resolviera y no se presenta el logging ni el
tratamiento de errores como solución de ese acceso. Las mismas consultas públicas
respondieron 200 desde la conexión normal del Mac.

Se transfirieron los 17 resultados previos y recibos asociados al lector local,
verificando todos sus hashes y prompts antes de ejecutarlo. El lector local
PID2528 usa el mismo código, perfil y PostgreSQL de producción por túnel SSH;
las claves permanecen en memoria. El contenedor de la VM está detenido y PG
confirma un solo coordinador. Esta recuperación depende de que el Mac permanezca
encendido; el retorno a una ejecución permanente en VM sigue pendiente.

El primer ciclo local terminó `sweep_drained` a las 12:27 UTC (07:27 de Lima):
210 registros fuente, cuatro candidatos, seis refrescos de ficha, nueve trabajos
de identificación documental, trece trabajos de extracción (tres lecturas nuevas
y diez reusos) y cuatro derivaciones. No quedan trabajos propios pendientes ni
reservados. Hay 20 juegos de recibos completos, sin guard ni envíos inciertos;
los 17 anteriores son idénticos. Los 53 matches de DIN y los ocho servicios ajenos
permanecen idénticos. No hubo evaluaciones LLM2 ni notificaciones.

La API de DIN devuelve siete contratos abiertos con lectura; los tres nuevos son
101976 (Banco de la Nación), 102186 (OSITRAN) y 102597 (AGROMERCADO). No tienen
puntaje LLM2; «Con puntaje» los excluye. Los 53 puntajes anteriores se conservan
y tres siguen abiertos. Las lecturas automáticas no acreditan elegibilidad ni
aprobación semántica.

El costo local nuevo conocido es US$0.01263575; total de este lector
US$0.08550875, sin duplicar los US$0.072873 anteriores. No es una conciliación de
factura. No se guardaron originales nuevos ni se subieron a R2.

Los recibos compactos y las entradas activas se conservan en
`tmp/prod6-recovery-20261016/`, especialmente `local-runtime/`. Antes de pasar la
operación a otro host, detener el lector, confirmar ausencia de coordinador y
sincronizar recibos/guards; nunca reactivar la VM usando solo su copia anterior.
No se creó una automatización de Codex.
