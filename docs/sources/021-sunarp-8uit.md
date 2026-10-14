# 021 - SUNARP 8 UIT

## Estado

```yaml
status: VERIFICADA
owner: Superintendencia Nacional de los Registros Publicos
country: PE
last_verified_at: 2026-09-23
adapter_key: sunarp_8uit_html
implementation: pendiente
```

## Alcance

El portal publica requerimientos activos de bienes y servicios menores o iguales a 8 UIT para la sede central y las zonas registrales de SUNARP. Permite descubrir la oportunidad, descargar sus TDR/EETT y postular con una cuenta de proveedor.

Es una fuente temprana complementaria a PROD6: varias oportunidades vigentes observadas el 23 de septiembre de 2026 no aparecieron al buscar sus frases en Contratos Menores SEACE. Posteriormente la orden o contrato puede registrarse en SEACE y debe vincularse como otra etapa del mismo ciclo de compra.

## URLs oficiales

| Uso | URL | Verificada |
| --- | --- | --- |
| Portal | `https://8uit.sunarp.gob.pe/portal` | si |
| Zona registral | `https://8uit.sunarp.gob.pe/portal/zona-registral/{slug}` | si |
| Documento | `https://8uit.sunarp.gob.pe/storage/files2/{archivo}.pdf` | si |
| Manual de proveedor | `https://www.sunarp.gob.pe/pdfs/8UIT/MANUAL-DE-PROVEEDOR-DE-8UIT-SUNARP.pdf` | si |
| Robots | `https://8uit.sunarp.gob.pe/robots.txt` | si; `Disallow` vacio |

## Acceso y permisos

```yaml
access_mode: public para lectura; registered para postular
authentication: none para HTML/PDF; session+CSRF+2FA para participar
index_allowed: pendiente de revision legal
store_metadata_allowed: pendiente de revision legal
store_documents_allowed: pendiente de revision legal
redistribute_allowed: pendiente de revision legal
ai_processing_allowed: pendiente de revision legal
```

No se encontro CAPTCHA en el listado publico. La lectura no requirio login ni cookies persistentes. El portal no muestra en su navegacion terminos o licencia de reutilizacion; usar frecuencia conservadora y enlazar el documento oficial.

## Superficie publica probada

| Metodo | Endpoint | Proposito | Resultado |
| --- | --- | --- | --- |
| GET | `/portal` | descubrir las 15 zonas registrales | 200 HTML |
| GET | `/portal/zona-registral/{slug}` | requerimientos activos de una zona | 200 HTML |
| GET | `/storage/files2/{archivo}.pdf` | TDR/EETT | 200 PDF con ETag, Last-Modified y Accept-Ranges |

No se encontro una API JSON de oportunidades. El HTML server-rendered contiene todas las tarjetas activas de la zona. `?page=2` devolvio el mismo contenido y no existe un paginador real.

Campos disponibles por tarjeta:

- ID interno en `data-convocatoria`;
- tipo `BIENES` o `SERVICIOS`;
- titulo;
- zona registral;
- fecha y hora de publicacion;
- fecha y hora de vencimiento;
- plazo y contador visible;
- URL del PDF;
- disponibilidad de la accion `Postular`.

No publica monto estimado, ganador, adjudicacion, contrato ni historial. El PDF contiene el detalle funcional y puede incluir requisitos, experiencia, plazo, lugar, pagos, penalidades y datos de contacto.

## Ejemplo de control

```yaml
external_id: 894607
object: SERVICIOS
buyer_unit: Sede Central
published_at: 2026-09-21T15:13:00-05:00
deadline_at: 2026-09-25T23:59:00-05:00
document_type: TDR
document_pages: 8
```

En el snapshot se observaron 16 tarjetas activas distribuidas en 9 de 15 zonas; 6 zonas no tenian publicaciones. El volumen es pequeno, pero las ventanas pueden ser de uno a seis dias.

## Endpoints de participacion no utilizables por el worker

| Metodo | Endpoint | Uso |
| --- | --- | --- |
| POST | `/portal/crear-proveedor` | registro con RUC, RNP y contacto |
| POST | `/portal/login` | inicio de sesion |
| POST | `/portal/verify-login-code` | segundo factor por correo |
| POST | `/portal/crear-convocatoria` | postulacion con `convocatoriaID` |

Son endpoints mutantes con sesion/CSRF. El adaptador de ingesta no debe invocarlos.

## Identificadores y lifecycle

La clave fuente recomendada es:

```text
sunarp_8uit:{data-convocatoria}
```

Persistir:

- `zone_slug`;
- `first_seen_at` y `last_seen_at`;
- `published_at` y `deadline_at` en `America/Lima`;
- `source_status=active|disappeared`;
- `pdf_url`, `pdf_sha256` y hash del snapshot HTML;
- tipo y titulo nativos.

Como el portal solo muestra activos, la desaparicion debe generar un evento `disappeared` o `closed_unverified`, nunca borrar el registro. El nombre del PDF y su timestamp sirven como version documental, no como ID de la oportunidad.

## Dedupe con PROD6/SEACE

No deduplicar por titulo. Mantener inicialmente la publicacion SUNARP y la futura orden/contrato SEACE como `source_record` distintos. Vincularlos usando:

1. entidad o zona y RUC normalizados;
2. numero de requerimiento extraido del PDF, si existe;
3. tipo de objeto;
4. descripcion normalizada;
5. fechas y monto cuando aparezcan en SEACE;
6. coincidencia de documentos por hash cuando aplique.

La relacion adecuada puede ser `same_procurement` o `lifecycle_successor`; no siempre es un duplicado exacto porque una fuente representa la oportunidad y la otra el resultado contractual.

## Politica del worker

- Descubrir slugs desde `/portal` y recorrer las 15 zonas con HTTP normal.
- Polling cada 30-60 minutos; una ejecucion diaria puede perder convocatorias breves.
- Concurrencia maxima recomendada: 2, con timeout, backoff, jitter y requests condicionales.
- Descargar el PDF solo si la URL, ETag o Last-Modified cambia.
- Extraer texto/OCR sin indexar DNI, firmas, correos o telefonos personales innecesarios.
- Enriquecer despues con PROD6/OCDS para monto, estado, ganador y orden.
