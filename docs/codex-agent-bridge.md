# Puente Codex para el copiloto

## Objetivo

El chat de BuenaPro puede usar Codex CLI como motor agentico para conversar con una licitación, el perfil de empresa, la biblioteca de postulación y el borrador. Redis se usa como memoria caliente con TTL; PostgreSQL conserva la conversación final, runs, citas y change sets.

## Flujo

```text
Frontend Copilot
  POST /api/chat/sessions/:id/messages/stream
Backend
  1. guarda user message + agent_run en PostgreSQL
  2. compacta memoria conversacional si hace falta
  3. prepara contexto tenant-scoped de licitación/perfil/biblioteca/borrador
  4. busca sesión caliente en Redis
  5. ejecuta codex exec --json o codex exec resume --json
  6. streamea eventos SSE al frontend
  7. valida JSON final contra IDs/citas permitidas
  8. persiste assistant message, usage, change set y artefactos en PostgreSQL
  9. elimina del workspace los documentos finales ya persistidos
```

## Redis

Keys principales:

```text
codex:chat:<tenantId>:<chatSessionId>:state
codex:chat:<tenantId>:<chatSessionId>:lock
```

Estado guardado:

```json
{
  "status": "active",
  "tenantId": "...",
  "chatSessionId": "...",
  "codexThreadId": "...",
  "workspacePath": "...",
  "contextHash": "...",
  "lastActivityAt": "..."
}
```

`CODEX_AGENT_SESSION_TTL_SECONDS` controla el TTL. Si no existe `REDIS_URL`, desarrollo usa fallback en memoria del proceso; producción debe configurar Redis.

## Workspace Codex

Se usa un workspace aislado por conversación dentro de cada tenant:

```text
.codex-runtime/tenants/<tenantId>/chats/<chatSessionId>/
  AGENTS.md
  memory.md
  context/current.json
  context/source-documents/
  tmp/downloads/
  outputs/generated-documents/
```

Cada conversación tiene su propio contexto, memoria caliente, temporales y entregables. Redis vincula el mismo `chatSessionId` al thread caliente de Codex, por lo que una conversación nueva no reanuda ni recibe el workspace de otra conversación.

Los documentos fuente se materializan en un cache acotado de la conversación bajo `context/source-documents/`. El bridge acepta solo HTTPS bajo `*.seace.gob.pe` y valida también cada redireccion; deduplica registros equivalentes, valida firma/tamano, limita cada archivo a 10 MB, cada expediente a 12 archivos/30 MB y elimina entradas inactivas despues de 30 minutos. `context/current.json` indica `localPath`, `localStatus`, `editableTemplate`, `documentRole` y `duplicateOf`; por eso Codex puede preservar una plantilla aunque su propio proceso tenga una falla DNS sin mezclar documentos de otro chat.

El `AGENTS.md` obliga a procesar OOXML/PDF con salida acotada: no se vuelca XML completo al thread, se agrupan inspecciones y se muestran solo campos/resumenes necesarios. Esto reduce contexto sin quitar al agente la capacidad de editar los originales.

## Documentos generados

Cuando el usuario pide una propuesta Word, PDF u hoja de calculo, Codex guarda el entregable final directamente en `outputs/generated-documents/`. El backend detecta solo archivos nuevos o modificados durante ese run, permite `.doc`, `.docx`, `.pdf`, `.xls` y `.xlsx`, limita cada archivo a 15 MB y valida que permanezca dentro del directorio autorizado.

La copia durable se guarda en `chat_artifacts`, asociada al tenant, sesion, mensaje y `agent_run`. El frontend recibe los artefactos con el mensaje SSE y muestra una accion `Descargar` que usa `GET /api/chat/artifacts/:artifactId`. La ruta vuelve a validar el tenant, fuerza descarga, desactiva cache HTTP y evita MIME sniffing.

Despues de confirmar el commit en PostgreSQL, el backend elimina los entregables detectados del workspace. Los originales de SEACE usados como insumo viven en `tmp/downloads/` y el bridge elimina los archivos nuevos o modificados al cerrar cada run, incluso cuando el agente falla. Por eso limpiar el cache o cerrar la sesion Codex no rompe descargas anteriores.

## Documentos SEACE

El contexto incluye `downloadUrl` publico para cada fila de `contract_documents`. Codex puede intentar consultar esos links cuando el usuario pide leer o verificar un PDF/DOCX original; si el acceso falla, debe decirlo y usar `summary_json`, `raw_extraction_json` y facets como fallback.

Validacion local 2026-08-08 con contrato `85409`:

- `curl -I -L` al TDR PDF de SEACE devolvio `200` y `Content-Length: 1209293`.
- `curl -I -L` al formato DOCX devolvio `200` y `Content-Length: 24528`.
- Una corrida directa de Codex recibio el `downloadUrl`, pero reporto fallo de resolucion DNS al intentar leer el contenido. Por eso este modo queda como experimental.
- Una segunda corrida directa con protocolo explicito (`curl -L --fail --max-time 30`, `file`, `pdftotext`) descargo el PDF en `outputs/generated-documents/diag-337160.pdf`, valido `PDF document, version 1.7, 10 pages` y extrajo texto de la primera pagina.
- Corridas posteriores mostraron intermitencia DNS dentro de `codex exec`: una descarga completa fallo con `curl: (6) Could not resolve host: prod6.seace.gob.pe`, pero pruebas HEAD inmediatas con y sin `--output-schema` devolvieron `200`. Diagnostico: el acceso publico funciona, pero debe usarse con protocolo explicito y reintentos; "consultar URL" no basta.
- Tres pruebas directas con `gpt-5.6-sol` y `--sandbox workspace-write` sobre PDFs reales (`85621`, `85601`, `85600`) ejecutaron el flujo correcto, pero fallaron por DNS restringido dentro del sandbox.
- La misma prueba con `gpt-5.6-sol` y `--sandbox danger-full-access` descargo el PDF `document-230692`, valido `PDF document, version 1.7, 6 pages` y extrajo la primera pagina con `pdftotext`.
- Prueba final por endpoint SSE en `POST /api/chat/sessions/:id/messages/stream` usando `CODEX_AGENT_MODEL=gpt-5.6-sol` y `CODEX_AGENT_SANDBOX_MODE=danger-full-access`: Codex descargo el PDF original, valido 271,461 bytes, extrajo 3,017 palabras y persistio la respuesta con `model=gpt-5.6-sol`.

Para produccion robusta, el siguiente paso recomendado es materializar documentos relevantes en `context/documents/` con limite de tamano, timeout, validacion MIME y limpieza por TTL. El link publico sigue siendo la fuente original; el archivo local seria cache efimero para la sesion Codex.

## Seguridad

- Codex inicia con `--sandbox workspace-write` en sesiones nuevas.
- Para permitir descarga agentica directa de SEACE desde Codex CLI, usar `CODEX_AGENT_SANDBOX_MODE=danger-full-access` solo dentro de una VM/container aislado. `workspace-write` mantiene red restringida y puede fallar con DNS.
- Los cambios se guardan como propuestas pendientes; el usuario confirma antes de aplicar.
- El backend valida citas e IDs contra el contexto permitido.
- No se pasan secretos ni credenciales SEACE.
- Los documentos generados deben entrar por `outputs/generated-documents/` antes de persistirse como artefactos.

## Flags

```text
CODEX_AGENT_ENABLED=0      usa el motor anterior como fallback
CODEX_AGENT_MODEL=...      modelo para Codex CLI
CODEX_AGENT_SANDBOX_MODE=workspace-write|danger-full-access
REDIS_URL=redis://...      habilita sesión caliente real
CODEX_AGENT_WORKSPACE_ROOT=/path
CODEX_AGENT_SESSION_TTL_SECONDS=1800
```

## Instalacion y autenticacion en Docker

La imagen web incluye una version fijada de Codex CLI. La autenticacion y el
workspace se conservan en los volumenes `codex_auth` y `codex_runtime`, por lo
que sobreviven a reconstrucciones y reinicios del contenedor.

Desde la raiz del proyecto en la VM:

```bash
docker compose -f infra/docker/docker-compose.yml run --rm --no-deps web \
  codex login --device-auth
```

El comando muestra una URL y un codigo de un solo uso. La autenticacion debe
completarse en el navegador del titular de la cuenta; no se copian tokens al
repositorio ni al archivo de entorno. Despues se valida con:

```bash
docker compose -f infra/docker/docker-compose.yml run --rm --no-deps web \
  codex login status
```

Mantener `CODEX_AGENT_ENABLED=0` hasta completar esta validacion. Luego se
puede habilitar y recrear solo `web`.
