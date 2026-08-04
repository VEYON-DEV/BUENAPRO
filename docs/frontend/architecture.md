# Arquitectura frontend por dominios

## Objetivo

BuenaPro organiza el frontend mediante **vertical slices**. Cada dominio contiene todo lo necesario para sus vistas: composicion, componentes privados, acceso cliente a API, modelos, hooks y utilidades. La raiz de la aplicacion conserva solamente infraestructura realmente compartida.

Esta estructura evita carpetas globales sin propietario, estilos copiados entre pantallas e imports que conectan una vista con detalles internos de otra.

## Arbol canonico

```text
apps/web/
├── app/                              # Rutas Next.js y route handlers
│   ├── feed/page.tsx                 # Sesion, params y render del feature
│   ├── mercado/page.tsx
│   ├── oportunidad/[id]/page.tsx
│   └── api/.../route.ts
├── components/
│   └── ui/                           # Primitives globales, sin negocio
│       ├── Button/
│       │   ├── Button.tsx
│       │   ├── Button.module.css
│       │   └── index.ts
│       └── Table/
├── features/
│   ├── opportunities/
│   │   ├── pages/
│   │   │   └── OpportunitiesPage/
│   │   │       ├── OpportunitiesPage.tsx
│   │   │       ├── OpportunitiesPage.module.css
│   │   │       └── index.ts
│   │   ├── components/
│   │   │   ├── OpportunityList/
│   │   │   └── OpportunityToolbar/
│   │   ├── api/                      # Requests de componentes cliente
│   │   ├── hooks/                    # Estado y comportamiento React
│   │   ├── model/                    # Types, schemas, mappers, constants
│   │   ├── utils/                    # Funciones puras del dominio
│   │   └── index.ts                  # API publica del dominio
│   ├── opportunity-detail/
│   ├── dashboard/
│   ├── market-intelligence/
│   ├── tracking/
│   ├── application-workspace/
│   ├── profile/
│   ├── alerts/
│   ├── design-system/                # Catalogo interno, sin navegacion publica
│   ├── auth/
│   ├── onboarding/
│   ├── shell/
│   └── admin/
├── lib/                              # Codigo generico sin vistas
│   ├── api/
│   ├── constants/
│   └── format/
├── server/                           # DB, auth y servicios solo servidor
└── styles/
    ├── tokens.css                    # Implementacion de DESIGN.md
    └── themes.css                    # Temas completos aprobados
```

Las carpetas `api`, `hooks`, `model` y `utils` se crean solamente cuando el dominio las necesita. No se agregan vacias.

## Responsabilidad de cada nivel

### `app/`

- Declara rutas, layouts y handlers de Next.js.
- Resuelve sesion, tenant y parametros de URL.
- Renderiza una pagina importada desde la API publica del dominio.
- No contiene componentes visuales, queries extensas ni CSS de una feature.

```tsx
import { OpportunitiesPage } from "@/features/opportunities";
```

### `features/<domain>/pages/`

- Compone una ruta completa y orquesta componentes privados del dominio.
- Puede consumir servicios de servidor en server components.
- Cada vista tiene carpeta propia con `Page.tsx`, `Page.module.css` e `index.ts`.
- La pagina se exporta desde `features/<domain>/index.ts`.

### `features/<domain>/components/`

- Contiene componentes que hablan el idioma del dominio, como `OpportunityToolbar`, `HistoricalComparables` o `QuoteEditor`.
- Cada componente no trivial vive en carpeta propia.
- El estilo permanece junto al componente en `Component.module.css`.
- Un componente privado no se importa directamente desde otro dominio.

### `features/<domain>/api/`

- Contiene requests ejecutados desde el navegador.
- Usa el cliente compartido de `lib/api` y normaliza respuestas.
- No contiene queries SQL ni secretos.

### `features/<domain>/model/`

- Contiene tipos, schemas, mappers y constantes del dominio.
- No importa React ni componentes.
- Los tipos publicos se reexportan desde `features/<domain>/index.ts`.

### `components/ui/`

- Contiene primitives sin reglas de negocio: Button, Input, Tabs, Table y Dialog.
- Implementa los tokens de `DESIGN.md`.
- No importa `features`, `server` ni endpoints.
- Un primitive no conoce contratos, SEACE, proveedores o postulaciones.

### `lib/`

- Contiene funciones genericas: moneda, fecha, cliente HTTP y constantes transversales.
- No contiene componentes ni importa dominios.
- Una utilidad exclusiva permanece dentro de su feature.

### `styles/`

- `tokens.css` traduce `DESIGN.md` a variables CSS.
- `themes.css` contiene un tema completo solo después de ser diseñado y validado.
- No existen `common.css`, `helpers.css` o estilos globales de componentes.
- `globals.css` se limita a reset, elementos base, tokens y foco global.

## Regla de dependencias

```text
app
 └── feature public API
      ├── own pages/components/api/model/hooks/utils
      ├── components/ui
      ├── lib
      ├── server (solo server components)
      └── another feature public API (excepcion explicita)

components/ui -> lib permitido
components/ui -> features prohibido
lib           -> features prohibido
feature A     -> feature B internals prohibido
```

```tsx
// Correcto
import { CopilotPanel } from "@/features/copilot";

// Incorrecto
import { CopilotPanel } from "@/features/copilot/components/CopilotPanel";
```

Cuando dos dominios necesitan la misma pieza:

1. Si es visual y no conoce negocio, promoverla a `components/ui`.
2. Si representa negocio con propietario claro, exportarla desde ese dominio.
3. Si comparte una funcion pura, moverla a `lib` solo si es transversal.
4. No copiar CSS ni crear una carpeta global `shared` por conveniencia.

## Dominios de BuenaPro

| Dominio | Rutas o responsabilidad |
| --- | --- |
| `shell` | Navegacion, top bar, cuenta y alertas globales |
| `dashboard` | `/`, radar ejecutivo y prioridades del dia |
| `auth` | `/login`, `/registro` |
| `onboarding` | `/onboarding` |
| `opportunities` | `/feed` |
| `opportunity-detail` | `/oportunidad/[id]` |
| `market-intelligence` | `/mercado`, detalle de proveedor |
| `tracking` | `/postulaciones` y actividad de seguimiento |
| `application-workspace` | `/postulaciones/[matchId]` |
| `copilot` | Conversacion y cambios en detalle/postulacion |
| `profile` | `/perfil`, empresa y radar |
| `alerts` | `/alertas` y preferencias simples |
| `design-system` | `/design-system`, inventario interno de primitives y composiciones aprobadas |
| `admin` | Superficie interna sin navegacion de cliente |

El dashboard resume decisiones y prioridades; la exploracion historica, sus filtros y analisis detallados pertenecen a `market-intelligence`.

## Estilos consistentes

La consistencia no se consigue con una hoja CSS gigante. Se consigue en tres niveles:

1. **Tokens:** color, tipografia, espacios, radios, profundidad y z-index en `styles/tokens.css`.
2. **Primitives:** comportamiento y anatomia comunes en `components/ui`.
3. **Composiciones de dominio:** layout específico junto a la vista o componente propietario.

Reglas:

- No usar hex, sombras o radios locales si ya existe un token.
- No recrear Button, Input, Badge, Table, Tooltip o Dialog dentro de una feature.
- No exportar class names CSS entre modulos.
- No usar selectores globales para corregir un componente.
- Si un patrón aparece tres veces con la misma semántica, evaluar un primitive o composición pública.
- Si dos elementos solo se parecen, mantenerlos separados; la coincidencia visual no basta para abstraer.

## API publica del dominio

`features/<domain>/index.ts` exporta lo minimo:

```ts
export { OpportunitiesPage } from "./pages/OpportunitiesPage";
export type { OpportunitySummary } from "./model/types";
```

No usar barrels profundos para exportar todo. Componentes, hooks y funciones permanecen privados por defecto.

## Validacion automatica

```bash
npm run check:frontend-architecture
```

El chequeo valida dominios, carpetas de pagina, APIs publicas, imports cruzados y primitives compartidos.

## Checklist para una nueva vista

1. Elegir o crear el dominio propietario.
2. Crear `pages/ViewName/` con TSX, CSS Module e index.
3. Crear componentes privados por responsabilidad.
4. Colocar tipos y normalizadores en `model/`.
5. Colocar requests cliente en `api/`.
6. Exportar solo la pagina desde el `index.ts` del dominio.
7. Mantener `app/<route>/page.tsx` delgado.
8. Ejecutar arquitectura, build y QA visual.
