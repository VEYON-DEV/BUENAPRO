# 010 - Peru Compras: Buscador de Catalogos CEAM

## Estado

```yaml
status: VERIFICADA
owner: Central de Compras Publicas - Peru Compras
country: PE
last_verified_at: 2026-09-23
adapter_key: peru_compras_catalogs
implementation: pendiente
```

## Alcance

El buscador publico permite consultar las fichas-producto disponibles en los Catalogos Electronicos de Acuerdos Marco. Aporta taxonomia comercial, especificaciones, marca, modelo, numero de parte, imagen y ficha tecnica.

No representa demanda ni una oportunidad de compra. Debe ser un catalogo maestro de productos enlazado a las lineas de las ordenes CEAM.

## URL oficial

```text
https://buscadorcatalogos.perucompras.gob.pe/
```

## Acceso

```yaml
access_mode: public
authentication: none
session: cookie + antiforgery token
captcha: none observado
index_allowed: pendiente de revision legal
store_metadata_allowed: pendiente de revision legal
store_documents_allowed: pendiente de revision legal
redistribute_allowed: pendiente de revision legal
ai_processing_allowed: pendiente de revision legal
```

La consulta usa sesion y token antiforgery, pero no exige cuenta. El worker debe iniciar con un `GET`, conservar cookies y extraer el token antes de cada secuencia de busqueda.

## Endpoints identificados

| Metodo | Endpoint | Proposito |
| --- | --- | --- |
| GET | `/` | iniciar sesion y obtener antiforgery |
| POST | `/` | iniciar busqueda |
| POST | `/Search/Search` | filtros y paginacion |
| POST | `/Search/SearchByAgreementStatus` | filtrar acuerdos vigentes/no vigentes |
| POST | `/Search/Download` | exportacion |

`/Search/Search` usa `multipart/form-data`. Campos observados:

- `SearchText`;
- `Status`;
- `Pagination.Page`, `Pagination.Paging`, `Pagination.LeftMostPage`;
- `ClientFilter.Agreement`, `Catalogue`, `Category`, `Feature`, `Department`;
- campos equivalentes `ServerFilter.*`.

Las colecciones de filtros se envian como arrays JSON dentro del formulario.

## Datos disponibles

- ID de producto;
- acuerdo marco;
- catalogo y categoria;
- descripcion normalizada;
- marca, modelo y numero de parte;
- caracteristicas;
- fecha de publicacion y ultimo estado;
- estado de la ficha, por ejemplo `OFERTADA`;
- imagen;
- PDF tecnico servido desde CDN.

La prueba sobre computadoras mostro 16 894 fichas y paginacion de 12 por pagina. Un ID de control observado fue `2128268`.

## Identidad y versionado

Clave recomendada:

```text
peru_compras_product:{agreement_id}:{product_id}
```

El mismo producto puede cambiar de estado o ficha dentro de otro acuerdo. Conservar snapshots append-only, URL del PDF, ETag/Last-Modified cuando existan y SHA-256 del documento.

## Relacion con oportunidades

- No crear registros `opportunity` desde este buscador.
- Enlazar la ficha con los items de `peru_compras_orders_ocds` por ID, acuerdo, catalogo y numero de parte.
- Usar la taxonomia y especificaciones para enriquecer busqueda, precios comparables y perfiles de proveedor.
- Mantener separado el estado de la ficha del estado de una orden o licitacion.

## Politica del worker

- Refrescar acuerdos y categorias diariamente.
- Refrescar fichas activas por delta o por particiones; un backfill completo requiere checkpoints.
- Concurrencia baja y sesion reutilizada.
- Descargar ficha tecnica solo si cambia la version o el hash.
- No interpretar la existencia de una ficha como intencion de compra.
