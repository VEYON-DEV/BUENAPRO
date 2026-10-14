# 003 - SEACE Buscador Publico (`prod2`)

## Estado

```yaml
status: VERIFICADA
scope_verified: anuncios futuros, procedimientos, detalle, cronograma y documentos
owner: OECE
country: PE
last_verified_at: 2026-10-05
adapter_key: seace_public_search
implementation: cliente_http_acotado_verificado_laptop; ingesta_bd_pendiente
```

La investigación fue de lectura. No se realizaron registros, postulaciones, expresiones de interés ni descargas masivas. Se completó una [prueba HTTP de búsqueda → ficha → cuatro archivos](prod2-http-flow-20261005.md), sin navegador ni escrituras de negocio.

## Conclusion operativa

`prod2` contiene varias superficies que representan momentos distintos de una compra publica. No deben tratarse como una sola lista de licitaciones vigentes:

| Superficie | Que representa | Tratamiento en BuenaPro |
| --- | --- | --- |
| Anuncio de Contratacion Futura | Intencion temprana de compra; aun puede no existir una convocatoria | `future_notice`, señal temprana no postulable |
| Procedimiento de Seleccion | Proceso formal convocado en SEACE | oportunidad; calcular si se puede postular con su cronograma |
| Expresion de Interes | Convocatoria especifica de expresiones de interes | oportunidad separada segun su acceso y fechas |
| Difusion de Requerimiento | Requerimiento difundido antes o fuera del procedimiento formal | señal/requerimiento, no asumir postulacion |
| Ordenes de Compra y Servicio | Compra u orden ya emitida | resultado/historico, no oportunidad abierta |
| Condiciones de Contratacion | Informacion contractual asociada | enriquecimiento o resultado |

La pantalla mostrada por el usuario corresponde a **Anuncio de Contratacion Futura**. Aunque diga `Concurso Publico de Servicios`, no demuestra que el proceso este abierto para recibir propuestas. La fecha aproximada incluso puede haber vencido sin que el anuncio desaparezca de esa lista.

## URLs oficiales

| Uso | URL | Verificada |
| --- | --- | --- |
| Portal | `https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml` | si |
| Detalle publico | `https://prod2.seace.gob.pe/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml?id={uuid}&ptoRetorno=LOCAL` | si |

## Acceso y limitaciones tecnicas

```yaml
access_mode: public
authentication: none_for_reading
transport: JSF/PrimeFaces postbacks
session_required: true
recaptcha_observed: v3
recaptcha_visible_challenge_observed: false
stable_json_api_observed: false
excel_export_observed: true
legal_permissions_review: pending
```

La interfaz publica usa JSF/PrimeFaces. Las consultas son `POST` al XHTML con:

- cookie de sesion;
- `javax.faces.ViewState` vigente;
- IDs de componentes generados por el formulario;
- parámetro del botón nativo `<source>=<source>` además de `javax.faces.source`;
- token de reCAPTCHA v3 en las búsquedas de navegador observadas; la prueba HTTP posterior fue aceptada sin generarlo ni reutilizarlo.

No aparece una casilla de CAPTCHA ni se presento un reto visual durante las
pruebas. El portal carga **reCAPTCHA v3 invisible**: el DOM contiene el script
`https://www.google.com/recaptcha/api.js?render=...`, el campo oculto
`g-recaptcha-response` y un token propio del buscador. Por eso una persona puede
no ver ningun CAPTCHA aunque el control exista tecnicamente.

No se debe intentar evadir ese control. El adaptador debera priorizar una
exportacion oficial o acceso permitido; si usa automatizacion de navegador,
debe conservar la sesion, limitar la frecuencia y detenerse cuando cambie el
control anti-bot.

### Prueba HTTP directa sin navegador

Se probo la misma superficie con `curl`, cookies y JSF, sin intentar generar o
evadir el token de reCAPTCHA:

1. `GET /buscadorPublico/buscadorPublico.xhtml` respondio `200 text/html`, creo
   `JSESSIONID` y entrego `javax.faces.ViewState`.
2. El HTML incluyo el formulario JSF, la clave publica de reCAPTCHA v3, el
   campo `tokenBusProSel` y los filtros completos.
3. Se envio un `POST` parcial JSF al mismo XHTML con una sesion valida,
   `ViewState`, año 2026, objeto Servicio y descripcion `ENSAYOS NO
   DESTRUCTIVOS`, pero sin token de reCAPTCHA.
4. El servidor respondio `200 text/xml`, sin error explicito, pero devolvio
   `0` resultados. La misma consulta desde el navegador devolvio `3`, incluido
   `ABR-PROC-51-2026-OFP / PETROPERU-1`.
5. Se solicito directamente, desde una sesion HTTP nueva, la URL UUID de la
   ficha que habia generado el navegador. Respondio `200 text/html`, pero sin
   nomenclatura, cronograma ni documentos.

Estos primeros intentos no acreditaron una búsqueda correcta. La prueba del
05/10/2026 corrigió la secuencia JSF y el parámetro nativo del botón, y obtuvo
resultados por HTTP sin navegador. No son endpoints REST JSON: requieren
sesión y estado coordinado de JSF; la ficha UUID depende del contexto que la
generó. Un `200` no demuestra éxito: los contract tests deben exigir resultados
y campos esperados, no solamente el código HTTP.

## Comparacion verificada contra OCDS

Se busco el mismo procedimiento en ambos portales:

```yaml
seace_tender_id: "1248325"
nomenclature: INTER-PROC-30-2026-SBS-1
buyer: SUPERINTENDENCIA DE BANCA, SEGUROS Y AFP
ocid: ocds-dgv273-seacev3-1248325
```

La fila de `prod2` envia internamente `nidProceso=1248325`. El record OCDS usa
`tender.id="1248325"` y declara como fuente:

```yaml
id: seace_v3
name: Sistema Electronico de Contrataciones del Estado - Version 3
url: https://prodapp2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml
```

Campos cotejados:

| Campo | `prod2` | OCDS | Resultado |
| --- | --- | --- | --- |
| ID SEACE | `1248325` | `tender.id=1248325` | igual |
| Nomenclatura | `INTER-PROC-30-2026-SBS-1` | `tender.title` | igual |
| Entidad | SBS | `buyer.name` | igual |
| Publicacion | `08/09/2026 15:22` | `2026-09-08T15:22:00-05:00` | igual, normalizada |
| Objeto | Servicio | `services` | igual, normalizado |
| Descripcion | Suscripcion de Kahoot... | `tender.description` | igual |
| Cuantia | `1,225.44 USD` | `tender.value.amount/currency` | igual |
| Direccion, telefono y web | ficha de entidad | `parties[]` | iguales |
| Documentos | 3 archivos | `tender.documents[]` | mismos tipos y horas |

Los tres documentos cotejados fueron Bases Administrativas, Documentos de
Presentacion de Propuestas y Documentos de Otorgamiento de Buena Pro.

La conclusion no es que ambos tengan copias identicas de cada campo:

- `prod2` conserva mejor el detalle operativo: numero de convocatoria, tipo de
  compra, normativa aplicable, gratuidad y cronograma con horas por etapa;
- OCDS agrega OCID, RUC, CUBSO/UNSPSC, estructura normalizada, participantes,
  adjudicacion, proveedor, releases e historico consultable;
- OCDS declara a SEACE v3 como su fuente para este proceso, por lo que no debe
  crearse una segunda oportunidad solo por venir de otro portal;
- la frescura puede diferir. El archivo mensual OCDS consultado tenia corte
  `2026-09-18 11:31`, por lo que no podia contener un proceso de `prod2`
  publicado ese mismo dia a las `23:58`.

### Caso donde OCDS no contiene el cronograma: `1246258`

Se verifico el proceso:

```yaml
ocid: ocds-dgv273-seacev3-1246258
nomenclature: ABR-PROC-51-2026-OFP / PETROPERU-1
buyer: PETROLEOS DEL PERU S.A.
object_type: Servicio
```

El record OCDS contiene comprador, descripcion, monto, items, CUBSO,
adjudicatario, documentos y contrato, pero no contiene `tenderPeriod`. La ficha
de `prod2` si expone:

```yaml
purchase_type: Por la Entidad
regulation: Acuerdo de Directorio N° 039-2021-PP
cause: 47.i Situacion afecta o puede afectar operacion
schedule:
  invitacion:
    channel: VIA CORREO ELECTRONICO
    date: 2026-05-14
  proposal_reception:
    channel: VIA CORREO ELECTRONICO
    start: 2026-05-15 08:30
    end: 2026-05-15
  award:
    channel: VIA CORREO ELECTRONICO
    date: 2026-06-11
```

OCDS registra ademas como adjudicatario a `TESTING SERVICE GROUP SOCIEDAD
ANONIMA CERRADA` y un contrato firmado el `2026-08-14`, por lo que al momento
de la verificacion el proceso ya estaba contratado. La publicacion visible en
SEACE del `2026-09-01` no debe confundirse con el inicio de una ventana de
postulacion: las etapas operativas ocurrieron meses antes.

Este caso demuestra dos reglas:

1. `procurementMethod=open` de OCDS no basta para declarar acceso abierto; se
   debe contrastar el canal y cronograma nativos cuando falten fechas o existan
   causales especiales.
2. Si OCDS carece de `tenderPeriod`, BuenaPro debe marcar la ventana como
   `unknown` hasta consultar `prod2`; nunca inferir que la fecha de publicacion
   equivale al inicio de postulacion.

## Filtros verificados

### Corrección a HTTP directo y prueba acotada del 2026-10-05

El usuario pidió explícitamente prescindir de Computer Use para la ingesta. Se detuvo el navegador y se ejecutó [un probe HTTP](../../scripts/probe_prod2_http.py) desde el Mac con una sesión HTTP nueva, sin cookies/tokens de navegador y sin generar o resolver reCAPTCHA. Se obtuvo el formulario con HTTP 200, cookie de sesión y ViewState; se resolvieron selectores nativos de Servicio/2026/SEACE 3 y el `update` real del botón para `javax.faces.partial.render`.

La consulta `SOFTWARE`, Servicio, publicación 01/10/2026–05/10/2026, devolvió HTTP 200 XML parcial con actualización de la tabla, contador total 0 y «No se encontraron Datos». Minutos antes, el flujo normal del navegador mostró nueve servicios para esos filtros. Una primera petición sin resolver el `update` nativo había entregado solo 198 bytes y ninguna tabla; ese intento no se interpreta como búsqueda vacía. [Evidencia del request con render nativo](evidence/prod2-http-direct-20261005.json).

La prueba posterior resolvió la secuencia nativa de pestaña y botón: faltaba enviar `<botón>=<botón>` además de `javax.faces.source`. Con sesión HTTP nueva se obtuvieron **nueve servicios**, identidad de fila verificada, ficha del procedimiento **1255251 / convocatoria 1046958**, cuatro etapas de cronograma y **cuatro archivos descargados: dos PDF y dos ZIP**. El TDR conserva firma, tamaño y SHA anteriores. La ficha figura Adjudicado. El cero anterior provenía de una petición incompleta y no se interpreta como bloqueo por reCAPTCHA ni ausencia de licitaciones. Sin navegador, tokens de navegador, DB o modelos. [Recorrido reproducible y límites](prod2-http-flow-20261005.md), [evidencia completa](evidence/prod2-http-flow-20261005.json).

### Captura de petición real del 2026-10-05

Se observó pasivamente la red de Chromium durante una búsqueda normal por
`SOFTWARE` en 2026. No se exportaron cookies, tokens ni valores ViewState.
El navegador hizo el flujo oficial y se confirmó contenido, no solo HTTP 200.

```text
POST /seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Respuesta: 200 text/xml;charset=UTF-8
Formato: <partial-response> con actualizaciones HTML de PrimeFaces

javax.faces.partial.ajax=true
javax.faces.partial.execute=@all
javax.faces.source=tbBuscador:idFormBuscarProceso:btnBuscarSel
submit=S
tbBuscador:idFormBuscarProceso:anioConvocatoria_input=2026
tbBuscador:idFormBuscarProceso:descripcionObjeto=SOFTWARE
```

También viajan `javax.faces.ViewState`, `tokenBusProSel`, el formulario y los
filtros por objeto/tipo, entidad, ubicación y fechas. IDs `j_idt...` cambiaron
entre sesiones: no fijarlos en el cliente. Respuesta confirmada: 306 resultados,
primera página de 15, 21 páginas. No apareció un campo `estado`/`vigente` en
la petición de búsqueda ni en el formulario básico/avanzado inspeccionado.
Esto no prueba que el backend carezca de funciones internas no publicadas.

El recurso oficial `javax.faces.resource/js/utils.js.xhtml?ln=default&v=1_0`
confirma el flujo: `cargaTokenBuscadorProSel()` solicita reCAPTCHA v3 con acción
`frmBuscadorProcedimientosSeleccion`, rellena `tokenBusProSel` y acciona el
botón JSF `btnBuscarSel`. Capturar la petición es viable; copiar una URL o
repetir indefinidamente una petición no produce una API autónoma estable.
La estrategia implementable es mantener una sesión de navegador y consumir
las respuestas XML del flujo normal, o leer la tabla después de verificar
que terminó la consulta. La vigencia requiere ficha, estados y cronograma.

### Estudio en vivo del 2026-10-05

Se repitió el flujo público en Chrome y navegador interno, sin credenciales,
sin extraer tokens ni resolver CAPTCHA manualmente. La consulta tiene respuesta
asíncrona: inmediatamente después de pulsar Buscar la tabla conserva los datos
anteriores. El scraper debe esperar contenido nuevo antes de leer el contador.
Aunque el iframe muestra `This site is exceeding reCAPTCHA Enterprise free quota`,
las consultas finalmente respondieron; el aviso por sí solo no demuestra bloqueo.

Resultados observados (año 2026, SEACE 3, objeto sin filtro):

| Consulta | Resultado visible | Interpretación |
| --- | --- | --- |
| Descripción `SOFTWARE`, sin fechas | 306, 21 páginas de 15 | Coincidencias anuales, no conteo de vigentes |
| Sin descripción, publicación 01/10/2026–05/10/2026 | 499, 34 páginas | Compatible con límite previamente observado; no usar como censo nacional |
| `SOFTWARE`, publicación 01/10/2026–05/10/2026 | 9, una página | Publicaciones recientes, todavía requieren validar cronogramas/estado |

La ficha del procedimiento `1253873`, convocatoria `1046875`,
`CP-ABR-11-2026-SIS-1`, fue abierta mediante la acción de ficha del resultado:

- entidad: Seguro Integral de Salud;
- registro electrónico: 03/10/2026 00:01–13/10/2026 23:59;
- presentación electrónica: 14/10/2026 00:01–23:59;
- ítem: estado `Convocado`, CUBSO `8111180500375926`;
- bases administrativas listadas: 2055 KB; no descargadas en este estudio.

Es una oportunidad tecnológica con registro abierto al 05/10/2026; **la ventana
de ofertas aún no comienza**. No confundir “puedo registrarme y preparar oferta”
con `open_for_submission=true`.

Se inspeccionaron el formulario básico y el avanzado: no hay selector nativo
de tecnología, segmento CUBSO o vigencia. Descripción permite prefiltrar, pero
una sola palabra no cubre toda tecnología. Clasificar después por CUBSO de ítems
y descripción con listas configurables (bienes segmento 43; servicios TI con
familias específicas, no todo segmento 81).

**Total nacional vigente: no determinado en este estudio.** No se recorrieron
todas las particiones ni todas las fichas; 306, 499 y 9 no son ese total. Para
obtenerlo hace falta un censo paginado, partición por fechas/objeto/entidad,
deduplicación por ID, revisión de cronograma y estado por ítem, checkpoints y
conteo separado de desconocidos. Incluir convocatorias anteriores todavía
abiertas; limitarse a octubre o a palabras clave no garantiza cobertura.

El estudio valida la viabilidad del flujo navegador → búsqueda → ficha →
cronograma/CUBSO. No implementa aún un adaptador PROD2 ni acredita cobertura
completa o que una oportunidad concreta esté ausente de PROD4.

### Reinspección de búsqueda y deduplicación del 2026-10-05

Una lectura posterior del tab Chrome del buscador confirmó `SOFTWARE`, año
2026, con contador 307 resultados y 21 páginas de 15. Es un resultado dinámico
distinto del 306 observado antes; no es conteo vigente ni censo nacional. No se
envió una nueva búsqueda ni se cambiaron filtros durante esta reinspección.

Las acciones DOM de las primeras filas permiten distinguir identificadores:
SEDAPAR usa ID de procedimiento `1254987` en la acción SNIP/CUI y `1046937` en
la acción de ficha; SIS usa `1253873` y `1046875`, respectivamente. El mismo
nombre de parámetro `nidProceso` representa valores distintos según la acción.
El parser debe resolver procedimiento/convocatoria por el contexto comprobado,
no deduplicar por el nombre de un parámetro o un título. No se conservaron en
archivos valores opacos de sesión o convocatoria del DOM.

El POST JSF por descripción ya observado permite buscar keywords, pero no es
una API JSON autónoma ni un scraper implementado. El plan de cobertura y caché
se describe en `docs/shared-technology-ingestion-architecture.md`: combinar
keywords y particiones por fechas, validar páginas/límites, guardar el mínimo
antes de Jev y no afirmar cobertura total sin el censo correspondiente.

### Anuncio de Contratacion Futura

| Filtro | Disponible |
| --- | --- |
| Entidad | si |
| Tipo de seleccion | si |
| Objeto de contratacion | si |
| Fecha de publicacion desde/hasta | si |
| Descripcion | si |
| Fecha aproximada de convocatoria desde/hasta | si |
| Exportar Excel | si |

Valores de objeto observados:

```text
62 = Bien
63 = Consultoria de Obra
64 = Obra
65 = Servicio
```

Para el alcance actual de BuenaPro se consultaran solamente `62` y `65`.

Una consulta read-only de anuncios futuros de `Servicio` devolvio 83 registros distribuidos en 6 paginas de 15 resultados. El total es vivo y solo sirve como evidencia de funcionamiento a la fecha de verificacion.

### Buscador de Procedimientos de Seleccion

Se verificaron los siguientes filtros:

- entidad;
- tipo de seleccion;
- objeto: bien, servicio, obra o consultoria de obra;
- numero de seleccion;
- descripcion;
- año, con opciones observadas desde 2004 hasta 2026;
- version SEACE 2 o 3;
- SNIP y CUI;
- sigla;
- modalidad;
- departamento, provincia y distrito;
- numero de convocatoria;
- tipo de compra o seleccion;
- fecha de publicacion desde/hasta;
- exportacion a Excel.

La busqueda es rica para segmentar, pero no tiene un filtro simple y confiable de **postulable ahora**. Ese estado debe derivarse desde el cronograma del detalle.

## Procedimiento vigente verificado

Se abrio un resultado real de bienes:

```yaml
nomenclatura: SIE-SIE-5-2026-MDW/DEC-1
buyer: MUNICIPALIDAD DISTRITAL DE WANCHAQ
object_type: Bien
published_at: 2026-09-18 23:58
detail_id: e7e152fd-4667-421b-a3c1-7780a45d1709
```

El detalle expuso:

- entidad compradora;
- nomenclatura y tipo de procedimiento;
- objeto y descripcion;
- monto y moneda cuando estan disponibles;
- cronograma por etapa;
- items;
- documentos descargables;
- contratos, historial y acciones relacionadas cuando existen.

Cronograma observado:

```yaml
convocatoria: 2026-09-18
registro_y_presentacion_electronica:
  start: 2026-09-21 00:01
  end: 2026-09-28 23:59
apertura_y_lances:
  start: 2026-09-29 08:00
  end: 2026-09-29 10:00
buena_pro: 2026-09-29 11:00
```

Esto confirma que `prod2` aporta el dato operativo que el portal OCDS no debe usarse para inferir por si solo: la ventana exacta de participacion.

## Regla para “postulable ahora”

Una oportunidad solo se marcara `open_for_submission=true` si:

```text
submission_start <= now <= submission_end
AND no existe señal de cancelado, nulo, desierto o adjudicado
AND el acceso no es solo por invitacion
```

La publicacion, la planificacion o la convocatoria por si solas no bastan. Si falta el cronograma, el estado canonico debe ser `unknown`, nunca `open` por defecto.

## Modelo para anuncios futuros

Un anuncio futuro se guarda separado de una oportunidad:

```yaml
record_type: future_notice
source: seace_prod2
object_type: goods_or_services
published_at: fecha_publicacion
expected_call_at: fecha_aproximada_convocatoria
buyer_name: entidad
procedure_type_hint: tipo_seleccion
title: descripcion_objeto
scope: alcance
quantity: cantidad
term_days: plazo_dias
open_for_submission: false
```

Cuando aparezca el procedimiento formal, el anuncio no se convierte mediante un `UPDATE` destructivo. Se conserva y se enlaza con `promoted_to_opportunity_id` para mantener trazabilidad.

## Deduplicacion con OCDS y otras fuentes

### Procedimientos formales

Orden de claves para resolver una identidad comun:

1. ID de convocatoria/proceso SEACE y version del sistema;
2. OCID de OCDS cuando tenga la forma `ocds-dgv273-seacev3-{id_convocatoria}`;
3. nomenclatura exacta + identificador/RUC del comprador + año + numero de convocatoria;
4. coincidencia probabilistica solo para revision, nunca fusion automatica silenciosa.

`prod2` y OCDS pueden describir el mismo procedimiento. BuenaPro debe crear **una oportunidad canónica** y guardar dos registros de procedencia (`source_records`). La precedencia por campo sera:

| Campo | Fuente preferida |
| --- | --- |
| OCID, adjudicacion, contrato e historico analitico | OCDS |
| cronograma operativo y ventana de postulacion | detalle `prod2` |
| documentos oficiales del procedimiento | `prod2` |
| clasificacion y texto base | conservar ambos; mostrar el valor oficial mas reciente |

### Anuncios futuros

Antes de existir una convocatoria no suele haber ID formal compatible con OCDS. Para sugerir el enlace posterior se comparan:

- comprador normalizado y, si existe, RUC;
- objeto de contratacion;
- tipo de seleccion;
- similitud de descripcion y alcance;
- fecha aproximada frente a fecha real dentro de una ventana configurable.

Una coincidencia ambigua queda para revision humana. No se elimina el anuncio ni se crea un duplicado de la oportunidad.

## Paginacion y particionamiento

- La UI observada pagina a 15 filas.
- En algunas consultas de procedimientos se observo un total truncado en 499 resultados. Debe considerarse un limite operativo observado, no un contrato oficial.
- Para evitar perdida, dividir por objeto (`Bien`/`Servicio`), rangos cortos de fechas, año y, cuando sea necesario, entidad o ubicacion.
- Preferir la descarga Excel oficial para lotes cuando el portal lo permita y validar que el archivo contenga el mismo universo que la tabla.
- Cada particion debe guardar checkpoint y conteo esperado/recibido.

## Documentos

El detalle publico lista documentos con descripcion y fecha de publicacion. El botón llama `descargaDocGeneral(...)`; la primera verificación no identificó una URL REST estable. La revisión del 05/10/2026 encontró en el JavaScript oficial un servicio HTTP documental separado de la búsqueda JSF: `https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc`, con UUID observado, callback y `guest=false`. Su respuesta entrega una URL con `alf_ticket`; guardar el UUID/resolver y regenerar la descarga, sin exponer el ticket como enlace público permanente.

El adaptador debe guardar primero metadatos y procedencia, descargar con el flujo público normal y detectar MIME por contenido. No considerar la etiqueta del portal prueba de que el archivo sea bases completas: puede contener un TDR.

El 05/10/2026 se verificó la descarga solicitada del TDR de cinco páginas de CONV-PROC-84-2026-SCI-BID-1, desde su fila Bases Administrativas y mediante el evento normal de descarga en Chrome. PDF válido de 316.451 bytes; LLM1 y revisión LLM2 aisladas completadas, con discrepancias de monto/cronograma y correcciones de calidad documentadas. [Prueba de ficha y documento](prod2-detail-enrichment-probe.md). La integración productiva y la identidad/estado numéricos del ítem no quedaron verificados por esta prueba.

La prueba posterior por HTTP desde el Mac, sin cookies ni cabeceras de autenticación, obtuvo del resolver `result: "200"` y descargó un PDF con exactamente el mismo tamaño/SHA-256. Esto verifica acceso documental de ese caso, no una API JSON autónoma de búsqueda/detalle ni acceso a todos los archivos desde cualquier servidor. Un GET independiente a la ficha UUID de ese mismo caso devolvió 200 sin nomenclatura/documento esperado: su contexto JSF sigue siendo necesario.

La [prueba desde la VM productiva](prod2-vm-access-probe.md) del mismo día devolvió HTTP 403 al abrir buscador, ficha conocida y los cuatro resolvers documentales del expediente. No llegaron formulario ni URLs de descarga; no se pudo enviar una búsqueda o navegar desde resultados. Cero archivos descargados. El control desde el Mac, con las mismas peticiones, obtuvo formulario/200 y resolver/200; regla exacta del bloqueo desconocida. No activar un barrido desde esta VM con el transporte probado.

El [diagnóstico ampliado](prod2-network-diagnostic.md) confirmó región OCI Valparaíso/Chile y ASN Oracle, DNS/TLS/certificado iguales en los destinos SEACE, salida pública host/contenedor coincidente y proxy efectivo ausente. VM y worker-io reciben 200 del conteo público PROD4 mientras PROD2/Alfresco responden 403. La plantilla es compatible con Oracle WAF; una regla geográfica es plausible, sin poder separarla de IP/ASN/cliente ni confirmar la política exacta desde el cliente.

## Pruebas ejecutadas

| Prueba | Resultado | Evidencia | Fecha |
| --- | --- | --- | --- |
| Carga del portal | OK | XHTML publico accesible | 2026-09-21 |
| Filtros de anuncios futuros | OK | bienes/servicios y fechas disponibles | 2026-09-21 |
| Consulta de anuncios futuros de servicios | OK | 83 resultados, 6 paginas observadas | 2026-09-21 |
| Busqueda de procedimientos de bienes | OK | resultados y paginacion visibles | 2026-09-21 |
| Detalle de procedimiento | OK | datos, items, cronograma y documentos | 2026-09-21 |
| Ventana vigente de participacion | OK | fechas de registro/presentacion verificadas | 2026-09-21 |
| Descubrimiento de adjudicaciones/contratos | Parcial | pestañas y acciones presentes; backfill queda en OCDS | 2026-09-21 |
| Exportacion Excel | Visible, no incorporada | botones oficiales presentes | 2026-09-21 |
| Idempotencia multi-fuente | Diseñada, no implementada | claves descritas en esta guia | 2026-09-21 |

## Riesgos

- JSF, IDs dinamicos, ViewState y CAPTCHA hacen fragil una integracion directa.
- La lista de anuncios futuros contiene fechas aproximadas vencidas; no es un feed de vigentes.
- La UI puede truncar universos grandes; no se debe backfillear con una sola consulta.
- El mismo proceso puede aparecer en OCDS, `prod2`, otros buscadores SEACE y portales institucionales.
- Los nombres de entidad y las descripciones no bastan como clave de deduplicacion.
- La disponibilidad publica para lectura no concede automaticamente permiso de redistribucion documental.

## Implementacion propuesta

```yaml
adapter_path: pending
fixtures_path: pending
contract_tests_path: pending
polling:
  future_notices: daily
  open_procedures: every_30_to_60_minutes
  lifecycle: every_6_to_12_hours
backfill_source: OCDS_first
production_enabled: false
```

### Actualizacion manual desde el detalle de BuenaPro

La integracion de `prod2` no se ejecutara masivamente al cargar cada pantalla.
El detalle de oportunidad tendra una accion **Actualizar datos oficiales** con
este orden:

```text
Usuario pulsa Actualizar
        |
        v
Consultar OCDS por OCID/tender.id
        |
        +-- OCDS tiene release nuevo y datos operativos suficientes
        |       -> actualizar la oportunidad canonica
        |
        +-- OCDS sigue sin cronograma/canal o contiene inconsistencias
                -> encolar consulta selectiva a prod2
                -> mantener visible el ultimo snapshot
                -> actualizar al terminar sin duplicar la oportunidad
```

El request web no debe permanecer abierto mientras navega `prod2`. La API
responde con un job y la interfaz muestra `Actualizando...` hasta que termine.
Para evitar abuso o trabajo duplicado se guardaran:

```text
last_ocds_refresh_at
last_prod2_refresh_at
official_refresh_status
official_refresh_error
next_refresh_allowed_at
```

Reglas:

- OCDS siempre se intenta primero;
- `prod2` solo se usa si OCDS no resolvio la carencia concreta;
- un refresh concurrente reutiliza el job ya activo;
- aplicar cooldown por oportunidad y usuario;
- conservar procedencia y snapshot anterior ante error;
- validar contenido esperado: HTTP `200` vacio no cuenta como exito;
- no crear una oportunidad nueva cuando coincide `tender.id`/OCID.

La exportacion Excel no forma parte de este flujo por decision de producto.

Secuencia:

1. usar OCDS para backfill e identidad historica;
2. ingerir anuncios futuros de bienes y servicios como señales separadas;
3. descubrir procedimientos recientes de bienes y servicios en `prod2`;
4. consultar detalle solo para candidatos nuevos o cambiados;
5. derivar la ventana `open_for_submission` desde el cronograma;
6. reconciliar con OCDS antes de crear la oportunidad canonica;
7. enlazar anuncios futuros con su proceso cuando haya evidencia suficiente.

## Proxima accion

Implementar el refresh asincrono OCDS-first con fallback selectivo a `prod2`,
cooldown, estados visibles y contract tests para respuesta completa, vacia y
fallida. Despues validar la reconciliacion contra cinco procesos presentes en
ambas fuentes.
