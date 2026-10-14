# 019 - JNE PLACOBS

## Estado

```yaml
status: VERIFICADA_CON_BLOQUEO_WAF
owner: Jurado Nacional de Elecciones
country: PE
last_verified_at: 2026-09-23
adapter_key: jne_placobs
implementation: pendiente
```

## Alcance

PLACOBS publica contrataciones de bienes y servicios del JNE, incluyendo procesos vinculados a los procesos electorales. La superficie publica permite buscar expedientes, consultar sus etapas y descargar TDR/EETT, TDR integrado y resultados.

Existe duplicacion parcial con SEACE/OCDS. PLACOBS debe integrarse como fuente operativa y de documentos, conservando el OCID de SEACE/OCDS como identidad canonica cuando exista.

## URLs oficiales

| Uso | URL | Verificada |
| --- | --- | --- |
| Portal | `https://web.jne.gob.pe/contratacionesbs/contratacion` | si |
| Lineamientos 2026 | `https://web.jne.gob.pe/contratacionesbs/assets/anexos/2026/LINEAMIENTOS%20PARA%20BIENES%20Y%20SERVICIOS%20EXONERADOS%20APROBADO.pdf` | si |

## Acceso y bloqueo observado

```yaml
access_mode: public para listado y documentos; registered para participar
authentication: none para lectura; session para consultas y propuestas
index_allowed: pendiente de revision legal
store_metadata_allowed: pendiente de revision legal
store_documents_allowed: pendiente de revision legal
redistribute_allowed: pendiente de revision legal
ai_processing_allowed: pendiente de revision legal
```

El host completo esta protegido por Imperva/Incapsula. El 23 de septiembre de 2026, el portal y los endpoints `/api/*` respondieron desde esta red con HTTP `200`, pero `Content-Type: text/html` y una pagina de incidente que contenia `/_Incapsula_Resource`. El worker debe validar contenido, no solo el codigo HTTP.

No se observo reCAPTCHA en la busqueda publica. El CAPTCHA alfanumerico de seis caracteres pertenece al login y registro, no al endpoint de busqueda. No se debe automatizar ni resolver ese CAPTCHA para la ingesta.

## Endpoints identificados

Los contratos se confirmaron desde el bundle Angular oficial de la aplicacion.

| Metodo | Endpoint | Proposito | Auth | Resultado |
| --- | --- | --- | --- | --- |
| POST | `/contratacionesbs/api/search` | listado y filtros | publica en diseno | bloqueado por WAF desde esta red |
| GET | `/contratacionesbs/api/estado` | catalogo dinamico de estados | publica en diseno | bloqueado por WAF desde esta red |
| POST | `/contratacionesbs/api/download/` | descarga de documentos | publica en diseno | bloqueado por WAF desde esta red |
| GET | `/contratacionesbs/api/captcha` | CAPTCHA de login/registro | publico | no usar para ingesta |

### Busqueda

```http
POST /contratacionesbs/api/search
Content-Type: application/json
```

```json
{
  "search": "",
  "idEstado": null,
  "idAnio": 2026,
  "star": 1,
  "end": 100
}
```

`star` y `end` son limites inclusivos basados en uno. La UI ofrece tamanos de 10, 15, 20, 50 y 100. El total se obtiene de `data[0].total`.

Campos consumidos por el frontend:

- `idExpediente`
- `numeroExpediente`
- `descripcion`
- `estado`
- `fechaPublicacion`
- `fechaAmpliacion`
- `fechaConsultaInicio` y `fechaConsultaFin`
- `isTdrIntegrado`, `isResultado`
- `isConsultar`, `isConsultar2`
- `isPostular`, `isPostular2`
- `total`

Los estados visibles incluyen `Vigente` y `Concluido`. Sus IDs deben obtenerse de `GET api/estado`; no deben quedar hardcodeados.

### Documentos

```http
POST /contratacionesbs/api/download/
Content-Type: application/json
```

```json
{
  "idExpediente": 123,
  "idTipoArchivo": 1
}
```

Tipos confirmados:

| `idTipoArchivo` | Documento |
| ---: | --- |
| 1 | TDR/EETT inicial |
| 4 | TDR/EETT integrado |
| 6 | resultados o Buena Pro |

La respuesta es binaria y el nombre se informa en el header `file-name`. La fila del listado y estos documentos conforman el detalle publico; no se encontro un endpoint JSON de detalle separado.

## Identificadores y deduplicacion

| Dato | Uso |
| --- | --- |
| `idExpediente` | `external_id` estable dentro de PLACOBS |
| `numeroExpediente` | clave de negocio y cruce con nomenclatura |
| OCID SEACE/OCDS | identidad canonica cuando exista |
| SHA-256 + tipo | deduplicacion y versionado documental |

Cruce comprobado: `RES-PROC-226-2026-JNE-1` aparece en OCDS como `ocds-dgv273-seacev3-1244618` y registra etapas presenciales en PLACOBS. Releases posteriores incorporan adjudicatario y documentos de oferta/Buena Pro.

Regla recomendada:

1. buscar coincidencia exacta por nomenclatura y entidad JNE (`RUC 20131378549`);
2. si existe, adjuntar PLACOBS como `source_record` y enriquecer documentos/ventanas;
3. si no existe, crear oportunidad provisional con `placobs:{idExpediente}`;
4. reconciliar posteriormente por numero de expediente, entidad, ano y fechas;
5. no fusionar automaticamente por descripcion solamente.

## Politica del adaptador

- OCDS/SEACE cubre el historico y es la primera fuente para reconciliacion.
- PLACOBS se consulta para vigentes, documentos faltantes y cambios operativos.
- Usar egress estable, sesion WAF persistente solo si el acceso publico lo permite y baja frecuencia.
- Si la respuesta es HTML, contiene `_Incapsula_Resource` o headers `x-iinfo`, registrar `blocked_by_waf` y abrir el circuit breaker.
- No reintentar en bucle y no evadir el WAF.
- Solicitar al JNE whitelist o canal institucional antes de escalar el volumen.
