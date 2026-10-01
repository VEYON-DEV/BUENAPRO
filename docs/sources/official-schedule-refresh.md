# Corrida de cronogramas oficiales — PROD6 y PROD4

Esta corrida actualiza exclusivamente las etapas publicadas y ventanas de
participación de registros **ya guardados**. No descubre oportunidades, no
descarga bases/TDR, no invoca Gemini, no recalcula afinidad y no genera alertas.

## Antes de ejecutar

Aplicar `db/migrations/0032_official_schedule_refresh.sql` y disponer de backup.
El comando usa `DATABASE_URL` del worker; verificar su destino sin imprimir
credenciales. Primero ejecutar sin `--apply` y revisar conteos/fallos.

```sh
buenapro-worker schedule-refresh --source both --limit 500
buenapro-worker schedule-refresh --source both --limit 500 --apply
```

`--limit` es por fuente (1 a 5000). Para probar una ficha guardada:

```sh
buenapro-worker schedule-refresh --source prod4 --ids 1246258 --limit 1
```

Los IDs sólo se usan como filtro, nunca insertan nuevos registros ni saltan el
filtro de rubro. `--include-inactive` permite actualizar registros inactivos
ya guardados; por defecto PROD6 usa `estado_codigo=2` y PROD4 `missing_since IS
NULL`. Esos filtros **no garantizan que aún se pueda ofertar**: revisar fecha y
cronograma oficial. La selección ordena por cronograma menos recientemente
refrescado para avanzar por lotes sin procesar siempre los mismos IDs.

## Alcance y endpoints

- PROD4: `technology_relevant=true`, bienes y servicios. Reutiliza
  `/fichaProceso/idProceso/{id}`; guarda todas las etapas `listaCronograma`, no
  sólo las primeras seis. El filtro tecnológico del adaptador ya excluye otros
  rubros antes de guardar la oportunidad.
- PROD6: bienes/servicios guardados en los segmentos configurados en
  `PROD4_TECHNOLOGY_SEGMENTS` (43/81 por defecto). Reutiliza
  `/buscadorpublico/contrataciones/listar-completo?id_contrato={id}` y conserva
  `uitContratoEtapaProjectionList` completo. Segmento 81 no equivale por sí solo
  a TI: esta corrida **no reclasifica** los registros legados del segmento.

Cada ficha es transaccional. Si una fuente falla, falta su lista de etapas o la
lista llega vacía, se preserva el cronograma anterior y se reporta el ID fallido.
El comando devuelve código 1 cuando hay fallos y 0 sin fallos. No muestra texto
de documentos ni credenciales en el reporte.

## Persistencia y fechas

- Se fusiona únicamente la lista de etapas dentro del JSON de detalle; el resto
  de metadata, documentos, análisis y puntajes se conserva.
- PROD6 recalcula `hash_detail` sobre ese JSON fusionado. Si otra metadata de
  la fuente cambió, la siguiente ingesta completa detectará el hash distinto.
- PROD4 reemplaza filas de `prod4_schedule` atómicamente y sincroniza ventanas
  de registro y presentación de propuestas.
- Se actualiza `schedule_fetched_at`, no `detail_fetched_at`: refrescar etapas
  no implica haber actualizado toda la ficha.
- Sólo se materializa un `timestamptz` si la fuente publica una hora. Fecha sin
  hora permanece en el JSON para mostrar el día y “Hora no informada”, sin
  inventar medianoche ni 23:59. Una ventana ausente/no precisa borra su timestamp
  previo para no presentar un cierre falso.

## Refresco automático con el worker habitual

El scheduler del worker puede encolar una corrida exclusiva de cronogramas en
la cola `io`. Se habilita después de aplicar migración y verificar el smoke:

```dotenv
SCHEDULE_REFRESH_ENABLED=true
SCHEDULE_REFRESH_INTERVAL_MINUTES=30
SCHEDULE_REFRESH_LIMIT=500
```

La configuración Python está deshabilitada por defecto; Docker Compose lo
habilita para scheduler/worker-io con intervalo de 30 minutos y límite 500,
salvo override explícito. El límite es por fuente; si hay más registros
que el límite se recorren en ciclos sucesivos y no todos se refrescan en el mismo
ciclo. Se encola al arrancar el scheduler y cada 30 minutos por defecto, incluso
para fichas cuyo texto de listado no cambió. El job estable `refresh_schedules`
queda deduplicado mientras esté pendiente o ejecutándose, mediante el índice
`uq_jobs_dedup`: no se acumulan barridos paralelos por fuente. PROD4 se omite si
`PROD4_ENABLED=false`. El refresco automático no activa pipelines
de documentos ni LLM. La ingesta completa habitual también persiste fechas de
etapas sin horas ficticias.

Compatibilidad: `SCHEDULE_REFRESH_INTERVAL_HOURS` sigue aceptándose cuando no
se configuró minutos; si ambos existen, minutos tiene prioridad. En producción
se debe configurar explícitamente `SCHEDULE_REFRESH_INTERVAL_MINUTES=30`.
La corrida manual queda como herramienta de QA/reparación, no como requisito
para mantener cronogramas frescos. Si un barrido tarda más que el intervalo,
el siguiente espera a que termine: 30 minutos es frecuencia de programación,
no una garantía de frescura ante errores o saturación de la cola.

## Validación

```sh
.venv/bin/pytest workers/seace/tests/test_schedule_refresh.py workers/seace/tests/test_prod4_poll.py -q
```

Pruebas: dry-run sin writes, límite e IDs, conservación de metadata/hash,
etapas completas, calendarios inválidos, precisión horaria, continuidad ante
fallos y CLI read-only por defecto.

## Ejecución productiva verificada — 2026-10-01

- Respaldo previo de las tres tablas: `/home/ubuntu/backups/schedule-before-20261001.dump`, 1.3 MB, formato custom y listado `pg_restore` válido.
- Migración0032 aplicada y registrada; smoke GET + escritura de una ficha por fuente aprobado.
- Dry-run completo: PROD4 69 fichas/556 etapas, PROD6 206 fichas/412 etapas, cero fallos.
- Apply completo: mismos conteos, cero fallos; SQL posterior confirma 69 y 206 fechas de refresco. Redis PONG y HTTPS login200.
- Corrida ejecutada dentro de worker-io con código aislado en `/tmp/buenapro-schedule-src-20261001`; no se alteró checkout, imagen ni proceso habitual del worker. No es un despliegue permanente.
- Interfaz timeline validada localmente; despliegue web/worker y activación del refresco automático de 30 minutos pendientes. Los jobs habituales de producción continúan con su código anterior.
