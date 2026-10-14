# 009 - Peru Compras: Consulta de Ordenes CEAM

## Estado

```yaml
status: VERIFICADA
owner: Central de Compras Publicas - Peru Compras
country: PE
last_verified_at: 2026-09-23
adapter_key: peru_compras_orders_ocds
implementation: pendiente
```

## Alcance

La Consulta de Ordenes publica compras y servicios formalizados mediante los Catalogos Electronicos de Acuerdos Marco (CEAM). Es especialmente valiosa para inteligencia de mercado: entidades compradoras, proveedores adjudicados, productos, precios, cantidades, fechas, estados y documentos de la orden.

No es, en general, una fuente de concursos futuros para postular. Debe alimentar el historial de compras, el grafo comprador-proveedor, precios de referencia y senales de demanda.

## URLs oficiales

| Uso | URL | Verificada |
| --- | --- | --- |
| Consulta publica | `https://catalogos.perucompras.gob.pe/ConsultaOrdenesPub` | si |
| Acceso general CEAM | `https://catalogos.perucompras.gob.pe/AccesoGeneral` | si |
| Buscador de catalogos | `https://buscadorcatalogos.perucompras.gob.pe/` | si |
| Peru Observa | `https://peruobserva.perucompras.gob.pe/` | si |

## Acceso y licencia

```yaml
access_mode: public para consulta/descarga; registered para operar en CEAM
authentication: none para ConsultaOrdenesPub
index_allowed: si para el paquete OCDS, sujeto a su licencia
store_metadata_allowed: si para el paquete OCDS, sujeto a su licencia
store_documents_allowed: revisar politica de publicacion por documento
redistribute_allowed: ODC-By 1.0 con atribucion para la descarga OCDS
ai_processing_allowed: pendiente de revision legal
```

La descarga probada declara:

```text
license: https://opendatacommons.org/licenses/by/1-0/
version: 1.1
publisher: Central de Compras Peru Compras
```

## Endpoints probados

| Metodo | Endpoint | Proposito | Resultado |
| --- | --- | --- | --- |
| POST | `/ConsultaOrdenesPub/obtenerFiltros` | acuerdos, catalogos y rutas | 200 texto delimitado |
| POST | `/ConsultaOrdenesPub/listarEntidad` | entidades compradoras | 200 texto delimitado |
| POST | `/ConsultaOrdenesPub/listarRUCProveedor` | proveedores del acuerdo | 200 texto delimitado |
| POST | `/ConsultaOrdenesPub/consultaOrdenes` | tabla interactiva | 200 texto delimitado |
| POST | `/ConsultaOrdenesPub/consultaOrdenesSinDetalle` | exportacion de cabeceras | 200 texto delimitado |
| POST | `/ConsultaOrdenesPub/consultaOrdenesDetallado` | lineas, fichas, precios y entregas | 200 texto delimitado |
| GET | `/ConsultaOrdenesPub/DescargaJsonOCDS` | paquete OCDS filtrado | 200 attachment JSON |
| GET | `/ConsultaOrdenesPub/_detalleCompra?N_OrdenCompra={id}` | detalle HTML de compra | publico |
| GET | `/ConsultaOrdenesPub/_detalleEstadoOrden?ID_OrdenCompra={id}` | historial de estados | publico |
| POST | `/ConsultaOrdenesPub/consultaDetalleOrdenServicio` | detalle de servicio | publico |
| GET | `https://apps1.perucompras.gob.pe/OrdenCompra/obtenerPdfOrdenPublico` | PDF de orden | publico |

El acceso general `/AccesoGeneral` usa reCAPTCHA para login. Ese flujo no se necesita para descargar las ordenes publicas.

La tabla usa cuerpos `text/plain` posicionales:

```text
orden^agreement_id^catalog_id^entity_internal_id^supplier_internal_id^fecha_inicio^fecha_fin^tipo
```

Ejemplo para bienes de mobiliario:

```text
^414^^^^2026-09-01^2026-09-23^BIENES
```

La respuesta propietaria usa delimitadores `¯`, `¬` y `^`. `consultaOrdenesDetallado` agrega catalogo, categoria, ficha-producto, marca, numero de parte, cantidades, precio unitario, flete, lugar y plazo de entrega. No pagina en servidor, por lo que debe consultarse con ventanas pequenas.

## Descarga OCDS recomendada

```http
GET /ConsultaOrdenesPub/DescargaJsonOCDS
  ?pAcuerdo=249
  &pEntidad=
  &pProveedor=
  &pFechaIni=20260901
  &pFechaFin=20260923
```

El 23 de septiembre de 2026 esta consulta devolvio:

```yaml
http_status: 200
content_type: application/octet-stream
content_disposition: attachment; filename=DatosAbiertos.json
size_bytes: 791479
ocds_version: "1.1"
```

El acuerdo `249` corresponde a `EXT-CE-2022-5`, computadoras de escritorio, portatiles y escaneres. Se probaron fechas `YYYYMMDD`; la UI vigente tambien construye solicitudes con `YYYY-MM-DD`.

La respuesta es un release package OCDS con `releases`. El primer registro de control incluyo:

```yaml
id: OCAM-2026-500134-64-0 + timestamp de release
ocid: ocds-luoe2u-OCAM-2026-500134-64-0
tag: [award, contract]
```

El adaptador debe tolerar texto legado y caracteres de control no escapados: una descarga de control no fue aceptada directamente por `jq`, aunque el contenido era un paquete JSON legible. Sanitizar solo los caracteres de control invalidos, conservar el archivo original y registrar el hash antes de parsear.

El JSON OCDS funciona para acuerdos de `BIENES`. Para el acuerdo de servicios `151` la descarga devolvio `500` y la UI deshabilita ese boton. Los servicios requieren el flujo propietario `consultaOrdenes`/`consultaDetalleOrdenServicio`/`consultaEstadosOrdenServicio`.

## Filtros

La UI publica permite filtrar por:

- numero de orden electronica;
- acuerdo marco;
- catalogo;
- entidad;
- proveedor;
- fecha inicial y final;
- reporte resumido o detallado.

`obtenerFiltros` devuelve acuerdos vigentes y no vigentes como texto delimitado. Ejemplos vigentes observados:

- `414-BIENES`: `EXT-CE-2025-11`, mobiliario;
- `324-BIENES`: `EXT-CE-2024-3`, limpieza y papeles;
- `249-BIENES`: `EXT-CE-2022-5`, computadoras;
- `151-SERVICIOS`: `IM-CE-2020-15`, boletos aereos.

No hardcodear esta lista: refrescar el catalogo y guardar vigencia/version.

## Identificadores y deduplicacion

| Dato | Uso canonico |
| --- | --- |
| `ocid` | ID canonico del proceso CEAM |
| `tender.id` / numero OCAM | clave de negocio visible |
| `release.id` | version/evento OCDS |
| RUC de entidad y proveedor | resolucion de organizaciones |
| ID interno de orden | descarga de PDF y detalle |
| SHA-256 | deduplicacion de documentos |

No crear una oportunidad abierta por cada orden: estas publicaciones representan principalmente `award` y `contract`. Si la misma orden aparece en SEACE/OECE, relacionar por numero OCAM/orden, RUC, fecha, monto y OCID; mantener la procedencia por campo.

## Politica del worker

- Usar `DescargaJsonOCDS` como canal principal, no scraping de la tabla.
- Particionar por acuerdo y ventanas de fecha acotadas; usar solapamiento de uno o dos dias y dedupe por `release.id`/`ocid`.
- Conservar cada release como evento append-only.
- Descargar PDF solo bajo demanda o cuando cambie su hash.
- Actualizar catalogos/acuerdos diariamente y ordenes varias veces al dia segun volumen.
- Poblar inteligencia de mercado, precios, compradores y proveedores; no marcar estas ordenes como `open_for_bids`.
- No almacenar ni indexar `Usuario Creacion OC`, DNI, IP, cuentas, CCI, pasajeros, telefonos o correos personales presentes en exportaciones, servicios o PDFs.

Para backfill usar el ID del acuerdo que estaba vigente en el periodo consultado. Las descargas masivas publicadas por `/getListaDescargaMasiva` terminan en noviembre de 2023 y sirven solo como archivo antiguo, no como feed actual.
