# 001 - SEACE/OECE Contratos Menores

## Estado

```yaml
status: VERIFICADA
scope_verified: buscador, detalle, archivos y descarga publica para bienes
owner: OECE
country: PE
last_verified_at: 2026-09-20
adapter_key: seace_minor_contracts
implementation: existente en worker SEACE; pendiente migracion universal y polling multiobjeto
```

Esta guia comienza por la ingesta de **bienes**. Las pruebas fueron read-only y no generaron cotizaciones, postulaciones ni registros externos.

## Endpoint base

```text
GET https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico/contrataciones/buscador
```

No requiere autenticacion para consultar. La participacion o cotizacion usa otro flujo y no forma parte de estas pruebas.

## Integracion que ya existe en el proyecto

El worker actual ya implementa los endpoints de lectura de esta fuente:

| Capacidad | Implementacion actualien  |
| --- | --- |
| Busqueda | `SeaceClient.search_contracts` |
| Detalle | `SeaceClient.contract_detail` |
| Archivos por categoria | `SeaceClient.list_files` |
| Descarga | `SeaceClient.download_file` |

Referencias:

- `workers/seace/src/buenapro_worker/seace/client.py`
- `workers/seace/src/buenapro_worker/jobs/process_contract.py`
- `workers/seace/src/buenapro_worker/jobs/download_file.py`
- `workers/seace/tests/seace_contract_test.py`

`process_contract` consulta detalle cuando corresponde, lista categoria 1 y 2, guarda sus metadatos y encola la descarga de categoria 1. `download_file_job` calcula SHA-256, detecta MIME, clasifica el documento y encola extraccion cuando es PDF. El contract test existente ya recorre busqueda, detalle, archivos y descarga, aunque usa el objeto principal configurado actualmente.

## Parametros confirmados para bienes

| Parametro | Uso confirmado |
| --- | --- |
| `anio` | Obligatorio para evitar `400`, pero no filtro confiable; ver advertencia de año. |
| `lista_codigo_objeto=1` | Filtra el objeto oficial `Bien`. |
| `segmento` | Filtro opcional por segmento CUBSO, por ejemplo `43` o `81`. |
| `lista_estado_contrato=2` | Filtra contratos con estado oficial `Vigente`. |
| `palabra_clave` | Busqueda textual opcional; puede enviarse vacio. |
| `orden=2` | Orden usado por el portal y por estas pruebas. |
| `page` | Pagina basada en 1. |
| `page_size` | Cantidad solicitada por pagina. |

## Endpoint recomendado para bienes vigentes

```text
https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico/contrataciones/buscador?anio=2024&lista_codigo_objeto=1&lista_estado_contrato=2&palabra_clave=&orden=2&page=1&page_size=5
```

Resultado de control del 20 de septiembre de 2026:

```json
{
  "pageable": {
    "pageNumber": 1,
    "pageSize": 5,
    "totalElements": 1087
  },
  "item_count": 5,
  "states": ["Vigente"],
  "objects": ["Bien"]
}
```

`totalElements` es una medicion viva y cambiara con el portal.

## Requests proporcionados y verificados

### 1. Todos los bienes, sin estado ni segmento

```text
?anio=2024&lista_codigo_objeto=1&palabra_clave=&orden=2&page=1&page_size=5
```

```yaml
http_status: 200
content_type: application/json
items_returned: 5
total_elements: 26244
object_values: [Bien]
```

La primera pagina devolvio procesos publicados en 2026 pese a recibir `anio=2024`.

### 2. Bienes del segmento 81

```text
?anio=2024&segmento=81&lista_codigo_objeto=1&palabra_clave=&orden=2&page=1&page_size=5
```

```yaml
http_status: 200
items_returned: 3
total_elements: 3
states_observed:
  - En Evaluacion
  - Culminado
object_values: [Bien]
```

Uno de los registros describe una suscripcion de software, pero la fuente lo clasifica oficialmente como `Bien`. BuenaPro debe conservar esa clasificacion nativa aunque agregue etiquetas propias.

### 3. Bienes del segmento 43

```text
?anio=2024&segmento=43&lista_codigo_objeto=1&palabra_clave=&orden=2&page=1&page_size=5
```

```yaml
http_status: 200
items_returned: 5
total_elements: 1670
states_observed:
  - Vigente
  - En Evaluacion
object_values: [Bien]
```

### 4. Bienes vigentes del segmento 43

```text
?anio=2024&segmento=43&lista_codigo_objeto=1&lista_estado_contrato=2&palabra_clave=&orden=2&page=1&page_size=5
```

```yaml
http_status: 200
items_returned: 5
total_elements: 57
states_observed: [Vigente]
object_values: [Bien]
```

## Estructura de respuesta

```json
{
  "data": [
    {
      "secuencia": 1,
      "idContrato": 95716,
      "nroContratacion": 269,
      "desContratacion": "CM-269-2026-CSJHA",
      "idObjetoContrato": 1,
      "nomObjetoContrato": "Bien",
      "desObjetoContrato": "ADQUISICION ...",
      "fecIniCotizacion": "18/09/2026 16:30:00",
      "fecFinCotizacion": "19/09/2026 08:00:00",
      "cotizar": false,
      "idEstadoContrato": 2,
      "nomEstadoContrato": "Vigente",
      "fecPublica": "18/09/2026 16:09:03",
      "idTipoCotizacion": 2,
      "nomEntidad": "CORTE SUPERIOR DE JUSTICIA DE HUAURA"
    }
  ],
  "pageable": {
    "pageNumber": 1,
    "pageSize": 5,
    "totalElements": 57
  }
}
```

El ejemplo conserva la forma real, pero se recorto la descripcion para mantener la guia legible.

## Detalle, archivos y descarga verificados

Se uso el bien `idContrato=95815`, obtenido del buscador durante esta misma verificacion.

### Detalle

```text
GET /buscadorpublico/contrataciones/listar-completo?id_contrato=95815
```

```yaml
http_status: 200
content_type: application/json
top_level_keys:
  - uitContratoCompletoProjection
  - uitContratoEtapaProjectionList
  - uitContratoItemProjectionList
contract:
  idContrato: 95815
  idObjetoContrato: 1
  nomObjetoContrato: Bien
  nomTipoInvitacion: INVITACION ABIERTA
  anio: 2026
stage_count: 2
item_count: 1
```

El item de detalle incluyo codigo CUBSO, nombre CUBSO, unidad de medida, ubicacion, descripcion y cantidad.

### Archivos categoria 1

```text
GET /archivo/archivos-publico/listar-archivos-contrato/95815/1
```

```yaml
http_status: 200
count: 1
idContratoArchivo: 380567
filename: EETT- PC 2216_compressed.pdf
declared_mime: application/pdf
declared_size: 910110
```

### Archivos categoria 2

```text
GET /archivo/archivos-publico/listar-archivos-contrato/95815/2
```

```yaml
http_status: 200
count: 1
idContratoArchivo: 380585
filename: DECLARACION JURADA Y CARTA DE PROPUESTA ECONOMICA - UNALM (1).pdf
declared_mime: application/pdf
declared_size: 118322
```

### Descarga

```text
GET /archivo/archivos-publico/descargar-archivo-contrato/380567
```

```yaml
http_status: 200
response_content_type: application/octet-stream
downloaded_size: 910110
detected_mime: application/pdf
sha256: 6749fd58c636fcef7ed432c961f6d53084a114130f62493f5e3800189e9c9cdd
```

El `Content-Type` HTTP de descarga es generico; el worker actua correctamente al detectar el MIME por contenido.

## Identificador y mapeo inicial

| Campo fuente | Uso |
| --- | --- |
| `idContrato` | `external_id` estable dentro de esta fuente. |
| `desContratacion` | Codigo visible del proceso. |
| `idObjetoContrato` | Codigo nativo del objeto; `1` representa bien. |
| `nomObjetoContrato` | Nombre nativo del objeto. |
| `desObjetoContrato` | Titulo/descripcion comercial. |
| `idEstadoContrato` | Codigo nativo del estado. |
| `nomEstadoContrato` | Estado legible. |
| `fecPublica` | Fecha de publicacion. |
| `fecIniCotizacion` | Inicio de cotizacion. |
| `fecFinCotizacion` | Cierre de cotizacion. |
| `nomEntidad` | Comprador; debe resolverse contra organizacion/RUC en detalle. |
| `cotizar` | Señal operativa del portal; no reemplaza la evaluacion de fechas y acceso. |

Mapeo canonico minimo:

```yaml
object_type: GOOD
procedure_type: RFQ
participation_access: REGISTRATION_REQUIRED
source_external_id: idContrato
native_object: nomObjetoContrato
native_status: nomEstadoContrato
```

La modalidad canonica final debe confirmarse con detalle y reglas de la fuente; `RFQ` es el comportamiento operativo esperado para contratos menores, no una sustitucion del dato oficial.

## Paginacion

Se probaron las paginas 1 y 2 de bienes vigentes del segmento 43:

```text
page=1 -> 95716, 95655, 95631, 95621, 95462
page=2 -> 95450, 95437, 95422, 95418, 95349
```

Ambas respondieron `200`, reportaron `pageNumber` correcto y no repitieron IDs en la muestra.

Implementacion recomendada:

1. Comenzar en `page=1`.
2. Hacer upsert/dedupe por `(source_id, idContrato)`.
3. Avanzar mientras `data` no este vacio y queden elementos segun `totalElements`.
4. No confiar solamente en `totalElements`, porque puede cambiar durante el recorrido.
5. Guardar checkpoint por objeto, segmento, estado, orden y pagina.

## Advertencia: `anio` es obligatorio pero fue ignorado

Pruebas con `anio=2024`, `anio=2025` y `anio=2026` devolvieron:

- `200 OK`
- `totalElements=26244`
- los mismos cinco IDs iniciales
- los mismos codigos y fechas de publicacion de 2026

Omitir `anio` o enviar solamente `lista_anio=2024` produjo `400 Bad Request`.

Conclusiones para el adaptador:

- enviar `anio` porque el endpoint lo exige
- no usarlo como garantia de particion historica
- derivar y validar el año desde codigo y fechas cuando sea necesario
- usar fuentes historicas/OCDS o filtros adicionales para un backfill anual confiable
- mantener un contract test que detecte si OECE corrige o cambia este comportamiento

## Fechas

La respuesta usa:

```text
dd/MM/yyyy HH:mm:ss
```

No incluye offset. BuenaPro debe interpretarla en `America/Lima`, guardar UTC y conservar el texto original en la observacion de fuente.

## Ejemplo reproducible

```bash
curl --silent --show-error --fail-with-body --location \
  'https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico/contrataciones/buscador?anio=2024&lista_codigo_objeto=1&lista_estado_contrato=2&palabra_clave=&orden=2&page=1&page_size=5' \
  | jq '{pageable, data}'
```

## Alcance de implementacion de bienes

Para agregar bienes al worker actual:

1. Permitir los objetos `1,2` en configuracion, sin reemplazar el objeto `2` de servicios.
2. Modificar `poll_search` para iterar todos los `allowed_codigo_objeto`; actualmente consulta solo `primary_codigo_objeto`.
3. Crear trabajos separados por objeto y, cuando corresponda, por segmento CUBSO.
4. Incluir `lista_codigo_objeto=1` en los requests de bienes.
5. Usar `lista_estado_contrato=2` para el feed vivo.
6. Persistir `idObjetoContrato=1` y `nomObjetoContrato=Bien` como valores nativos.
7. Reutilizar el detalle, categorias 1/2, descarga, hash y extraccion ya implementados.
8. Separar el backfill historico del polling vigente.
9. Agregar contract tests para bienes sin segmento, segmento 43 y segmento 81.

No se recomienda hardcodear bienes como unico objeto; el registro universal debe admitir bienes, servicios, obras y consultorias.

## Pendiente de implementacion para habilitar bienes en produccion

- verificar parametros de palabra clave y orden
- probar limites de `page_size`
- probar estados 3 y 4 de forma controlada
- confirmar rate limits, cache y headers relevantes
- iterar todos los objetos configurados en `poll_search`
- agregar contract test automatizado especifico de bienes
- implementar la migracion al adaptador universal y sus fixtures
- ejecutar reconciliacion y backfill permitido

Los endpoints publicos necesarios para bienes estan verificados. Lo pendiente es habilitar el objeto `1` sin perder servicios, automatizar su prueba y migrar la integracion existente al contrato universal.
