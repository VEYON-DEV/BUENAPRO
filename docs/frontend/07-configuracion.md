# Vista 07 - Alertas

## Objetivo

Mostrar oportunidades notificadas y controlar cuan exigente debe ser el radar.

## Usuario

Proveedor que solo quiere ser interrumpido por oportunidades con afinidad suficiente.

## Ruta

```text
/alertas
```

## Secciones

1. Activar o desactivar alertas.
2. Afinidad minima: alta o alta y posible.
3. Limite diario.
4. Historial reciente con acceso a la oportunidad.

## Componentes

```text
features/alerts/AlertsPage.tsx
features/alerts/components/AlertPreferencesForm/
```

## Backend

```text
GET /api/notifications/prefs
PUT /api/notifications/prefs
GET /api/notifications
```

## UX

- Mantener Perfil y radar separados de las preferencias de aviso.
- No mostrar workspace, miembros, canales tecnicos ni operacion interna.
- Guardado unico y estado de confirmacion accesible.

## Estados

- Sin preferencias: crear defaults.
- Sin alertas: estado vacio compacto.
- Error de guardado: explicar el siguiente paso.

## Criterios de done

- Editar afinidad minima y limite diario.
- Abrir una oportunidad desde su alerta.
- Mantener Admin, Docs y miembros fuera de la interfaz normal.
