# Vista 00 - App Shell

## Objetivo

Definir la estructura permanente de BuenaPro: header, navegacion lateral, contexto del usuario y area principal. Todas las vistas privadas deben sentirse parte del mismo producto.

## Usuario

El usuario entra varias veces al dia para revisar oportunidades y dar seguimiento. Necesita ubicarse rapido, sin menus grandes ni ruido.

## Layout

```text
Header superior fijo
Sidebar icon rail
Main content con ancho fluido
Drawer lateral opcional
```

## Elementos

- Wordmark BuenaPro.
- Selector/contexto: `Contratos menores`.
- Busqueda global opcional.
- Accesos principales: Oportunidades, Mercado y Postulaciones.
- Perfil de empresa y Alertas dentro del menu del avatar.
- Admin y documentacion tecnica sin entrada en la navegacion del producto.
- Campana enlazada al centro de alertas relevantes.

## Componentes

```text
features/shell/components/AppShell/
features/shell/components/TopBar/
features/shell/components/IconRail/
features/shell/components/NavIconButton/
features/shell/components/UserMenu/
features/shell/components/NotificationBell/
```

Cada carpeta:

```text
ComponentName.tsx
ComponentName.module.css
index.ts
```

## Comportamiento

- Sidebar activa la ruta actual.
- En mobile, el rail se convierte en una barra compacta de tres iconos.
- Header no debe tapar contenido.
- El main debe tener padding consistente: `24px` desktop, `16px` mobile.
- No usar cards alrededor de toda la pagina.

## Datos backend

- Puede usar sesion NextAuth.
- `GET /api/notifications?status=queued` para contador si se decide mostrar alertas.
- `GET /api/tenant` para nombre del workspace.

## Estados

- Sin sesion: redirigir a `/login`.
- Tenant ausente: mostrar estado de onboarding.
- Error de tenant: mensaje corto con accion de reintentar.

## Criterios de done

- Todas las vistas privadas usan el mismo shell.
- Navegacion activa funciona.
- Mobile no rompe contenido.
- Cumple [Design Patterns](../design-patterns.md): sin sidebar textual pesado ni decoracion innecesaria.
