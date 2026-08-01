# Frontend BuenaPro - Guia de desarrollo

Este directorio define como construir la interfaz de BuenaPro. Complementa:

- [Design Patterns](../design-patterns.md)
- [Arquitectura](../architecture.md)
- [Backend/Web tasks](../../tasks/03-backend-web.md)
- [Frontend tasks](../../tasks/05-frontend.md)

## Principio de producto

BuenaPro debe ayudar al usuario a decidir rapido:

1. Que oportunidades existen.
2. Cuales encajan con su empresa.
3. Que le falta para postular.
4. En que estado esta cada postulacion.

La UI no debe parecer una landing ni un dashboard lleno de metricas decorativas. Es una herramienta diaria de trabajo.

## Estructura recomendada

Las rutas de `apps/web/app` deben ser delgadas. La logica visual y de datos vive por dominio en `features`.

```text
apps/web/
├── app/
│   ├── page.tsx
│   ├── feed/page.tsx
│   ├── oportunidad/[id]/page.tsx
│   ├── perfil/page.tsx
│   ├── postulaciones/page.tsx
│   ├── alertas/page.tsx
│   ├── mercado/page.tsx
│   ├── configuracion/page.tsx
│   ├── admin/page.tsx
│   └── login/page.tsx
├── components/
│   ├── layout/
│   └── ui/
├── features/
│   ├── shell/
│   ├── auth/
│   ├── dashboard/
│   ├── market-intelligence/
│   ├── opportunities/
│   ├── opportunity-detail/
│   ├── profile/
│   ├── tracking/
│   ├── alerts/
│   ├── settings/
│   └── admin/
├── lib/
│   ├── api/
│   ├── format/
│   └── constants/
└── styles/
    ├── tokens.css
    └── themes.css
```

## Regla de componentes

Cada componente no trivial debe vivir en su propia carpeta.

```text
features/opportunities/components/OpportunityTable/
├── OpportunityTable.tsx
├── OpportunityTable.module.css
└── index.ts
```

Usar CSS Modules por componente. `globals.css` queda para reset, variables globales y estilos base. No usar clases globales para componentes nuevos salvo tokens o utilidades muy pequenas.

## Convenciones

- Componentes visuales: `PascalCase`.
- Hooks: `useNombre`.
- Servicios cliente: `features/<dominio>/api.ts`.
- Tipos: `features/<dominio>/types.ts`.
- Estados y constantes compartidas: `lib/constants`.
- Formateo de dinero, fechas y countdown: `lib/format`.
- No llamar `fetch` directamente desde componentes profundos; centralizarlo por dominio.

## Backend disponible

APIs que el frontend debe consumir:

```text
GET    /api/contracts
GET    /api/contracts/:id
POST   /api/contracts/:id/track
GET    /api/contracts/:id/documents
GET    /api/contracts/:id/facets
GET    /api/contracts/:id/original/:docId

GET    /api/feed
GET    /api/dashboard
GET    /api/market
GET    /api/profile
PUT    /api/profile
GET    /api/profiles
POST   /api/profiles
PATCH  /api/profiles/:id

GET    /api/lines
POST   /api/lines
PATCH  /api/lines/:id
DELETE /api/lines/:id

GET    /api/tracking
PATCH  /api/matches/:id
GET    /api/matches/:id/events
POST   /api/matches/:id/events
GET    /api/matches/:id/tasks
POST   /api/matches/:id/tasks
PATCH  /api/matches/:id/tasks/:taskId
DELETE /api/matches/:id/tasks/:taskId

GET    /api/notifications
GET    /api/notifications/prefs
PUT    /api/notifications/prefs

GET    /api/catalogs/objects
GET    /api/catalogs/states
GET    /api/catalogs/cubso/segments
GET    /api/catalogs/enabled-cubso-segments
GET    /api/catalogs/entities
GET    /api/catalogs/ubigeo
```

## Estados obligatorios por vista

Cada vista debe contemplar:

- loading
- empty
- error recuperable
- datos parciales
- accion exitosa
- permisos o falta de perfil cuando aplique

## Navegacion principal

```text
Oportunidades
Mercado
Postulaciones
```

Perfil y Alertas viven en el menu de cuenta. Admin y Swagger no forman parte del flujo del usuario.

## Specs por vista

- [00 App Shell](./00-app-shell.md)
- [01 Auth](./01-auth.md)
- [02 Inicio](./02-inicio.md)
- [03 Oportunidades](./03-oportunidades.md)
- [04 Detalle de oportunidad](./04-detalle-oportunidad.md)
- [05 Perfil de empresa](./05-perfil-empresa.md)
- [06 Postulaciones](./06-seguimiento.md)
- [07 Alertas](./07-configuracion.md)
- [08 Admin tecnico](./08-admin-tecnico.md)
- [09 Componentes base](./09-componentes-base.md)
- [10 Workspace de postulación](./10-postulacion.md)
- [11 Copiloto de licitación](./11-copiloto-licitacion.md)
- [12 Inteligencia de mercado](./12-mercado.md)
