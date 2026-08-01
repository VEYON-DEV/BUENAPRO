# Vista 06 - Postulaciones

## Objetivo

Gestionar el embudo de oportunidades desde interes hasta cobro. Para MVP, debe cubrir evaluacion, preparacion y postulacion.

## Usuario

Proveedor o equipo comercial que quiere saber que oportunidades estan pendientes y que falta para enviar cotizacion.

## Ruta

```text
/postulaciones
```

## Estados del embudo

```text
inbox
en_evaluacion
interesada
en_preparacion
postulada
ganada
perdida
desierta
en_ejecucion
cobrada
descartada
```

## Layout

Dos vistas:

- Kanban para seguimiento rapido.
- Tabla para operacion densa.

El MVP usa una tabla de cartera como vista principal. La edición detallada vive en `/postulaciones/[matchId]`; no se incrustan notas, tareas e historial dentro de cada fila.

## Componentes

```text
features/tracking/components/TrackingToolbar/
features/tracking/components/TrackingBoard/
features/tracking/components/TrackingColumn/
features/tracking/components/TrackingCard/
features/tracking/components/TrackingTable/
features/tracking/components/TrackingDetailDrawer/
features/tracking/components/TaskChecklist/
features/tracking/components/OfferAmountForm/
features/tracking/components/ResponsiblePicker/
features/tracking/components/MatchEventTimeline/
```

## Backend

```text
GET /api/tracking?state=
PATCH /api/matches/:id
GET /api/matches/:id/tasks?ensure_defaults=true
POST /api/matches/:id/tasks
PATCH /api/matches/:id/tasks/:taskId
DELETE /api/matches/:id/tasks/:taskId
GET /api/matches/:id/events
POST /api/matches/:id/events
GET /api/tenant/members
```

## Acciones

- Cambiar estado.
- Asignar responsable.
- Registrar monto ofertado.
- Agregar nota.
- Crear tarea manual.
- Marcar tarea como hecha.
- Abrir detalle de oportunidad.
- Continuar una postulación en su workspace dedicado.

## Estados

- Sin matches: sugerir ir a Oportunidades.
- Match sin checklist: cargar defaults con `ensure_defaults=true`.
- Error al mover estado: revertir UI optimista.
- Muchos items: paginacion o virtualizacion si crece.

## Criterios de done

- Se puede mover una oportunidad por el embudo.
- Se ve avance de checklist.
- Se puede registrar monto ofertado.
- Se puede abrir detalle desde Postulaciones.
