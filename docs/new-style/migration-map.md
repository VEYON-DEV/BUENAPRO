# Mapa de adopcion BuenaPro Glass

## Objetivo

Mantener todas las rutas de BuenaPro dentro del mismo sistema visual sin alterar contratos de API ni reglas de negocio. La referencia normativa es `DESIGN.md`; las diez imágenes de `generated-glass/` definen la composición esperada por vista.

## Fundamentos compartidos

- `apps/web/styles/tokens.css`: color, radio, profundidad, blur y movimiento.
- `apps/web/components/ui/`: primitives sin conocimiento de negocio.
- `/design-system`: catálogo interno para revisar materiales, controles, estados, métricas y tablas.
- `apps/web/features/shell/`: navegación y contexto global.
- `apps/web/features/<dominio>/`: composición y componentes propios de cada recorrido.

No crear componentes `V2`, hojas CSS globales para una vista ni una segunda aplicación. Si un patrón es transversal, se valida primero en `/design-system` y se promueve a `components/ui`.

## Referencias por ruta

| Ruta | Referencia | Contrato principal |
| --- | --- | --- |
| `/login` | `01-login.png` | Fotografía institucional, formulario blanco, CTA negro |
| `/registro` | `02-registro.png` | Misma composición de acceso y transición clara a onboarding |
| `/` | `03-inicio.png` | Radar ejecutivo, métricas accionables, mercado y cierres |
| `/feed` | `04-oportunidades.png` | Filtros de vidrio y tabla de trabajo densa |
| `/oportunidad/[id]` | `05-detalle-oportunidad.png` | Decisión primero, TDR y comparables históricos |
| `/mercado` | `06-mercado.png` | Filtros, KPIs, tendencia, regiones y competidores |
| `/postulaciones` | `07-postulaciones.png` | Cartera compacta y siguiente acción |
| `/postulaciones/[matchId]` | `08-workspace-postulacion.png` | Oferta, RTM, archivos y rail de progreso |
| `/perfil` | `09-perfil-empresa.png` | Empresa, líneas, keywords y conexión SEACE |
| `/alertas` | `10-configuracion-alertas.png` | Preferencias simples y actividad reciente |

## Definicion de terminado

- Usa tokens y componentes compartidos.
- El vidrio se limita a navegación, contexto, filtros y lectura ejecutiva.
- Tablas, formularios, documentos y editores usan superficies sólidas.
- CTA principal negro; selección y navegación violeta.
- Iconos Lucide, sin SVG manual nuevo.
- Datos reales largos no desbordan ni deforman controles.
- Foco, contraste, loading, empty y error están contemplados.
- Se compara lado a lado con su referencia en desktop, laptop y mobile.
- Arquitectura, TypeScript y build pasan.
- La captura y el checkbox quedan registrados en `tasks/05-frontend.md`.

## Fuera de alcance visual

- Cambiar reglas de negocio o endpoints.
- Activar envío oficial a SEACE sin endpoint confirmado.
- Rediseñar workers o persistencia histórica.
- Habilitar dark mode antes de diseñarlo como sistema completo.
