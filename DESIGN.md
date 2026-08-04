# BuenaPro Glass

## Estado

Este documento es el contrato visual vigente de BuenaPro. Las diez referencias aprobadas viven en `docs/new-style/generated-glass/` y forman, junto con este archivo, una sola fuente de verdad.

Una pantalla no esta terminada si conserva la apariencia plana anterior aunque use los colores nuevos.

## Direccion

BuenaPro combina dos capas:

1. **Capa ambiental Glass:** orientacion, resumen, contexto, navegacion, KPIs e inspectores.
2. **Capa de trabajo solida:** tablas, formularios, documentos, edicion y datos densos.

La elegancia procede de profundidad suave, proporcion, luz fria, controles precisos y consistencia. El producto no debe parecer una plantilla administrativa ni una landing promocional.

## Identidad

La marca de BuenaPro usa una orbita abierta como radar de oportunidades: trazo tinta, nucleo violeta y un unico punto verde que representa la señal detectada.

- `apps/web/public/brand/buenapro-logo.svg`: logotipo horizontal principal.
- `apps/web/public/brand/buenapro-logo-light.svg`: variante para fondos oscuros.
- `apps/web/public/brand/buenapro-mark.svg`: isotipo para rail compacto y superficies pequeñas.
- `apps/web/app/icon.svg`: favicon canónico de Next.js.
- No reconstruir la marca con texto o iniciales en cada vista; usar `BrandLogo`.
- No encerrar el isotipo en cuadrados de color ni añadir documentos, checks o edificios.
- El punto verde pertenece al símbolo de marca; no implica por sí mismo un estado de éxito.

## Referencias por ruta

| Ruta | Referencia |
| --- | --- |
| `/login` | `01-login.png` |
| `/registro` | `02-registro.png` |
| `/` | `03-inicio.png` |
| `/feed` | `04-oportunidades.png` |
| `/oportunidad/[id]` | `05-detalle-oportunidad.png` |
| `/mercado` | `06-mercado.png` |
| `/postulaciones` | `07-postulaciones.png` |
| `/postulaciones/[matchId]` | `08-workspace-postulacion.png` |
| `/perfil` | `09-perfil-empresa.png` |
| `/alertas` | `10-configuracion-alertas.png` |

## Paleta

```css
--glass-canvas: #f1f3ff;
--glass-canvas-deep: #e5eaff;
--glass-surface: rgba(255, 255, 255, 0.72);
--glass-surface-strong: rgba(255, 255, 255, 0.9);
--glass-work: #ffffff;
--glass-work-soft: #f8f9ff;
--glass-border: rgba(255, 255, 255, 0.72);
--glass-border-solid: #dfe3f0;
--glass-ink: #0c1533;
--glass-text: #26304d;
--glass-muted: #69718a;
--glass-violet: #4c3cff;
--glass-violet-hover: #3d2de7;
--glass-violet-soft: #eeecff;
--glass-black: #050609;
--glass-green: #249a68;
--glass-green-soft: #e8f8f0;
--glass-amber: #c77800;
--glass-amber-soft: #fff4df;
--glass-red: #d83a48;
--glass-red-soft: #ffebee;
```

- Violeta indica seleccion, navegacion activa, foco e inteligencia.
- Negro corresponde a la accion primaria de una superficie.
- Verde, ambar y rojo comunican estados, nunca identidad.
- El fondo ambiental puede usar una transicion tonal muy suave. No agregar orbes decorativos independientes.

## Materiales

### Ambient glass

```css
background: rgba(255, 255, 255, 0.62);
border: 1px solid rgba(255, 255, 255, 0.78);
backdrop-filter: blur(24px) saturate(125%);
box-shadow: 0 18px 55px rgba(61, 72, 140, 0.12);
```

Se usa para shell, KPI, resumentes, filtros principales, preview e inspectores. No se anidan dos paneles glass.

### Solid work surface

```css
background: #fff;
border: 1px solid #dfe3f0;
box-shadow: 0 8px 28px rgba(46, 55, 105, 0.07);
```

Se usa para tablas, formularios, documentos, requisitos y edicion. El texto siempre se renderiza sobre una superficie suficientemente opaca.

## Forma

- Shell y contenedores principales: `24px`.
- Paneles glass y work surfaces: `20px`.
- Paneles compactos: `16px`.
- Inputs, selects y botones: `14px`.
- Chips y filtros: `999px` cuando son capsulas.
- Icon buttons y avatares: circulares.
- Las tablas redondean solo su contenedor exterior; las filas permanecen estructuradas.

## Tipografia

Familia: `Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`.

- Page title: `32/40`, `750`.
- Section title: `20/28`, `700`.
- Panel title: `16/24`, `700`.
- Body: `14/22`, `400`.
- Body strong: `14/22`, `650`.
- Label: `12/17`, `650`.
- Caption: `12/18`, `450`.
- KPI: `34/40`, `750`, numeros tabulares.
- `letter-spacing: 0` en todo el producto.

## Profundidad

- Superficie base: sin sombra o sombra minima.
- Glass: `0 18px 55px rgb(61 72 140 / 0.12)`.
- Work surface: `0 8px 28px rgb(46 55 105 / 0.07)`.
- Popover: `0 18px 45px rgb(25 31 68 / 0.18)`.
- Dialog: `0 28px 80px rgb(25 31 68 / 0.24)`.
- Hover de fila: fondo violeta de `3-5%`, sin movimiento vertical.

## Shell

- Sidebar glass claro de `228-244px` en desktop.
- Marca arriba; cuenta y accesos secundarios abajo.
- Item activo con fondo violeta suave, icono y texto violeta, radio `14px`.
- Topbar se integra con el lienzo; no repite el titulo principal.
- Mobile usa bottom navigation glass con los tres destinos principales.

## Componentes

### Button

- Primary: negro, texto blanco, radio `14px` o capsula cuando es CTA destacado.
- Accent: violeta, reservado para aplicar o confirmar una seleccion contextual.
- Secondary: glass fuerte, borde frio.
- Ghost: transparente.
- Danger: rojo semantico.
- Altura normal `44px`; compacta `36px`.

### Inputs y selects

- Altura `44px`, radio `14px`, superficie blanca `80-92%`.
- Label siempre visible.
- Icono lineal opcional a la izquierda.
- Focus violeta con halo suave de `3px`.
- Placeholder con contraste legible.

### Tabs y filtros

- Las vistas hermanas pueden usar tabs glass o segmented control.
- Los filtros rapidos usan capsulas compactas.
- Activo: violeta suave, borde violeta translúcido y texto violeta.
- Los filtros avanzados viven en drawer, popover o expansion.

### Tables

- Contenedor `20px`, superficie solida.
- Header frio muy suave, `12px` y peso `650`.
- Filas `64-76px` segun densidad.
- Seleccion con superficie violeta suave y borde interior.
- Moneda alineada a la derecha y numeros tabulares.
- En mobile se transforma a filas estructuradas; no se comprime hasta ser ilegible.

### Status pills

- Pastel, compactos y acompañados por texto.
- Adjudicado/abierto: verde.
- Revision/espera: ambar.
- Desierto/error: rojo.
- Informacion/afinidad: violeta.

### GlassSurface

- Solo para contexto, resumen o inspeccion.
- Puede contener un work surface, pero un work surface no contiene otro panel decorativo.
- Blur desactivado bajo `prefers-reduced-transparency` cuando exista soporte o mediante fallback opaco.

### Iconografia

- Usar `lucide-react` con stroke `1.75-2`.
- No dibujar SVG manual si existe equivalente.
- Iconos desconocidos requieren tooltip.

## Graficos

- Violeta para serie principal o seleccion.
- Verde para adjudicados/resultado positivo.
- Rojo para desiertos.
- Glass solo en el contenedor; el plot mantiene fondo legible.
- Sin 3D ni efectos decorativos que impidan leer valores.

## Responsive

- Desktop amplio: `>= 1280px`.
- Laptop: `768-1279px`.
- Mobile: `< 768px`.
- Los paneles laterales pasan debajo o a drawer.
- Los CTA importantes ocupan ancho completo en mobile.
- Touch target minimo `44px`.
- Ningun elemento debe provocar overflow horizontal.

## Criterio de terminado

Para cerrar una vista:

1. Usar componentes visibles en `/design-system`.
2. Conectar datos reales y estados funcionales.
3. Capturar desktop, laptop y mobile cuando aplique.
4. Comparar lado a lado con el PNG asignado.
5. Revisar shell, material, radios, iconos, densidad y jerarquia.
6. Corregir y repetir captura si aun parece la interfaz plana anterior.
7. Ejecutar arquitectura, TypeScript y build.
8. Actualizar `tasks/05-frontend.md` y QA.
