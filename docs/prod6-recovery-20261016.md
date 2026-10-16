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

Los recibos compactos privados se conservan en `tmp/prod6-recovery-20261016/`.
La publicación y su readback se registran al verificar la recuperación.
