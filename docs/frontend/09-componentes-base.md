# Vista 09 - Componentes base

## Objetivo

Crear un sistema reusable para que todas las vistas se vean consistentes y no se copien estilos manualmente.

## Referencia

Usar [DESIGN.md](../../DESIGN.md) como unica fuente visual y [migration-map.md](../new-style/migration-map.md) para la adopcion progresiva.

## Componentes UI

```text
components/ui/Button/
components/ui/IconButton/
components/ui/Input/
components/ui/Select/
components/ui/Checkbox/
components/ui/Switch/
components/ui/Tabs/
components/ui/Badge/
components/ui/Table/
components/ui/Drawer/
components/ui/Dialog/
components/ui/Toast/
components/ui/Skeleton/
components/ui/EmptyState/
components/ui/Pagination/
components/ui/Tooltip/
components/ui/SegmentedControl/
```

Cada carpeta:

```text
Component.tsx
Component.module.css
index.ts
```

## Iconos

No usar iconos genericos sin criterio visual. Para MVP se puede usar una libreria consistente, pero envolverla en componentes propios:

```text
components/ui/AppIcon/
```

Reglas:

- Trazos consistentes.
- Tamano base 18px.
- No mezclar familias.
- No usar iconos decorativos sin accion.
- Tooltips en icon buttons no obvios.

## Tokens CSS

Crear:

```text
apps/web/styles/tokens.css
apps/web/styles/themes.css
```

Variables:

- color
- spacing
- radius
- shadow
- typography
- z-index

## Patrones obligatorios

- Tabla con headers compactos.
- Badge semaforo: verde, ambar, rojo, gris.
- Deadline/cierre con formato America/Lima.
- Empty state corto y accionable.
- Drawer para detalles secundarios.
- Dialog solo para confirmaciones o formularios cortos.

## Criterios de done

- Ninguna vista nueva define botones propios si ya existe `Button`.
- No hay estilos sueltos duplicados.
- CSS Modules por componente.
- Tokens importados globalmente.
