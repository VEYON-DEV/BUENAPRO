# Vista 11 - Copiloto de licitación

## Objetivo

Permitir que el proveedor converse sobre una licitación y prepare campos del borrador con evidencia, sin otorgar a la IA capacidad de postular ni escribir sin confirmación humana.

## Superficies

- `/oportunidad/[id]`: preguntas sobre TDR, requisitos, perfil y riesgos. Sin borrador, el agente no propone escrituras.
- `/postulaciones/[matchId]`: además puede preparar precios sustentados, RTM, vigencia y contacto como cambios pendientes.

## Memoria

PostgreSQL conserva sesiones, mensajes, ejecuciones, resumen compacto y conjuntos de cambios. Se envía al modelo el resumen histórico más los mensajes recientes de la sesión activa. `Nueva conversación` crea otra sesión sin mensajes ni resumen anteriores, inicia un thread Codex nuevo y usa un workspace aislado por `tenantId/chatSessionId`; conserva únicamente el contexto vigente de empresa, licitación y borrador.

## Contexto

- Contrato y cronograma.
- Extracción del TDR y requisitos normalizados.
- Perfil, líneas, experiencia, equipo y recursos del tenant.
- Borrador, ítems, RTM y adjuntos.
- DOCX oficiales y adjuntos: lectura acotada de texto y tablas con Mammoth; el original permanece intacto.

Los documentos y mensajes se consideran contenido no confiable. Sus instrucciones internas se ignoran para reducir prompt injection.

## Confirmación manual

1. Gemini responde y devuelve `proposedChanges` estructurado.
2. El backend valida IDs y campos permitidos.
3. Se crea un `agent_change_set` pendiente.
4. La UI muestra que el borrador todavía no fue modificado.
5. Solo `Aplicar al borrador` ejecuta la transacción.
6. La aplicación mantiene estado `draft`, registra usuario y evento.

No existe tool ni endpoint de envío oficial a SEACE.

## Endpoints

```text
GET/POST /api/contracts/:id/chat/sessions
GET      /api/chat/sessions/:sessionId
POST     /api/chat/sessions/:sessionId/messages
POST     /api/chat/change-sets/:changeSetId/confirm
POST     /api/chat/change-sets/:changeSetId/reject
```

## Prompt

El prompt base vive en `server/agent/licitationAgent.ts`. Obliga al agente a distinguir solicitud, evidencia empresarial, dato del usuario e inferencia; citar fuentes; no inventar capacidades/precios; y nunca afirmar que una propuesta fue enviada.
