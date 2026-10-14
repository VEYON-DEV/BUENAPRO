# Plan integral de ingesta universal de oportunidades

## Objetivo

Convertir BuenaPro en un radar universal, diario y trazable de oportunidades comerciales. El alcance final incluye:

- contrataciones publicas peruanas de bienes, servicios, consultorias y obras
- contratos menores, acuerdos marco, planes de compra, adjudicaciones y contratos
- empresas estatales y regimenes especiales
- procesos financiados por organismos internacionales
- oportunidades privadas publicamente descubribles
- invitaciones privadas recibidas por cada empresa
- historico versionado de publicaciones, cambios, resultados y documentos

Este documento describe el estado objetivo completo. Las etapas indican dependencias de construccion; no representan una reduccion del alcance.

## Resultado esperado

BuenaPro debe poder responder, con evidencia:

1. Que oportunidades nuevas aparecieron hoy.
2. Que se compra y bajo que modalidad.
3. Quien puede participar y por que canal.
4. Que documentos y requisitos se publicaron.
5. Que cambio desde la publicacion anterior.
6. Quien gano, por cuanto y en que fecha.
7. Que tan confiable, completa y reciente es la informacion.
8. Que oportunidades encajan con el perfil de cada empresa.

La promesa del producto no sera afirmar que existe cobertura absoluta de procesos que nunca se publican. La promesa verificable sera mostrar las fuentes cubiertas, su frecuencia, su ultima revision exitosa y sus limitaciones conocidas.

## Principios obligatorios

1. PostgreSQL es la fuente de verdad de los datos normalizados.
2. Cada fuente conserva su identificador, estado y payload originales.
3. Una oportunidad tiene un UUID interno independiente de cualquier portal.
4. Las observaciones son append-only: nunca se destruye una version anterior.
5. La fuente oficial tiene prioridad, pero la procedencia se conserva por campo.
6. Los hechos, las inferencias y las recomendaciones deben permanecer separados.
7. La ingesta captura todo el mercado permitido; el matching decide que mostrar a cada empresa.
8. Las invitaciones privadas son tenant-scoped y nunca se incorporan al indice compartido sin autorizacion explicita.
9. No se evaden CAPTCHA, MFA, paywalls, bloqueos, limites ni controles anti-bot.
10. No se guardan credenciales en el repositorio ni en las guias de fuentes.
11. Antes de habilitar una fuente se revisan terminos, licencia, robots, privacidad y retencion.
12. LLM para leer y estructurar; reglas para estados, comparacion, deduplicacion y elegibilidad verificable.

## Estrategia selectiva por rubro

BuenaPro no ejecutara un backfill profundo de todos los rubros de todas las
fuentes. La cobertura universal significa poder descubrir y clasificar fuentes,
no descargar cada documento ni procesar con IA cientos de miles de registros que
no corresponden al perfil de la empresa.

La ingesta se divide en tres niveles:

1. **Descubrimiento liviano.** Guardar ID de fuente, titulo, comprador, objeto,
   fechas, estado, ubicacion, clasificaciones disponibles y URL. No descargar
   documentos ni llamar al LLM.
2. **Prefiltro deterministico local.** Comparar el registro con los segmentos
   CUBSO, tipos de objeto, palabras clave positivas/negativas, entidades y
   ubicaciones configuradas para las lineas de negocio. El resultado conserva
   la razon de inclusion o descarte.
3. **Hidratacion profunda.** Solo para registros relevantes, guardados por el
   usuario o solicitados manualmente: consultar detalle, descargar documentos,
   extraer requisitos, reconciliar otras fuentes y calcular afinidad.

El historico profundo se limita inicialmente a los ultimos tres anos y a los
rubros configurados. El historico ya almacenado se conserva; no se elimina por
un cambio de alcance. Los registros fuera del rubro pueden mantenerse solo como
indice liviano cuando su captura sea barata y este permitida.

### Politica cuando la fuente no ofrece filtro CUBSO

El adaptador aplicara, en este orden, el filtro mas barato disponible:

1. objeto nativo: bien, servicio, obra o consultoria;
2. rango de fechas o particion mensual;
3. categoria, acuerdo, entidad, departamento o texto ofrecido por la fuente;
4. clasificacion de items incluida en el listado o payload;
5. reglas locales sobre titulo y descripcion;
6. detalle o documento solo si las reglas anteriores dejan el registro como
   relevante o incierto.

Un registro incierto no se envia automaticamente al LLM. Queda en una cola de
muestreo o revision con un presupuesto diario. Los falsos positivos y negativos
confirmados alimentan las reglas versionadas, no un prompt opaco.

### Estrategia por las fuentes ya verificadas

| Fuente | Descubrimiento recomendado | Filtro de rubro | Profundizacion |
| --- | --- | --- | --- |
| PROD6 Contratos Menores | Consulta por estado y objeto | CUBSO en servidor | Detalle, TDR y lifecycle solo para segmentos habilitados |
| PROD4 Oportunidades | Solo oportunidades vigentes | `/listaProcesosCubso/codigoSegmento/{segmento}`; objeto como respaldo | Ficha, cronograma y bases de coincidencias relevantes |
| OECE OCDS | Particiones por fecha y categoria | `mainProcurementCategory`, items OCDS y reglas locales | Historico de tres anos solo para rubros habilitados; adjudicaciones y contratos sin LLM |
| PAC3 | Procesos futuros por objeto, ano y descripcion | Filtros JSF mas reglas locales | Items y versiones solo para senales relevantes; no descargar todo el PAC |
| PROD2 | No usar como crawler masivo | Buscar por ID/nomenclatura ya descubierta | Fallback puntual cuando OCDS/PROD4 no tengan cronograma o documentos |
| SUNARP 8 UIT | Recorrer todas las zonas activas; volumen pequeno | Tipo y titulo local | PDF solo para coincidencias o registros inciertos priorizados |
| JNE PLACOBS | Listado vigente si el WAF lo permite | Estado, titulo y descripcion local | TDR/resultado solo para coincidencias; abrir circuito ante bloqueo |
| Peru Compras Ordenes | Ventanas de fecha por acuerdo | Acuerdos/catalogos relacionados con el rubro | Historico de precios y proveedores, sin crear oportunidades abiertas |
| Peru Compras Catalogos | Consulta bajo demanda | Producto/acuerdo referenciado por una orden relevante | Ficha tecnica solo cuando tenga una relacion concreta |

### Orden resumido de implementacion

1. Crear el modelo universal de fuentes, observaciones, oportunidades,
   clasificaciones y checkpoints.
2. Crear un `RelevancePolicy` versionado por perfil con CUBSO, objetos, keywords,
   exclusiones y presupuesto de registros inciertos.
3. Migrar PROD6 al adaptador comun y habilitar Servicios y Bienes sin perder la
   ingesta actual.
4. Integrar PROD4 como radar principal de oportunidades vigentes y reconciliar
   por ID/nomenclatura con PROD6 y OCDS.
5. Integrar OCDS como historico selectivo de tres anos, primero solo metadatos,
   items, adjudicaciones y contratos de los rubros habilitados.
6. Integrar SUNARP y luego JNE como fuentes institucionales de vigentes y TDR.
7. Integrar PAC3 como senal futura; no mezclar una fila planificada con una
   convocatoria abierta.
8. Mantener PROD2 como fallback puntual, no como fuente de barrido diario.
9. Integrar Peru Compras como inteligencia de precios/proveedores por acuerdos
   seleccionados y resolver fichas de catalogo bajo demanda.

### Limites operativos iniciales

- conservar el historico PROD6 ya extraido;
- backfill nuevo: maximo tres anos y solo rubros configurados;
- documentos: descargar unicamente registros relevantes, guardados o inciertos
  dentro del cupo diario;
- LLM: nunca durante descubrimiento masivo; solo despues del prefiltro;
- cada adaptador debe tener checkpoint, rate limit, presupuesto diario, metrica
  de descartados y una muestra auditable de descartes;
- el feed operativo exige fecha vigente y verificacion reciente, aunque el
  estado nativo de la fuente todavia diga `Vigente`.

Esta politica permite ampliar fuentes sin que el costo crezca en proporcion al
universo completo de contrataciones.

## Arquitectura objetivo

```text
API / feed / CSV / HTML / PDF / correo / carga autorizada
                            |
                            v
                    source registry
                            |
                            v
                source adapter + checkpoint
                            |
                            v
              immutable source observations
                            |
                            v
             normalization + entity resolution
                            |
                            v
        canonical opportunities + lots + documents
                            |
                            v
          lifecycle + dedupe + awards + contracts
                            |
                            v
       extraction + matching + alerts + applications
```

## Modelo universal

### Registro y operacion de fuentes

```text
sources
source_accounts
source_capabilities
source_partitions
source_checkpoints
ingestion_runs
source_observations
source_failures
```

`sources` debe registrar nombre, propietario, pais, URLs, tipo de acceso, autenticacion, adaptador, capacidades, frecuencia, rate limit, terminos, licencia, permisos, salud y ultima ejecucion exitosa.

### Organizaciones y oportunidades

```text
organizations
organization_identifiers
organization_aliases
organization_roles

opportunities
opportunity_sources
opportunity_releases
opportunity_events
opportunity_deadlines
opportunity_lots
opportunity_items
opportunity_classifications
```

RUC es importante en Peru, pero no puede ser obligatorio globalmente. La identidad requerida es:

```text
opportunities.id = UUID interno BuenaPro
opportunity_sources UNIQUE (source_id, external_id)
```

Una oportunidad puede tener multiples registros de fuente. Una publicacion institucional y su copia en un agregador no deben crear dos oportunidades comerciales distintas.

### Documentos, resultados y taxonomias

```text
opportunity_documents
document_versions
document_extractions
participants
awards
award_suppliers
contracts

taxonomy_schemes
taxonomy_terms
taxonomy_mappings
```

El pipeline documental debe soportar PDF nativo y escaneado, DOCX, XLSX, PPTX, HTML, imagenes, ZIP y RAR. Toda conversion se ejecuta de forma aislada, con deteccion MIME real, limites de tamano, antivirus, macros deshabilitadas y hash SHA-256.

Se deben poder mapear CUBSO, UNSPSC, CPV, categorias privadas y etiquetas internas sin convertir ninguna taxonomia de una fuente en el modelo global.

## Vocabulario canonico inicial

```text
object_type:
  GOOD | SERVICE | GENERAL_CONSULTING | WORK | WORKS_CONSULTING
  INVESTMENT | GRANT | SUBCONTRACT

procedure_type:
  PUBLIC_TENDER | PUBLIC_COMPETITION | RFI | RFQ | RFP
  REVERSE_AUCTION | PRICE_COMPARISON | DIRECT_PURCHASE
  FRAMEWORK_AGREEMENT | PRIVATE_INVITATION | CONCESSION

stage:
  PLANNED | MARKET_RESEARCH | OPEN | EVALUATION
  AWARDED | CONTRACTED | CANCELLED | CLOSED

participation_access:
  PUBLIC | REGISTRATION_REQUIRED | HOMOLOGATION_REQUIRED
  CATALOG_MEMBER | INVITATION_ONLY | TENANT_PRIVATE
```

Siempre se conserva el valor nativo junto al canonico.

## Contrato comun de adaptadores

Cada fuente implementara solo las capacidades que realmente ofrece:

```python
class SourceAdapter:
    def discover(self, checkpoint): ...
    def fetch_detail(self, external_id): ...
    def list_documents(self, external_id): ...
    def download_document(self, document_id): ...
    def fetch_events(self, external_id): ...
    def fetch_awards(self, external_id): ...
    def healthcheck(self): ...
```

Capacidades declarables:

```text
DISCOVER | DETAIL | DOCUMENTS | AMENDMENTS | QUESTIONS
AWARDS | CONTRACTS | PLANS | SUBMISSION_LINK | HISTORICAL_BACKFILL
```

## Proceso obligatorio por plataforma

Cada fuente pasa por el siguiente ciclo antes de activarse en produccion:

1. **Identificar la fuente oficial.** Confirmar propietario, dominio y finalidad.
2. **Revisar acceso y permisos.** Documentar terminos, licencia, robots, autenticacion y restricciones.
3. **Descubrir la superficie tecnica.** Enumerar endpoints, feeds, descargas, paginas y formularios relevantes.
4. **Probar de forma read-only.** Ejecutar solicitudes seguras sin postular, modificar ni crear datos.
5. **Capturar fixtures.** Guardar ejemplos sanitizados de busqueda, detalle, documentos, errores y estados.
6. **Documentar el contrato.** Parametros, paginacion, IDs, fechas, monedas, estados, errores y rate limits.
7. **Definir el mapeo canonico.** Objeto, procedimiento, etapa, acceso, comprador, fechas, monto y ubicacion.
8. **Implementar el adaptador.** Descubrimiento, detalle, documentos, lifecycle y resultados disponibles.
9. **Agregar contract y golden tests.** Validar schema, paginacion, idempotencia y cambios conocidos.
10. **Ejecutar backfill.** Recorrer todo el rango historico permitido con checkpoint reanudable.
11. **Reconciliar cobertura.** Comparar conteos del publicador con registros capturados.
12. **Activar polling y observabilidad.** Frecuencia, alertas, circuit breaker y tablero de salud.
13. **Mantener la guia.** Actualizarla cuando cambie un endpoint, schema, termino o mecanismo de acceso.

## Pruebas minimas por endpoint

- resolucion DNS, TLS y redirecciones
- respuesta valida y tipo de contenido real
- autenticacion y expiracion cuando aplique
- primera, siguiente y ultima pagina
- pagina vacia y parametros invalidos
- limites de fecha y rango historico
- estabilidad del identificador externo
- detalle existente e inexistente
- listado y descarga de documentos
- cambios de estado y cronograma
- adjudicacion o cierre, cuando exista
- idempotencia al repetir la captura
- comportamiento ante `429`, `401`, `403`, `404` y `5xx`
- `ETag`, `Last-Modified`, `Retry-After` y cache cuando existan

Las pruebas no deben generar ofertas, cotizaciones, registros de proveedor ni otras mutaciones externas.

## Guia obligatoria por fuente

Cada plataforma tendra un archivo en `docs/sources/NNN-slug.md` creado a partir de `docs/sources/_TEMPLATE.md`. Una guia no se considera verificada si no incluye:

- fecha y ambiente de verificacion
- URLs oficiales y alcance
- requisitos legales y tecnicos
- endpoints probados y ejemplos sanitizados
- autenticacion sin secretos
- paginacion, checkpoints e IDs
- documentos, estados y lifecycle
- rango historico y rate limits
- mapeo al modelo canonico
- evidencia de pruebas y limitaciones
- estado de implementacion y proxima accion

## Fases de plataformas

El inventario canonico, detallado y numerado de todas las fuentes conocidas vive en [`sources/README.md`](./sources/README.md). La lista siguiente resume los paquetes de trabajo; el registro maestro define el ID, la guia prevista, el acceso y el estado de cada plataforma.

### A. Nucleo publico peruano

1. **SEACE/OECE Contratos Menores (`prod6`)**: fuente patron ya consumida por BuenaPro; validara el nuevo adaptador sin cambiar resultados.
2. **OECE OCDS/API**: datos estructurados y releases para procedimientos, etapas e historico.
3. **SEACE Buscador Publico (`prod2`)**: procedimientos generales, expresiones, requerimientos y ordenes.
4. **SEACE Oportunidades (`prod4/openegocio`)**: oportunidades vigentes.
5. **Plan Anual de Contrataciones (PAC)**: demanda futura y versiones.
6. **Buscador Publico de Contratos (`prod4/contratos`)**: ejecucion contractual.
7. **Datos Abiertos OECE**: backfill de convocatorias, resultados, proveedores, contratos y ordenes.
8. **Peru Compras**: catalogos, proveedores, productos y ordenes publicas.
9. **Cotizador OECE**: indagaciones de mercado con acceso autorizado.
10. **FUP, proveedores adjudicados y CUBSO**: enriquecimiento y taxonomia.

### B. Contratos menores institucionales

11. JNE PLACOBS.
12. Poder Judicial.
13. SUNARP.
14. Lima Compra.
15. Provias Nacional.
16. SBN.
17. MINSA.
18. SENAMHI.
19. SANIPES.
20. OTASS.
21. CENARES.
22. Hospital de Ventanilla.
23. ENOSA.
24. Provias Descentralizado.
25. PNSR y PNSU.
26. Gobiernos regionales y universidades con portales propios.
27. Paginas, compendios y archivos Gob.pe sin plataforma estructurada.

### C. Regimenes especiales y empresas estatales

28. Petroperu: portal publico, Suplos y PAAC.
29. ProInversion: concursos, expresiones, APP y Obras por Impuestos.
30. PERUPETRO.
31. Compras a MYPEru y nucleos ejecutores.
32. Wasi Mikuna.
33. Banco de la Nacion y EsSalud.
34. Empresas FONAFE priorizadas por volumen y procesos ausentes de SEACE.

### D. Organismos internacionales

35. Banco Mundial: API, avisos, planes y adjudicaciones.
36. BID: notices, plans, awards y open data.
37. UNGM.
38. PNUD Procurement Notices y Quantum autorizado.
39. OEI Peru.
40. CAF.
41. Union Europea, AFD y GIZ.
42. Unidades ejecutoras peruanas con BID, BIRF, JICA, KfW o AFD.

### E. Privados publicamente descubribles

43. eBIZ B2M y Subastas.pe.
44. SAP Business Network Discovery.
45. ConnectAmericas.
46. Suplos.
47. Licify.
48. Procu.
49. Mercado Eletronico.
50. iConstruye.
51. Contacto Latam y Sistad, condicionados a actividad y permisos verificados.

### F. Privados autenticados o por invitacion

52. Buzon privado de BuenaPro e integracion autorizada con Gmail/Outlook.
53. Coupa Supplier Portal.
54. Oracle Supplier Portal.
55. JAGGAER Supplier Network.
56. Ivalua.
57. GEP SMART.
58. Achilles y SICEP.
59. Portales particulares de compradores priorizados por los usuarios.
60. Carga manual y captura mediante extension autorizada.

### G. Inventario propio

61. Portal para que compradores publiquen RFQ, RFP, RFI, subastas y busquedas de proveedores.
62. API, webhook, SFTP y CSV para compradores asociados.
63. Red de homologacion y descubrimiento de proveedores.

## Frecuencias objetivo

| Tipo de fuente | Frecuencia |
| --- | --- |
| API de oportunidades vigentes | 15-30 minutos |
| Correo o webhook privado | tiempo real |
| Lifecycle de procesos abiertos | 1-2 horas |
| Pagina institucional publica | 2-4 veces al dia |
| PAC y planes de compra | diario |
| Adjudicaciones y contratos | diario |
| Dataset historico | segun publicador |
| Backfill | continuo hasta completar |

La frecuencia efectiva siempre respeta terminos, rate limits, `Retry-After`, `ETag` y `Last-Modified`.

## Deduplicacion

Orden de identificacion:

1. OCID o identificador nativo estable.
2. RUC/comprador + codigo de proceso + anio.
3. Comprador + lote + convocatoria.
4. URL canonica y hash documental.
5. Similitud bloqueada por pais, comprador, fechas, monto y categoria.

```text
EXACT_MATCH       -> fusion automatica
HIGH_CONFIDENCE   -> fusion conservando fuentes
MEDIUM_CONFIDENCE -> revision humana
LOW_CONFIDENCE    -> mantener separado
```

No se deben confundir lotes, modificaciones, nuevas convocatorias, agregadores, adjudicaciones ni contratos derivados.

## Migracion del modelo SEACE actual

1. Crear el registro de fuentes y las tablas canonicas.
2. Registrar Contratos Menores SEACE como primera fuente.
3. Hacer backfill `seace_contracts -> opportunities`.
4. Crear el mapa `id_contrato -> opportunity_id`.
5. Agregar `opportunity_id` a documentos, facets, matches, guardados, borradores y chats.
6. Ejecutar dual-write y comparar resultados.
7. Cambiar APIs y rutas al UUID canonico.
8. Mantener `seace_contracts` como staging y compatibilidad durante la transicion.
9. Retirar dependencias directas solo despues de reconciliar feed, matching, documentos, alertas y seguimiento.

## Calidad y observabilidad

Metricas obligatorias por fuente:

- ultima ejecucion exitosa y retraso respecto del publicador
- registros esperados y recibidos
- nuevas oportunidades y modificaciones
- errores HTTP y de parsing
- completitud de campos y documentos
- duplicados y fusiones revertidas
- enlaces rotos y cambios de schema

Cada adaptador debe tener contract tests, fixtures, golden cases, prueba de idempotencia, reconciliacion y alerta de cambio de contrato.

## Definicion de completado por fuente

Una fuente esta completa solo cuando:

- su guia fue verificada y fechada
- acceso y permisos estan documentados
- endpoints y capacidades fueron probados
- fixtures y contract tests existen
- el adaptador es idempotente
- busqueda, detalle, documentos y lifecycle disponibles funcionan
- el backfill permitido termino o tiene checkpoint reanudable
- conteos fueron reconciliados
- procedencia y evidencias llegan a la API/UI
- polling y alertas tecnicas estan activos
- el checkbox correspondiente fue actualizado en `tasks/`

## Primera accion

La primera fuente sera **SEACE/OECE Contratos Menores (`prod6`)**. Se documentaran los endpoints que BuenaPro ya consume, se repetiran pruebas read-only, se capturaran fixtures y se usara como contrato de referencia para crear el SDK comun de adaptadores. La segunda fuente sera **OECE OCDS/API**, que validara que el modelo canonico soporte procedimientos mas amplios y releases historicos.
