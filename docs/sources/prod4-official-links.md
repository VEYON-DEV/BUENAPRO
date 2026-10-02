# Enlace para revisar concursos en SEACE

La ficha oficial de cada concurso usa:
`https://prod4.seace.gob.pe/openegocio/#/ficha/idProceso/{id_procedimiento}`.

El botón «Revisar en SEACE» consume `process.source_url`. Antes el worker
guardaba el mapa general `#/georeferenciacion/`, por lo que el botón abría el
portal pero no el concurso. Migración0034 corrige los enlaces existentes, el
upsert actualiza la URL por ID y la API genera el enlace canónico en detalles
y listados para no depender de una URL histórica incorrecta.

Los enlaces Alfresco de bases/PDF no cambian. No se requieren credenciales
SEACE para construir el enlace; el acceso efectivo depende del portal oficial.

Validación 2026-10-02: navegador normal carga la ficha1254603 y muestra
CP SER-SM-2-2026-UNSCH/C-1, entidad, cronograma y base administrativa. 175
tests worker, test backend de URLs por ID y TypeScript aprobados. No cambio
visual ni de autenticación y no llamadas Gemini.

Desplegado `3609491` en web/worker-io. Migración0034 corrigió94 filas;
PostgreSQL confirma94/94 URLs canónicas. Servicios activos sin reinicios y
login200. La vista existente mantiene el mismo botón y abre una pestaña nueva.
