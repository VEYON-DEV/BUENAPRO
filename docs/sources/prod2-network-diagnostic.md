# Diagnóstico del rechazo de PROD2 desde producción

La VM de BuenaPro está en Oracle Cloud, región `sa-valparaiso-1` —Valparaíso, Chile— y sale con IP pública `168.129.177.199`. El rechazo ocurre al solicitar el formulario y los documentos de PROD2. DNS, conexión TCP, certificado y negociación TLS funcionan. La explicación más consistente es una política de acceso en la protección del portal, posiblemente relacionada con el origen; no hay evidencia suficiente para identificar una regla exclusivamente geográfica.

Pruebas del 05/10/2026, aproximadamente 18:49–18:51 de Lima, desde Mac, host VM y contenedor `worker-io`. [Evidencia estructurada completa](evidence/prod2-network-diagnostic-20261005.json). La prueba anterior de [ficha y cuatro resolvers](prod2-vm-access-probe.md) sigue siendo evidencia complementaria.

## Reprueba con el cliente HTTP corregido

El 05/10/2026, 20:43–20:45 Lima, se repitió desde la VM el [cliente que ya completó búsqueda/ficha/documentos desde el Mac](prod2-http-flow-20261005.md). Se copiaron únicamente `client.py`, `documents.py` y un diagnóstico acotado a una carpeta privada; SHA-256 confirma que son los mismos módulos locales. Se ejecutó un contenedor temporal con la imagen worker existente, red del host y TLS validado, sin variables de negocio ni arranque del worker. No se instalaron paquetes o desplegó código sobre producción.

| Paso | VM, código corregido | Control contemporáneo Mac |
| --- | --- | --- |
| GET inicial del buscador | 403, 69 bytes, sin formulario/ViewState | 200 |
| POST SOFTWARE/Servicio/2026/01–05Oct | No alcanzado: bootstrap rechazado | 9 resultados, identidad verificada |
| Ficha desde fila | No alcanzada | 200, procedimiento1255251/convocatoria1046958 |
| Resolver documental | 403 usando UUID TDR conocido | 200; TDR obtenido de ficha y descargado |
| Archivo final | Ninguno | 316451 bytes, cinco páginas y SHA esperado |

Los dos cuerpos 403 de la VM conservaron SHA-256 `10a6977b5afbb46e864aa8ebc082f9660394e44c14585213eab937c93fcded0e`. La corrección del parámetro nativo del botón resuelve la búsqueda incompleta en el Mac; no puede llegar a ejecutarse desde este origen, porque el GET anterior no entrega la aplicación. No se envió un POST inventando estado JSF ni se intentó eludir el rechazo. El origen/regla exactos siguen sin aislarse.

[Evidencia de la reprueba y control](evidence/prod2-vm-corrected-client-20261005.json). El contenedor temporal se eliminó al terminar; se comprobó que scheduler y los cuatro workers legacy seguían detenidos. Sin navegador, BD, modelos o reactivación de fuentes antiguas. Diagnóstico remoto conservado en carpeta privada `/home/ubuntu/prod2-http-corrected-20261005-hlenszkc/`.

## Ubicación y origen comprobados

El servicio de metadatos de la propia VM entregó `region=sa-valparaiso-1`, `canonicalRegionName=sa-valparaiso-1` y `shape=VM.Standard.A1.Flex`. Oracle identifica esa región como [Chile West, Valparaíso](https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm). No se exportaron OCIDs, claves SSH, metadatos de usuario o credenciales.

La salida IPv4 del host y del contenedor coincidió con `168.129.177.199`. La base GeoIP consultada clasificó la VM como `CL`, `AS31898 Oracle Corporation`, y la conexión del Mac como `PE`, `AS6147 INTEGRATEL PERÚ S.A.A.`. GeoIP describe la clasificación de esa base; no demuestra cómo clasifica SEACE la IP. La región OCI aporta confirmación independiente de la ubicación de la VM.

## Comparación de acceso HTTP

Se usaron GET HTTPS, el mismo User-Agent, ninguna cookie o credencial de portal, TLS validado y sin reintentos automáticos.

| Recurso observado | Mac | Host VM | Contenedor worker-io |
|---|---|---|---|
| Buscador PROD2 | 200, formulario/ViewState y script reCAPTCHA | 403, sin formulario | 403, sin formulario |
| JavaScript oficial `cmsDescarga.js` | 200, código de descarga presente | 403 | No se repitió |
| Resolver del TDR conocido | 200, `result="200"`, URL de descarga entregada | 403 | 403 |
| Conteo público API PROD4, puerto 8086 | 200, conteo 2903 | 200, mismo conteo/cuerpo | 200, mismo conteo |
| HTTPS general, example.com | 200 | 200 | No se repitió |
| Página institucional gob.pe/oece | 200 | 200, mismo cuerpo | No se repitió |

El conteo PROD4 fue un control de conectividad a una superficie pública ya documentada; no modifica el alcance exclusivo PROD2 del colector propuesto. Un 200 se contrastó con el contenido esperado, no solo con el código.

Los tres rechazos del host VM recibieron el mismo cuerpo, 69 bytes y SHA-256 `10a6977b5afbb46e864aa8ebc082f9660394e44c14585213eab937c93fcded0e`. No entregaron cookie de sesión, ViewState, código reCAPTCHA ni URL de descarga. En el cliente no pudo iniciarse la búsqueda ni el flujo JSF/reCAPTCHA; la ejecución interna del servidor no es observable.

## Red, certificados y entorno

| Comprobación | Resultado |
|---|---|
| DNS PROD2 | Mac y VM: `141.148.46.142` |
| DNS Alfresco documental | Mac y VM: `129.80.46.214` |
| DNS control PROD4 | Mac y VM: `129.158.255.27` |
| IPv6 en los tres destinos SEACE | No se observaron registros AAAA |
| TCP y TLS de los destinos SEACE | Conectados y certificados validados en ambos orígenes |
| Certificado SEACE | Mismo SHA-256 en Mac y VM; emisor Entrust; vencimiento 19/01/2027 |
| Negociación SEACE | TLS 1.2, `ECDHE-RSA-AES128-GCM-SHA256`, en ambos orígenes |
| Proxies efectivos de Python, host VM y Mac | Ninguno configurado |
| Variables proxy en worker-io | Ninguna presente |
| Reloj VM | NTP sincronizado |
| Salida del contenedor | Misma IP pública que el host; reproduce 403 y control PROD4/200 |

Estos resultados descartan, para las solicitudes medidas, un fallo de resolución, imposibilidad de conexión, certificado inválido o salida general sin Internet. Reproducir el resultado fuera y dentro de Docker reduce la probabilidad de que una configuración exclusiva del contenedor origine el rechazo. No se modificó firewall, DNS, proxy, rutas o certificados para realizar la prueba.

Misma versión TLS y cifrado negociado no significan huella TLS idéntica: el Mac usa Python 3.9.6/LibreSSL 2.8.3; el host Python 3.10.12/OpenSSL 3.0.2; el contenedor OpenSSL 3.5.7. No se igualaron todos los atributos de cliente. El diagnóstico no atribuye la diferencia únicamente al país o a la IP.

## Qué explica mejor el 403

El cuerpo recibido coincide exactamente con una [plantilla documentada de acción Oracle WAF](https://docs.oracle.com/en-us/iaas/Content/WAF/Actions/create_action.htm):

```json
{"code":"403","message":"Forbidden","RequestId":"${http.request.id}"}
```

Es una señal fuerte de respuesta configurada en una capa de protección, no una identificación absoluta de su fabricante ni de la regla. El texto `${http.request.id}` llegó literal y no apareció `X-Request-Id` en las respuestas rechazadas; no hay un ID real de incidente que pueda extraerse de ese campo. La falta de interpolación es una inferencia, no acceso a la configuración del portal.

Oracle permite [condiciones por IP/rango, país, ASN y atributos HTTP](https://docs.oracle.com/en-us/iaas/Content/WAF/Concepts/understanding_conditions.htm). Esto explica por qué una conexión peruana de ISP y una VM chilena de cloud pueden recibir decisiones distintas, pero no demuestra qué condición usa SEACE.

| Hipótesis | Evaluación con esta evidencia |
|---|---|
| Regla geográfica que restringe ese origen | Plausible: VM Chile rechazada y Mac Perú permitido; país no aislado del proveedor/IP/cliente |
| Regla por proveedor cloud, ASN, rango o reputación de IP | También plausible; no se probó otra red que permita separarla del país |
| Regla por cliente o atributos HTTP/TLS | Sigue posible; cambiar la librería entre host y contenedor no resolvió el rechazo, pero no iguala una sesión de navegador |
| Regla específica por host/ruta/puerto | Compatible con PROD2/Alfresco rechazados y API PROD4 permitida; las reglas efectivas no son observables |
| Límite de tasa | No identificado: no se observó Retry-After ni regla; 403 no lo descarta por sí solo |
| Falta de sesión/reCAPTCHA como causa de este GET rechazado | No demostrada: se rechazó antes de obtener el formulario; esos requisitos siguen existiendo para búsquedas cuando el acceso inicial funciona |
| Caída general de SEACE, Internet o certificados | No explica el patrón medido de controles y acceso contemporáneo desde Mac |

No se puede observar desde el cliente si las solicitudes rechazadas llegaron al backend. La regla exacta —país, ASN, rango/IP o cliente— requiere configuración/logs del operador o controles autorizados que separen esas variables.

## Navegador y alcance pendiente

Esta corrida no instaló ni ejecutó un navegador remoto PROD2. La [prueba previa de PROD4 en la VM](prod4-vm-browser-probe.md) sí usó Chromium normal headless y con interfaz/Xvfb y recibió 403 tanto en portal como en documento. Eso no prueba el resultado de un navegador PROD2 y tampoco justifica asumir que instalar Chromium resolverá el acceso.

El flujo PROD2 de búsqueda → ficha desde fila → descarga completa está acreditado desde el Mac; sigue pendiente en la VM. La reprueba del cliente corregido confirmó rechazo antes del POST. No hubo solución de CAPTCHA o cambio de controles en estas pruebas. La descarga del TDR y su hash siguen acreditados desde el Mac.

## Acción recomendada

Solicitar al operador OECE/SEACE revisión del rechazo para la IP productiva y orientación sobre el acceso automatizado público permitido. El [formulario oficial de contacto](https://consultas.oece.gob.pe/formulario-contacto) está registrado en el [Comunicado 010-2026-OECE](https://www.gob.pe/institucion/oece/noticias/1427338-comunicado-n-010-2026-oece-oece-fortalece-sus-canales-de-atencion-virtual-a-nivel-nacional). No se encontró un procedimiento público que garantice una allowlist para scrapers.

Texto técnico preparado para revisar y enviar manualmente:

> Desde una VM Oracle Cloud en Chile West, sa-valparaiso-1, IP pública 168.129.177.199, recibimos HTTP 403 al acceder al buscador público PROD2 y al resolver documental Alfresco. Pruebas del 05/10/2026, aproximadamente 23:49–23:51 UTC, con validación TLS normal y sin credenciales. Desde una conexión peruana el mismo recurso responde 200. El cuerpo contiene RequestId literal, sin X-Request-Id real. Solicitamos orientación sobre acceso automatizado autorizado y revisión de la regla que rechaza este origen, incluyendo país/ASN/rango si corresponde. Adjuntamos URL exacta, horarios y evidencia sanitizada.

No se envió ninguna solicitud externa de soporte. El expediente y rutas exactas están en la evidencia; no incluir cookies, tickets o claves.

Para mantener el diseño, separar colector y procesamiento: PostgreSQL y modelos pueden seguir en la VM mientras se valida el acceso del colector. Un piloto en Perú podría ayudar si la regla es geográfica; un datacenter peruano podría seguir rechazado si el filtro es por cloud/ASN/IP. Verificar el flujo completo antes de contratar o activar esa infraestructura. No activar el barrido desde la VM con el transporte actualmente rechazado.

## Evidencia y cambios

Se ejecutó un diagnóstico temporal acotado en el host y una lectura independiente dentro del contenedor worker-io, sin importar la configuración de negocio del worker. Se conservaron reportes privados con permiso 600 en `tmp/prod2-detail-probe-20261005/` y en `/home/ubuntu/prod2-access-probe-20261005-27ec2fnk/`. El reporte público conserva resultados estructurados y omite valores de cookies, tickets, claves y metadatos de usuario.

No se instalaron paquetes, cambiaron despliegues, activaron jobs o hicieron llamadas LLM. Los servicios y la pausa operativa permanecen sin cambios por esta intervención.
