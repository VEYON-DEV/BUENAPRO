# 002 - OECE Contrataciones Abiertas OCDS/API

## Estado

```yaml
status: verified
owner: Organismo Especializado para las Contrataciones Publicas Eficientes (OECE)
country: PE
last_verified_at: 2026-09-21
verified_by: Codex
adapter_key: oece_ocds
```

## Alcance

El Portal de Contrataciones Abiertas publica procesos de compra publica del
SEACE en el estandar OCDS 1.1. Es la fuente estructurada preferida para:

- construir el historico de bienes y servicios
- conservar las distintas entregas (`releases`) de un proceso
- identificar compradores, items CUBSO/UNSPSC y documentos
- registrar adjudicaciones, proveedores ganadores y contratos
- construir perfiles historicos de compradores y proveedores
- analizar participantes, recurrencia, competencia, montos y estacionalidad
- ejecutar backfills mensuales sin recorrer manualmente la interfaz

No debe usarse por si solo para afirmar que una oportunidad **sigue abierta para
postular**. Los filtros `has_planning`, `has_tender`, `has_awards` y
`has_contracts` indican que el expediente contiene esa seccion o etapa; no son
un estado operativo exclusivo. En las pruebas, todos los procesos 2024-2026 de
bienes y servicios con convocatoria tambien aparecieron con planificacion.

La seccion OCDS `planning` tampoco reemplaza al Plan Anual de Contrataciones
(PAC): en los ejemplos observados solo contenia informacion presupuestal de un
procedimiento ya convocado.

## URLs oficiales

| Uso | URL | Verificada |
| --- | --- | --- |
| Portal | `https://contratacionesabiertas.oece.gob.pe/` | si |
| Busqueda | `https://contratacionesabiertas.oece.gob.pe/busqueda` | si |
| Directorio de entidades | `https://contratacionesabiertas.oece.gob.pe/entidades` | si |
| Directorio de proveedores | `https://contratacionesabiertas.oece.gob.pe/proveedores` | si |
| Documentacion OpenAPI | `https://contratacionesabiertas.oece.gob.pe/api` | si |
| Descargas masivas | `https://contratacionesabiertas.oece.gob.pe/descargas` | si |
| Politica de publicacion | `https://contratacionesabiertas.oece.gob.pe/downloads/politica_publicacion.pdf` | si |
| Robots | `https://contratacionesabiertas.oece.gob.pe/robots.txt` | devolvio 403 |

Base API verificada:

```text
https://contratacionesabiertas.oece.gob.pe/api/v1
```

## Acceso y permisos

```yaml
access_mode: public
authentication: none
license: CC-BY-4.0 declarada en los paquetes OCDS
index_allowed: pending
store_metadata_allowed: yes_with_attribution
store_documents_allowed: pending_legal_review
redistribute_allowed: yes_with_attribution_for_ocds_packages
ai_processing_allowed: pending_legal_review_for_linked_documents
legal_reviewed_at: pending
```

Los paquetes probados declaran:

- licencia `https://creativecommons.org/licenses/by/4.0/`
- politica de publicacion del portal
- editor OECE/OSCE en el objeto `publisher`

La licencia declarada cubre el paquete de datos. No se asume automaticamente
que todos los documentos enlazados permitan redistribucion o procesamiento por
IA; BuenaPro debe conservar la URL y revisar ese punto antes de replicarlos.

## Capacidades

| Capacidad | Disponible | Mecanismo |
| --- | --- | --- |
| Descubrimiento | si | `/search`, `/records`, `/recordsAfter` |
| Detalle | si | `/record/{ocid}` |
| Documentos | si | `compiledRelease.tender.documents[]` y documentos de contratos |
| Modificaciones | si | `records[].releases[]` y endpoints de releases |
| Preguntas/respuestas | parcial | `tender.enquiryPeriod`; no se verifico un feed completo de consultas |
| Adjudicaciones | si | `compiledRelease.awards[]` |
| Contratos | si | `compiledRelease.contracts[]` |
| Perfiles de compradores | si | `/buyers`, `/buyer`, `/buyerProcesses`, `/buyerContracts` |
| Historial de nombres | si | `/buyerNameHistory` por RUC |
| Perfiles de proveedores | si | `/suppliers`, `/supplier`, `/supplierProcesses`, `/tendererProcesses`, `/supplierContracts` |
| Planes futuros | no confiable | `planning` es una seccion OCDS, no el PAC operativo |
| Backfill historico | si | archivos mensuales JSON/CSV/XLSX y `/recordsAfter` |

## Endpoints probados

| Metodo | Endpoint | Proposito | Auth | Resultado | Fecha |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/v1/search` | Busqueda usada por la interfaz | ninguna | 200 JSON | 2026-09-20 |
| GET | `/api/v1/recordsAfter` | Recorrido secuencial de mas de 10 000 records | ninguna | 200 RecordPackage con `links.next` | 2026-09-20 |
| GET | `/api/v1/record/{ocid}` | Expediente consolidado y releases | ninguna | 200 RecordPackage | 2026-09-20 |
| GET | `/api/v1/files` | Inventario de archivos mensuales | ninguna | 200 JSON | 2026-09-20 |
| GET | `/api/v1/file/seace_v3/json/2026/09/` | Backfill mensual JSON | ninguna | 200 ZIP, 2 792 222 bytes | 2026-09-20 |
| GET | `/api/v1/file/seace_v3/sha/2026/09/` | Checksum del archivo mensual | ninguna | 200, 64 bytes | 2026-09-20 |
| GET | `/api/v1/recordsCategoryFilter` | Conteos para el filtro de categoria | ninguna | 200 JSON | 2026-09-20 |
| GET | `/api/v1/recordsStageFilter` | Conteos para etapas presentes | ninguna | 200 JSON | 2026-09-20 |
| GET | `/api/v1/process?ocid={ocid}` | Vista consolidada usada por la ficha del proceso | ninguna | 200 JSON | 2026-09-21 |
| GET | `/api/v1/processHistory?ocid={ocid}` | Releases historicas visibles en Historial | ninguna | 200 JSON | 2026-09-21 |
| GET | `/api/v1/buyers` | Directorio paginado de compradores | ninguna | 200 JSON | 2026-09-21 |
| GET | `/api/v1/buyer?id={buyer_id}` | Identidad y datos de un comprador | ninguna | 200 JSON | 2026-09-21 |
| GET | `/api/v1/buyerProcesses` | Procesos de un comprador | ninguna | 200 JSON | 2026-09-21 |
| GET | `/api/v1/buyerContracts` | Contratos de un comprador | ninguna | 200 JSON | 2026-09-21 |
| GET | `/api/v1/buyerNameHistory?ruc={ruc}` | Denominaciones historicas del comprador | ninguna | 200 JSON | 2026-09-21 |
| GET | `/api/v1/suppliers` | Directorio paginado de proveedores/participantes | ninguna | 200 JSON | 2026-09-21 |
| GET | `/api/v1/supplier?id={supplier_id}` | Partido OCDS asociado al proveedor | ninguna | 200 JSON | 2026-09-21 |
| GET | `/api/v1/supplierProcesses` | Procesos que el proveedor gano | ninguna | 200 JSON | 2026-09-21 |
| GET | `/api/v1/tendererProcesses` | Procesos donde participo como oferente | ninguna | 200 JSON | 2026-09-21 |
| GET | `/api/v1/supplierContracts` | Contratos adjudicados al proveedor | ninguna | 200 JSON | 2026-09-21 |

La interfaz tambien consume endpoints auxiliares para facetas:

```text
/recordsBuyerFilter
/recordsYearFilter
/recordsMonthFilter
/recordsSourceFilter
/recordsCategoryFilter
/recordsProcurementFilter
/recordsStageFilter
```

Estos endpoints **no devuelven el universo de procesos**. Solo calculan opciones
y conteos para los filtros visibles. El endpoint de resultados de la pantalla
es `/search`.

## Parametros y paginacion

### Busqueda de la interfaz

Parametros observados y verificados en `/search`:

| Parametro | Ejemplo | Significado |
| --- | --- | --- |
| `page` | `1` | pagina tradicional |
| `paginateBy` | `10` | resultados por pagina |
| `year` | `2026` | anio de segmentacion/convocatoria |
| `category` | `goods`, `services` | bienes o servicios |
| `has_planning` | `1` | contiene planificacion OCDS |
| `has_tender` | `1` | contiene convocatoria |
| `has_awards` | `1` | contiene una o mas adjudicaciones |
| `has_contracts` | `1` | contiene uno o mas contratos |
| `format` | `json` | respuesta JSON |

Los indicadores de etapa son acumulativos: un proceso contratado puede tener
planificacion, convocatoria, adjudicacion y contrato al mismo tiempo.

### API OCDS documentada

`/records` usa `page` y esta documentado para explorar menos de 10 000
resultados. `/recordsAfter` esta documentado para universos mayores y acepta:

- `size`: hasta 100
- `sourceId`: `seace_v3` o `seace_v2`
- `startDate`, `endDate`: fechas `YYYY-MM-DD`
- `dataSegmentationID`: mes `YYYY-MM`
- `tenderId`
- `ocid`
- `tenderTitle`
- `mainProcurementCategory`: `goods`, `works` o `services`

Para continuar, no se debe reconstruir el cursor. Se persiste y solicita
exactamente la URL de `links.next` hasta que sea nula. El record se deduplica por
`ocid` y cada cambio por `release.id`.

### Perfiles de compradores y proveedores

Comprobación en navegador del 2026-10-05: `/entidades` sin filtros mostró
**3,316 entidades compradoras**, 332 páginas a 10 filas. Es el universo del
directorio OCDS observado, no el número de entidades activas hoy, de compradores
de tecnología ni un censo completo de los convocantes actuales de PROD2. La
primera página incluye últimos procesos de 2018–2023. El acceso HTTP directo a
`/api/v1/buyers?format=json` respondió 403 desde el cliente local, mientras la
página oficial cargó normalmente en navegador. No usar el directorio histórico
como señal de oportunidades vigentes ni tratarlo como lista inmutable.

La pagina de una entidad no usa `/search`: consume endpoints especializados.
Para `PE-CONSUCODE-1922` se verificaron:

```text
/buyer?id=PE-CONSUCODE-1922
/buyerProcesses?buyerID=PE-CONSUCODE-1922&processesPage=1&processesPaginateBy=5
/buyerContracts?buyerID=PE-CONSUCODE-1922&contractsPage=1&contractsPaginateBy=5
/buyerNameHistory?ruc=20131370564
```

Filtros de procesos del comprador:

```text
processes_description
processes_nomenclature
processes_date
processes_date_end_date
processes_supplier
processes_tender_id
order_processes_description
order_processes_date
order_processes_nomenclature
order_processes_supplier
order_processes_tender_id
type_processes_date
```

Filtros de contratos del comprador:

```text
contractTitle
contractDescription
contractSupplier
contractAmount
typeContractAmount
contractDateSigned
contractDateSignedEndDate
typeContractDateSigned
orderContractTitle
orderContractDescription
orderContractSupplier
orderContractAmount
orderContractDateSigned
```

Los operadores observados incluyen igualdad y rango (`between`) para montos y
fechas. Los valores de orden deben conservarse como configuracion del adaptador
y probarse mediante contract tests, no codificarse como supuestos permanentes.

La pagina de proveedor usa el mismo patron con:

```text
/supplier?id={PE-RUC-*}
/supplierProcesses?supplierID={PE-RUC-*}
/tendererProcesses?tendererID={PE-RUC-*}
/supplierContracts?supplierID={PE-RUC-*}
```

`supplierProcesses` representa resultados donde aparece como proveedor
adjudicado; `tendererProcesses` permite observar participaciones aunque no haya
ganado. Esa diferencia permite calcular tasas de exito sin confundir oferta con
adjudicacion.

## Ejemplos sanitizados

### Resultados de bienes convocados

```bash
curl --fail-with-body \
  'https://contratacionesabiertas.oece.gob.pe/api/v1/search?page=1&paginateBy=10&has_tender=1&category=goods&year=2026&format=json'
```

### Adjudicaciones de servicios

```bash
curl --fail-with-body \
  'https://contratacionesabiertas.oece.gob.pe/api/v1/search?page=1&paginateBy=10&has_awards=1&category=services&year=2025&format=json'
```

### Recorrido completo con cursor

```bash
curl --fail-with-body \
  'https://contratacionesabiertas.oece.gob.pe/api/v1/recordsAfter?size=100&sourceId=seace_v3&startDate=2026-09-01&endDate=2026-09-30&mainProcurementCategory=goods'
```

### Detalle por OCID

```bash
curl --fail-with-body \
  'https://contratacionesabiertas.oece.gob.pe/api/v1/record/ocds-dgv273-seacev3-1247314?format=json'
```

### Perfil e historial de un comprador

```bash
curl --fail-with-body \
  'https://contratacionesabiertas.oece.gob.pe/api/v1/buyer?id=PE-CONSUCODE-1922&format=json'

curl --fail-with-body \
  'https://contratacionesabiertas.oece.gob.pe/api/v1/buyerProcesses?buyerID=PE-CONSUCODE-1922&processesPage=1&processesPaginateBy=100&format=json'

curl --fail-with-body \
  'https://contratacionesabiertas.oece.gob.pe/api/v1/buyerContracts?buyerID=PE-CONSUCODE-1922&contractsPage=1&contractsPaginateBy=100&format=json'
```

### Proceso consolidado e historial

```bash
curl --fail-with-body \
  'https://contratacionesabiertas.oece.gob.pe/api/v1/process?ocid=ocds-dgv273-seacev3-1248325&format=json'

curl --fail-with-body \
  'https://contratacionesabiertas.oece.gob.pe/api/v1/processHistory?ocid=ocds-dgv273-seacev3-1248325&format=json'
```

### Perfil, participaciones, adjudicaciones y contratos de un proveedor

```bash
SUPPLIER_ID='PE-RUC-20250314745'

curl --fail-with-body \
  "https://contratacionesabiertas.oece.gob.pe/api/v1/supplier?id=${SUPPLIER_ID}&format=json"

curl --fail-with-body \
  "https://contratacionesabiertas.oece.gob.pe/api/v1/tendererProcesses?tendererID=${SUPPLIER_ID}&processesTendererPage=1&processesTendererPaginateBy=100&format=json"

curl --fail-with-body \
  "https://contratacionesabiertas.oece.gob.pe/api/v1/supplierProcesses?supplierID=${SUPPLIER_ID}&processesPage=1&processesPaginateBy=100&format=json"

curl --fail-with-body \
  "https://contratacionesabiertas.oece.gob.pe/api/v1/supplierContracts?supplierID=${SUPPLIER_ID}&contractsPage=1&contractsPaginateBy=100&format=json"
```

En `/supplier`, localizar siempre la parte mediante coincidencia exacta:

```text
result.compiledRelease.parties[].id == supplier_id
```

No usar la primera parte del arreglo, porque puede ser el comprador u otro
participante del proceso usado por el portal como contexto del perfil.

### Inventario y descarga mensual

```bash
curl --fail-with-body \
  'https://contratacionesabiertas.oece.gob.pe/api/v1/files?page=1&paginateBy=20&source=seace_v3&year=2024'

curl --fail-with-body --output 2024-01_seace_v3_json.zip \
  'https://contratacionesabiertas.oece.gob.pe/api/v1/file/seace_v3/json/2024/01/'

curl --fail-with-body --output 2024-01_seace_v3.sha \
  'https://contratacionesabiertas.oece.gob.pe/api/v1/file/seace_v3/sha/2024/01/'
```

## Identificadores

| Entidad | Campo nativo | Estabilidad | Uso canonico |
| --- | --- | --- | --- |
| Proceso | `records[].ocid` | alta | `external_id` de la oportunidad |
| Convocatoria | `compiledRelease.tender.id` | alta dentro de la fuente | identificador auxiliar SEACE |
| Version/cambio | `releases[].details.id` o release completo `id` | alta | `source_release_id` |
| Item | `tender.items[].id` | media/alta | `external_lot_id` o item nativo |
| Documento | `tender.documents[].id` | media/alta | `external_document_id` |
| Comprador | `buyer.id` / `procuringEntity.id` | alta | organization identifier |
| RUC del comprador | `parties[].additionalIdentifiers[scheme=PE-RUC].id` | alta | organization tax identifier |
| Proveedor | `awards[].suppliers[].id` | alta | supplier identifier |
| Contrato | `contracts[].id` | alta | external contract id |

## Mapeo canonico

| Campo canonico | Campo fuente | Regla | Confianza |
| --- | --- | --- | --- |
| `title` | `compiledRelease.tender.title` | conservar nomenclatura | alta |
| `description` | `compiledRelease.tender.description` | normalizar espacios sin perder original | alta |
| `object_type` | `tender.mainProcurementCategory` | `goods` -> bien; `services` -> servicio; `works` -> obra | alta |
| `procedure_type` | `tender.procurementMethodDetails` | tabla versionada de equivalencias | alta |
| `stage` | presencia de `planning/tender/awards/contracts` | tomar etapa mas avanzada, sin inferir apertura | media |
| `participation_access` | no confiable en OCDS | resolver desde SEACE operativo | baja |
| `buyer` | `buyer` y `tender.procuringEntity` | conservar ambos y reconciliar | alta |
| `published_at` | `tender.datePublished` | fecha de convocatoria | alta |
| `closes_at` | no confiable solo con `tenderPeriod.endDate` | cruzar cronograma/SEACE operativo | baja |
| `amount` | `tender.value.amount` | permitir cero/reservado; adjudicado desde `awards[].value` | alta |
| `currency` | `*.value.currency` | ISO, normalmente PEN | alta |
| `awarded_at` | `awards[].date` | una fila por adjudicacion | alta |
| `winner` | `awards[].suppliers[]` | preservar todos los ganadores | alta |
| `contracted_at` | `contracts[].dateSigned` | una fila por contrato | alta |

## Inteligencia de compradores y proveedores

La fuente permite generar productos derivados que no deben perderse al
normalizar oportunidades:

### Perfil del comprador

- identificador OECE/CONSUCODE y RUC
- nombre vigente y nombres historicos con primer/ultimo anio
- direccion, departamento, provincia/localidad y sitio institucional
- numero de procesos y contratos
- bienes, servicios y obras comprados por CUBSO/UNSPSC
- procedimientos de seleccion usados
- frecuencia y estacionalidad mensual/anual
- montos convocados, adjudicados y contratados por moneda
- proveedores recurrentes y concentracion de adjudicaciones
- duracion entre convocatoria, adjudicacion, firma y fin de contrato
- documentos y bases historicas asociadas

### Perfil del proveedor

- identificador PE-RUC o identificador extranjero
- procesos donde participo como `tenderer`
- procesos donde gano como `supplier`
- contratos, compradores, categorias, items, montos y monedas
- tasa de adjudicacion sobre participaciones observadas
- compradores recurrentes y rubros de mayor experiencia
- competidores que aparecen en los mismos procedimientos

### Grafo competitivo

```text
comprador -> proceso -> item/CUBSO
                    -> participante
                    -> adjudicacion -> ganador
                    -> contrato -> monto/fecha/documentos
```

BuenaPro debe persistir estas relaciones en `organizations`,
`organization_identifiers`, `organization_aliases`, `organization_roles`,
`awards`, `award_suppliers` y `contracts`, ya previstos en el plan universal.
No deben aplastarse en una sola fila de oportunidad.

### Caso verificado: SBS

Para `PE-CONSUCODE-1922` el 2026-09-21:

- `/buyerProcesses`: 4 445 procesos, 889 paginas a 5 resultados
- `/buyerContracts`: 2 999 contratos, 600 paginas a 5 resultados
- `/buyerNameHistory`: dos denominaciones observadas desde 2004
- el primer contrato probado incluyo OCID, convocatoria, contrato, ganador,
  participantes, items CUBSO/UNSPSC, monto PEN, fecha de firma y periodo

El directorio general tambien devolvio 3 316 compradores y 501 549 entradas de
proveedores/participantes. Estas cantidades son fotografias del indice y pueden
cambiar con la carga o correccion de datos.

### Caso verificado: FIX IT S.A.

Perfil publico:

```text
https://contratacionesabiertas.oece.gob.pe/proveedor/PE-RUC-20250314745
```

Resultados observados el 2026-09-21:

| Dimension | Endpoint | Total |
| --- | --- | ---: |
| Identidad | `/supplier?id=PE-RUC-20250314745` | FIX IT S.A., RUC 20250314745 |
| Participaciones como oferente | `/tendererProcesses` | 245 |
| Procesos adjudicados | `/supplierProcesses` | 95 |
| Contratos | `/supplierContracts` | 72 |

La razon simple `95 / 245` da una tasa observada aproximada de 38.8 %, pero no
debe presentarse como tasa definitiva hasta deduplicar OCID, separar items y
validar que ambos endpoints cubran el mismo periodo y universo.

El primer contrato probado incluyo:

- comprador `PE-CONSUCODE-1922` (SBS)
- OCID `ocds-dgv273-seacev3-167797`
- numero de contrato `018-2015`
- fecha de firma y periodo contractual
- monto original y monto en PEN
- proveedor ganador
- item, cantidad y unidad
- clasificacion CUBSO y UNSPSC

Esto permite que el adaptador produzca cuatro conjuntos separados:

```text
supplier_profile
supplier_participations
supplier_awards
supplier_contracts
```

La API canonica de BuenaPro deberia exponerlos sin reproducir la estructura
accidental del portal:

```text
GET /api/market/suppliers/{supplier_id}
GET /api/market/suppliers/{supplier_id}/participations
GET /api/market/suppliers/{supplier_id}/awards
GET /api/market/suppliers/{supplier_id}/contracts
GET /api/market/suppliers/{supplier_id}/buyers
GET /api/market/suppliers/{supplier_id}/competitors
```

Estas rutas son una propuesta interna; no estan implementadas todavia.

La respuesta de `/supplier` puede contener el `compiledRelease` de un proceso
asociado con varias partes. Para identificar al proveedor se debe buscar por su
`party.id` exacto; no se debe asumir que la primera parte de la lista es el
proveedor solicitado.

Por minimizacion de datos, BuenaPro no debe indexar correos o telefonos de
personas naturales. Los datos de contacto institucionales solo se conservan si
son necesarios para una funcionalidad explicita y con revision legal.

## Estados y lifecycle

El lifecycle no se modela como un unico filtro excluyente:

```text
planning -> tender -> awards -> contracts
```

Un record consolidado puede contener todas las etapas. Para el historico se
guardan tanto `compiledRelease` como las releases, porque la version compilada
describe el estado conocido mas reciente y las releases permiten reconstruir
los cambios.

Reglas para BuenaPro:

1. `has_tender=1` significa **tiene convocatoria**, no **acepta propuestas hoy**.
2. `has_awards=1` significa que existe al menos una adjudicacion; puede existir
   tambien un contrato posterior.
3. `has_contracts=1` significa que el proceso llego a contrato.
4. `items[].status=active` o `statusDetails=CONVOCADO` no bastan para afirmar
   que el plazo de postulacion sigue abierto.
5. `is_open_for_application` solo sera verdadero cuando una fuente operativa
   confirme estado vigente y exista una fecha limite futura o una regla nativa
   equivalente.
6. Para reprogramaciones, anulaciones y nuevas versiones se comparan releases,
   no solo la version compilada.

## Documentos

Los documentos aparecen principalmente en:

- `compiledRelease.tender.documents[]`
- `compiledRelease.contracts[].documents[]`

Se observaron URLs de SEACE/Alfresco y del API de documentos de contratos, con
formatos PDF y ZIP. El adaptador debe conservar `id`, `url`, `title`,
`documentType`, `format`, `datePublished` y `language`. La descarga y extraccion
se delegan al pipeline documental universal, con limites, aislamiento de
archivos comprimidos y deteccion real de MIME.

## Historico 2024-2026

Conteos devueltos por `/search` el 2026-09-20:

| Anio | Categoria | Con convocatoria | Con adjudicacion | Con contrato |
| ---: | --- | ---: | ---: | ---: |
| 2024 | Bienes | 35 513 | 28 752 | 26 362 |
| 2024 | Servicios | 35 728 | 28 142 | 25 490 |
| 2025 | Bienes | 33 985 | 26 907 | 24 733 |
| 2025 | Servicios | 34 639 | 26 883 | 24 424 |
| 2026 | Bienes | 23 315 | 16 342 | 13 006 |
| 2026 | Servicios | 23 279 | 16 300 | 12 636 |

Total observado de procesos con convocatoria para bienes y servicios en esos
tres anios: **186 459**. Los conteos son una fotografia y pueden cambiar por
correcciones o nuevas publicaciones.

El inventario `/files` reporto 12 archivos mensuales para 2024, 12 para 2025 y
9 para 2026 hasta septiembre. El ZIP JSON de septiembre de 2026 contenia un
RecordPackage con 3 957 records. Siempre validar el archivo contra su `.sha`.

## Estrategia de ingesta

### Backfill inicial

1. Consultar `/files` por `source=seace_v3` y anio.
2. Descargar JSON ZIP y SHA de cada mes de 2024, 2025 y 2026.
3. Verificar checksum antes de descomprimir.
4. Guardar cada record por `ocid` y cada release por su identificador.
5. Filtrar canonicamente `goods` y `services`, sin descartar el original.
6. Extraer adjudicaciones y contratos como entidades hijas, no como columnas
   unicas, porque un proceso puede tener varios.
7. Reconciliar los totales mensuales contra `/search` y `/files`.

### Incremental

1. Ejecutar `/recordsAfter` en ventanas pequenas y `size=100`.
2. Seguir `links.next` exactamente hasta `null`.
3. Hacer upsert idempotente por OCID y release ID.
4. Volver a consultar una ventana de solapamiento para capturar correcciones.
5. Consultar `/record/{ocid}` al detectar cambios o cuando el usuario abre el
   detalle.

### Feed de postulacion

El feed vivo no se decide con OCDS solamente:

```text
OCDS descubre y aporta historico
  + SEACE operativo confirma estado y cronograma
  = oportunidad realmente postulable
```

Para contratos menores ya existe la fuente `prod6`. Para procedimientos
generales se debe completar la guia 003 (Buscador Publico SEACE) y la guia 004
(SEACE Oportunidades). Para demanda futura real se debe completar la guia 005
(PAC).

## Rate limits, cache y errores

- No se observo autenticacion ni un limite publicado durante estas pruebas.
- Usar un `User-Agent` identificable y correo de contacto cuando se habilite
  produccion.
- Limitar concurrencia y aplicar backoff exponencial con jitter para 429 y 5xx.
- Respetar `ETag` y `Last-Modified` en archivos masivos.
- Los archivos mensuales son preferibles a miles de llamadas de detalle.
- Guardar checkpoint de `links.next`; el cursor es opaco.
- El portal se identifica como beta, por lo que el schema debe validarse en
  cada ejecucion y conservar el JSON original.

## Pruebas ejecutadas

| Prueba | Resultado | Evidencia | Fecha |
| --- | --- | --- | --- |
| Healthcheck portal/API | 200 | portal, busqueda y `/api` | 2026-09-20 |
| Primera pagina de busqueda | 200 | bienes 2026 | 2026-09-20 |
| Filtros bienes/servicios | 200 | 2024, 2025 y 2026 | 2026-09-20 |
| Etapas | 200 | planning, tender, awards y contracts | 2026-09-20 |
| Detalle | 200 | OCID de bien con documentos | 2026-09-20 |
| Adjudicacion/contrato | 200 | ganador, importe y contrato observados | 2026-09-20 |
| Cursor | 200 | `/recordsAfter` devolvio `links.next` | 2026-09-20 |
| Inventario historico | 200 | `/files` para 2024-2026 | 2026-09-20 |
| Descarga mensual | 200 | ZIP JSON septiembre 2026 | 2026-09-20 |
| Checksum | 200 | archivo SHA de 64 bytes | 2026-09-20 |
| Perfil comprador | 200 | `/buyer`, identidad SBS y RUC | 2026-09-21 |
| Procesos comprador | 200 | 4 445 procesos SBS | 2026-09-21 |
| Contratos comprador | 200 | 2 999 contratos SBS | 2026-09-21 |
| Historial de nombres | 200 | dos denominaciones SBS | 2026-09-21 |
| Proceso consolidado | 200 | items, oferentes, documentos y award | 2026-09-21 |
| Historial de releases | 200 | dos releases para OCID probado | 2026-09-21 |
| Perfil proveedor | 200 | identidad encontrada por `party.id` | 2026-09-21 |
| Participaciones proveedor | 200 | `/tendererProcesses`, paginado | 2026-09-21 |
| Adjudicaciones proveedor | 200 | `/supplierProcesses`, paginado | 2026-09-21 |
| Contratos proveedor | 200 | `/supplierContracts`, paginado | 2026-09-21 |
| Proveedor FIX IT S.A. | 200 | identidad, 245 participaciones, 95 adjudicaciones y 72 contratos | 2026-09-21 |

## Limitaciones y riesgos

- `has_planning` no representa por si mismo una compra futura del PAC.
- `has_tender` no garantiza que el periodo para presentar oferta siga abierto.
- En muestras observadas, `tenderPeriod.startDate` y `endDate` pueden ser el
  mismo dia y no deben asumirse como cronograma completo de postulacion.
- Algunos montos son cero o estan reservados hasta una etapa posterior.
- El portal es beta y puede cambiar endpoints o schema.
- Los conteos del anio corriente son parciales y mutables.
- Puede haber varios awards, proveedores, items y contratos por OCID.
- Los directorios de proveedores incluyen participantes y entidades extranjeras;
  no equivalen a un padron depurado de proveedores nacionales activos.
- Las metricas derivadas deben distinguir participacion, adjudicacion y contrato.
- Un comprador o proveedor puede cambiar de nombre; reconciliar por esquema e
  identificador y conservar alias historicos.
- Las URLs documentales pertenecen a sistemas externos y pueden expirar o
  cambiar de disponibilidad.

## Implementacion

```yaml
adapter_path: pending
fixtures_path: pending
contract_tests_path: pending
polling_frequency: pending
backfill_status: pending
production_enabled: false
```

## Proxima accion

Implementar `oece_ocds` usando primero los archivos mensuales 2024-2026 y
`/recordsAfter` para actualizaciones; despues cruzar cada candidato vigente con
los adaptadores operativos SEACE antes de mostrarlo como postulable.
