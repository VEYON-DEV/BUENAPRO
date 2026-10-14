# 005 - Plan Anual de Contrataciones (PAC)

## Estado

```yaml
status: verified
owner: pendiente
country: PE
last_verified_at: 2026-09-22
verified_by: Codex + subagente Luna xhigh
adapter_key: seace_pac3
```

## Alcance

El PAC publica la intención de compra de las entidades: procesos programados,
mes previsto, objeto, procedimiento, financiamiento e ítems CUBSO. Sirve para
anticipar oportunidades, pero una fila del PAC todavía no equivale a una
convocatoria abierta ni garantiza que el proceso llegue a convocarse.

El portal vigente también ofrece contrataciones no programadas y fichas de
entidad con versiones del PAC. Para saber si ya se puede postular se debe
enlazar posteriormente con PROD4/PROD2; para adjudicaciones e histórico de
contratos se usa OCDS.

## Portal PAC3 vigente

| Rol | URL | Estado comprobado |
| --- | --- | --- |
| Página oficial OECE | `https://www.gob.pe/8190-consultar-el-plan-anual-de-contrataciones-pac-de-las-entidades-publicas` | enlaza PAC3 |
| PAC3 público | `https://prod2.seace.gob.pe/pac3-publico/` | fuente vigente principal |

El adaptador de BuenaPro cubrirá exclusivamente PAC3. El frontend antiguo de
PROD1 queda registrado como referencia histórica fuera de alcance y no se
utilizará ni para polling ni como fallback.

## Acceso y permisos

```yaml
access_mode: public
authentication: none
transport_state: jsf_session_and_viewstate
captcha: none_observed
index_allowed: pending_legal_review
store_metadata_allowed: pending_legal_review
store_documents_allowed: pending_legal_review
redistribute_allowed: pending_legal_review
ai_processing_allowed: pending_legal_review
legal_reviewed_at: pending
```

No se pidió login ni CAPTCHA. Sin embargo, PAC3 no es una API REST anónima: el
servidor exige mantener cookies de sesión y `javax.faces.ViewState`. No guardar
cookies en el repositorio ni tratarlas como identificadores estables.

## Capacidades

| Capacidad | Disponible | Mecanismo |
| --- | --- | --- |
| Planes por entidad | sí | buscador PAC |
| Procesos programados | sí | buscador PP |
| Programados convocados/no convocados | sí | filtro de tipo de consulta |
| Contrataciones no programadas | sí | buscador PNP |
| Ficha de entidad | sí | reporte con versiones e indicadores |
| Ítems CUBSO | sí | detalle `visualizarProcesoItem.xhtml` |
| Versiones PAC | sí | ficha de entidad |
| PDF/Excel | sí | acciones POST JSF, no URL estática |
| Convocatoria postulable | no por sí sola | reconciliar con PROD4/PROD2 |
| Adjudicación/contrato | no | usar OCDS |

## Rutas PAC3 verificadas

Base: `https://prod2.seace.gob.pe/pac3-publico/pages`

| Método | Ruta | Función |
| --- | --- | --- |
| GET/POST | `/buscadorPACpublico.xhtml` | planes por entidad/año/ubigeo |
| GET/POST | `/buscadorPPpublico.xhtml` | procesos programados |
| GET/POST | `/buscadorPNPpublico.xhtml` | contrataciones no programadas |
| GET/POST | `/fichaEntidad.xhtml` | entidad, versiones, montos e indicadores |
| GET/POST | `/visualizarProcesoItem.xhtml` | detalle e ítems del proceso seleccionado |

No se encontró una API JSON pública. Las acciones PrimeFaces envían POST a la
misma página con parámetros como:

```text
javax.faces.partial.ajax=true
javax.faces.source=<componente>
javax.faces.partial.execute=<form/componente>
javax.faces.partial.render=<componente>
javax.faces.ViewState=<estado actual>
```

La sesión usa cookies `JSESSIONID` y `X-Oracle-BMC-LBS-Route`. Cada respuesta
puede renovar el `ViewState`; el cliente debe conservar el nuevo valor.

## Contrato HTTP observado

### Inicio de sesión pública

```http
GET /pac3-publico/pages/buscadorPPpublico.xhtml HTTP/1.1
Host: prod2.seace.gob.pe
```

La respuesta inicial entrega:

```text
Set-Cookie: JSESSIONID=...
Set-Cookie: X-Oracle-BMC-LBS-Route=...
<input name="javax.faces.ViewState" value="...">
```

Las cookies y el `ViewState` son efímeros. Deben mantenerse únicamente en
memoria durante el job y renovarse cuando el servidor responda con una vista
nueva o expirada.

### Formularios y componentes

Snapshot verificado el 2026-09-22 en `buscadorPPpublico.xhtml`:

| Función | Formulario/componente observado | Valor |
| --- | --- | --- |
| Formulario principal | `formTable_monDlg` | formulario de búsqueda |
| Entidad seleccionada | `formTable_monDlg:it_institucion` | texto deshabilitado; identidad real queda en estado JSF |
| Año | `formTable_monDlg:cb_search_input` | `2017`–`2026` |
| Tipo de consulta | `formTable_monDlg:j_idt46_input` | `1`, `2`, `3` |
| Objeto | `formTable_monDlg:j_idt53_input` | `62`–`65` |
| Tipo de proceso | `formTable_monDlg:j_idt59_input` | catálogo SEACE |
| Moneda | `formTable_monDlg:j_idt66_input` | catálogo de moneda |
| Tipo de compra | `formTable_monDlg:j_idt72_input` | catálogo de compra |
| Departamento | `formTable_monDlg:cb_dpto_input` | catálogo geográfico |
| Provincia | `formTable_monDlg:cb_prov_input` | dependiente del departamento |
| Distrito | `formTable_monDlg:cb_dist_input` | dependiente de provincia |
| Descripción | `formTable_monDlg:j_idt89` | texto libre |
| Financiamiento | `formTable_monDlg:it_fuentefinanc_input` | catálogo de fondos |
| Acción buscar | `formTable_monDlg:j_idt101` | renderiza la tabla |
| Tabla | `formTable_monDlg:tbl` | 20 filas por página |

Los IDs descriptivos como `formTable_monDlg`, `cb_search_input`, `cb_dpto_input`
y `tbl` son mejores anclas. Los segmentos generados `j_idt46`, `j_idt101`,
etc. pueden cambiar en cualquier despliegue. El adaptador debe descubrirlos en
el HTML/JavaScript inicial y validarlos con contract tests, no asumirlos para
siempre.

### Selección de entidad

La entidad se elige en el formulario `frmBusqEntidad`. Campos observados:

| Campo | Componente/ancla |
| --- | --- |
| RUC | primer campo de criterios; ID `j_idt*` dinámico |
| Sigla | segundo campo; ID `j_idt*` dinámico |
| Nombre | `frmBusqEntidad:txt_noment` |
| Departamento | `frmBusqEntidad:cb_dpto_input` |
| Provincia | `frmBusqEntidad:cb_prov_input` |
| Distrito | `frmBusqEntidad:cb_dist_input` |
| Tabla de resultados | `frmBusqEntidad:tblInstituciones` |

La tabla de entidades pagina de cinco en cinco y permite seleccionar/doble clic.
Después de seleccionar, el servidor asocia internamente la entidad a la sesión.
Enviar únicamente `it_institucion=BANCO DE LA NACION` no reproduce esa selección.

### Búsqueda AJAX

La acción observada tenía esta forma conceptual:

```text
POST /pac3-publico/pages/buscadorPPpublico.xhtml
Content-Type: application/x-www-form-urlencoded

javax.faces.partial.ajax=true
javax.faces.source=<id actual del botón Buscar>
javax.faces.partial.execute=<formulario o componente>
javax.faces.partial.render=formTable_monDlg:tbl
formTable_monDlg=<formTable_monDlg>
formTable_monDlg:cb_search_input=2026
formTable_monDlg:<tipo_consulta_actual>=3
formTable_monDlg:<objeto_actual>=65
formTable_monDlg:<descripcion_actual>=SOFTWARE
javax.faces.ViewState=<valor vigente>
```

La respuesta es XML parcial de JSF, no JSON. El cliente debe:

1. aplicar los fragmentos `<update>` relevantes;
2. extraer las filas de `formTable_monDlg:tbl_data`;
3. reemplazar `javax.faces.ViewState` con el nuevo valor;
4. detectar mensajes `<error>` y `ViewExpiredException`.

### Paginación

La tabla se inicializó como:

```text
id: formTable_monDlg:tbl
rows: 20
page: 0
currentPageTemplate: ({currentPage} of {totalPages})
```

La paginación también es un POST AJAX PrimeFaces y debe reutilizar la sesión.
Los criterios de finalización son, en orden:

1. alcanzar `rowCount` cuando esté disponible;
2. llegar a la última página indicada por el paginador;
3. recibir una página sin filas nuevas como protección secundaria.

### Detalle de ítems

La acción `Ver Ítems` se genera por fila y navega dentro de la sesión a:

```text
/pac3-publico/pages/visualizarProcesoItem.xhtml
```

No es un endpoint direccionable mediante un ID en la URL. El proceso se guarda
en el estado de sesión al pulsar la acción de la fila. El detalle devuelve:

- referencia PAC, año y versión;
- objeto, descripción y tipo de selección;
- mes previsto, tipo de compra, dependencia y modalidad;
- financiamiento, inversión y lugar de ejecución;
- CUBSO, descripción, unidad, cantidad y moneda por ítem;
- proceso desierto precedente y proceso convocado, cuando existen.

## Endpoints operativos del adaptador

Aunque todos sean páginas JSF, en el código conviene modelarlos como operaciones
separadas:

| Operación interna | Ruta PAC3 | Salida normalizada |
| --- | --- | --- |
| `start_session` | `GET buscadorPPpublico.xhtml` | cookies, ViewState, catálogos |
| `search_entities` | `POST buscadorPPpublico.xhtml` (`frmBusqEntidad`) | RUC, nombre, ubigeo, token de selección en sesión |
| `select_entity` | `POST buscadorPPpublico.xhtml` | entidad activa y nuevo ViewState |
| `search_planned_processes` | `POST buscadorPPpublico.xhtml` | filas de planificación y paginador |
| `next_process_page` | `POST buscadorPPpublico.xhtml` | siguiente grupo de 20 filas |
| `get_process_items` | acción de fila → `visualizarProcesoItem.xhtml` | cabecera e ítems CUBSO |
| `get_entity_plan` | `GET/POST buscadorPACpublico.xhtml` | resumen PAC por entidad/año |
| `get_entity_versions` | `GET/POST fichaEntidad.xhtml` | versiones, aprobación e indicadores |
| `search_unplanned` | `GET/POST buscadorPNPpublico.xhtml` | contrataciones no programadas |

No exponer estos nombres internos como API pública de BuenaPro. El worker debe
transformarlos al modelo canónico y PostgreSQL debe seguir siendo la fuente de
verdad.

## Filtros de procesos programados

| Filtro | Valores observados |
| --- | --- |
| Año | 2017–2026 |
| Entidad | selector por RUC, sigla, nombre y ubicación |
| Tipo de consulta | `1` todos; `2` convocados; `3` no convocados |
| Objeto | `62` bien; `63` consultoría de obra; `64` obra; `65` servicio |
| Tipo de proceso | catálogo completo de procedimientos |
| Moneda | PEN y monedas extranjeras |
| Tipo de compra | entidad, encargo, centralizada y corporativas |
| Lugar | departamento, provincia y distrito |
| Texto | descripción |
| Financiamiento | ordinarios, recaudados, donaciones, crédito, determinados y privados |

La selección de entidad es un flujo previo dentro de un diálogo JSF. No basta
con enviar el nombre visible: se debe conservar el identificador interno que el
servidor asocia a la sesión.

## Consulta real verificada

Se ejecutaron dos pruebas públicas el 2026-09-22:

### Ministerio Público

```text
RUC: 20131370301
Año: 2026
Procesos programados: 24
Páginas: 2
Filas por página: 20
Última versión PAC: 1
Valor total mostrado: 153,883,493.06
```

### Banco de la Nación

```text
RUC: 20100030595
Año: 2026
Objeto: Servicio
Descripción: SOFTWARE
```

La búsqueda devolvió, entre otros:

```text
Referencia PAC: 56
Descripción: SERVICIO ESPECIALIZADO DE FÁBRICA DE SOFTWARE PARA EL BANCO DE LA NACIÓN (BN)
Tipo: Concurso Público de Servicios
Mes previsto: Febrero
Financiamiento: Recursos Directamente Recaudados/Recursos Propios
CUBSO: 8111150200326499
Unidad: Servicio
Cantidad: 1.00
Moneda: Soles
```

El detalle se abrió en `/visualizarProcesoItem.xhtml` dentro de la misma
sesión. Un GET aislado puede mostrar contenido vacío o lanzar
`ViewExpiredException`.

## Ficha de entidad y versiones

`/fichaEntidad.xhtml` muestra:

- datos y ubicación de la entidad;
- responsable/aprobador;
- año y tres últimas versiones;
- documento y fecha de aprobación;
- estado, monto y cantidad de procesos;
- distribución mensual;
- distribución por tipo de procedimiento;
- ubicación de ejecución e información presupuestal.

Las versiones deben conservarse como snapshots, no sobrescribirse. Una
modificación del PAC no es un duplicado, sino una nueva versión de la misma
planificación institucional.

## Identificadores y deduplicación

No usar la descripción como llave. La identidad recomendada dentro del PAC es:

```text
source_system = SEACE_PAC
year = año PAC
entity_ruc = RUC de entidad
pac_version = versión
pac_reference = número de referencia al PAC
```

Una referencia PAC no comparte necesariamente el `idProcedimiento` de la
convocatoria posterior. El enlace PAC → convocatoria debe manejarse como una
relación probabilística y auditable, usando:

1. RUC de entidad y año;
2. objeto y CUBSO;
3. descripción normalizada;
4. monto/moneda cuando estén disponibles;
5. cercanía entre mes previsto y fecha de convocatoria;
6. identificador explícito de proceso convocado cuando PAC lo publique.

Solo fusionar automáticamente cuando exista un identificador explícito o varias
señales fuertes. En caso contrario, crear `planned_as`/`resulted_in` con nivel
de confianza y revisión posible.

## Exportaciones

PDF y Excel son comandos JSF server-side, no enlaces permanentes. En una
versión observada del buscador de procesos correspondían a componentes como:

```text
formTable_monDlg:j_idt107
formTable_monDlg:j_idt109
```

Los IDs `j_idt*` pueden cambiar con un despliegue. No deben hardcodearse sin un
contract test; ubicar la acción por estructura/título o preferir el HTML
paginado si no se necesita el archivo.

## Paginación y frecuencia

La tabla PAC3 usa paginación PrimeFaces. En la prueba del Ministerio Público:

```text
rows = 20
rowCount = 24
pages = 2
```

El worker debe continuar hasta página vacía o hasta alcanzar `rowCount`,
actualizando `ViewState` en cada respuesta. Frecuencia inicial recomendada:

- procesos no convocados: una vez al día;
- ficha/versiones por entidad: una vez al día o al detectar cambios;
- histórico PAC3: backfill reanudable por entidad/año desde 2017.

## Errores y riesgos

- `ViewExpiredException` por cookie o `ViewState` vencidos.
- IDs generados `j_idt*` inestables entre despliegues.
- Tablas vacías si se abre un detalle fuera de la sesión que lo seleccionó.
- Descripción, mes previsto y monto pueden cambiar entre versiones.
- Una planificación puede modificarse, cancelarse o nunca convocarse.
- No hay API REST documentada ni feed incremental.
- El nombre visible de una entidad no es una PK; usar RUC.
- Una respuesta `200` puede contener una tabla vacía por pérdida de estado; el
  worker debe validar contenido, no solo el código HTTP.

## Implementación

```yaml
adapter_path: pending
fixtures_path: pending
contract_tests_path: pending
polling_frequency: daily
backfill_status: pending
production_enabled: false
```

Flujo recomendado:

```text
GET página inicial
→ guardar cookies
→ extraer ViewState
→ seleccionar entidad
→ ejecutar búsqueda AJAX JSF
→ extraer tabla y nuevo ViewState
→ paginar
→ abrir detalle de ítems dentro de la misma sesión
→ guardar snapshot por versión
→ intentar enlace con PROD4/OCDS
```

### Política del worker diario

```text
por cada entidad activa y año vigente:
  iniciar o renovar sesión PAC3
  seleccionar entidad por RUC
  consultar procesos no convocados y todos los procesos
  paginar hasta terminar
  agrupar por referencia PAC
  abrir detalle solo si es nuevo o cambió su hash
  persistir snapshot por versión
  marcar ausencias sin borrarlas
  reconciliar contra PROD4 y OCDS
```

Una sesión expirada se reintenta una sola vez desde `start_session`. Los errores
repetidos activan circuit breaker; nunca se debe crear un bucle de reintentos
contra el portal.

## Próxima acción

Implementar primero un prototipo de sesión PAC3 para una entidad fixture,
incluyendo selección por RUC, paginación y detalle de ítems. Crear contract
tests que fallen si cambian formularios, componentes JSF o campos esenciales.
