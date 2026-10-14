# Acceso PROD2 desde la VM productiva

Prueba del 05/10/2026, 18:36–18:38 de Lima. El acceso HTTPS desde la VM actual de BuenaPro fue rechazado antes de cargar la aplicación. No se pudo ejecutar el flujo completo búsqueda → ficha → documentos desde ese origen.

| Operación desde VM | Resultado | Qué se verificó |
|---|---|---|
| Abrir buscador público PROD2 | HTTP 403 | No llegó formulario JSF; no se envió una búsqueda por keyword |
| GET de la ficha UUID del caso conocido | HTTP 403 | No llegaron nomenclatura, cronograma ni inventario |
| Resolver Resumen ejecutivo | HTTP 403 | No se recibió URL autorizada de archivo |
| Resolver Bases Administrativas/TDR | HTTP 403 | No se recibió URL autorizada de archivo |
| Resolver Documentos de Presentación de Propuestas | HTTP 403 | No se recibió URL autorizada de archivo |
| Resolver Documentos de Otorgamiento de Buena Pro | HTTP 403 | No se recibió URL autorizada de archivo |

Resultado: **cero de cuatro archivos descargados desde la VM**. No se presenta el GET de una ficha UUID conocida como si fuera navegación desde un resultado; el bloqueo impidió crear ese contexto. Tampoco se presenta un rechazo del resolver como una petición al PDF final: no se obtuvo su URL.

El expediente es CONV-PROC-84-2026-SCI-BID-1, Analista técnico de software. Los UUID documentales usados proceden de los botones oficiales observados en la sesión normal del Mac; no se adivinaron identificadores ni rutas. El resolver es el servicio que publica el [JavaScript oficial de descarga](https://prod2.seace.gob.pe/seacebus-uiwd-pub/recurso?nombre=cmsDescarga.js), con `guest=false` igual que el portal.

La VM devolvió `{"code":"403","message":"Forbidden","RequestId":"${http.request.id}"}`. Se contrastaron buscador y resolver del TDR desde el Mac con el mismo User-Agent y sin credenciales: el buscador respondió 200 con formulario JSF; el resolver respondió 200, `result=200` y `downloadUrl` en JSONP. La [prueba anterior](prod2-detail-enrichment-probe.md) ya verificó descarga del PDF y hash desde el Mac. Esta diferencia demuestra que el transporte probado desde la VM no tiene el mismo acceso, pero no identifica de manera concluyente la regla de bloqueo ni garantiza acceso desde otro servidor.

Las solicitudes corrieron por SSH en Python estándar sobre la VM ARM64, con su salida de red normal, validación TLS y timeout de 25 segundos. Se hicieron una petición de entrada, una de ficha y una por cada resolver documental; no se reintentaron rechazos, no se usaron tickets del Mac, proxies, rotación de IP o evasión de controles. No se ejecutó un navegador remoto en esta prueba y no se encontró un formulario de login o CAPTCHA en las respuestas recibidas.

No se instalaron paquetes, iniciaron contenedores, reiniciaron servicios, modificaron datos de negocio o crearon jobs. Tampoco hubo llamadas Jev/Gemini. Solo se creó una carpeta privada de diagnóstico en `/home/ubuntu/prod2-access-probe-20261005-27ec2fnk/`, con reporte de permisos 600.

La integración productiva debe mantener captura estructurada, documentos y modelos en etapas separadas. No activar el barrido en esta VM como lector PROD2 con el transporte probado. Antes de migrar el colector a otro origen autorizado, verificar allí formulario, consulta positiva, apertura de ficha desde una fila, cronograma y descarga de todos sus documentos. El procesamiento y la persistencia pueden seguir en el servidor; esto no valida todavía un nuevo canal de ingesta.

Evidencia sanitizada: [reporte VM y control Mac](evidence/prod2-vm-access-20261005.json). Los reportes privados originales están en `tmp/prod2-detail-probe-20261005/production-vm-access-probe.json` y `mac-vm-access-control.json`, ignorados por Git.

El [diagnóstico ampliado de red y origen](prod2-network-diagnostic.md) confirmó VM en Chile/Oracle Cloud, DNS y certificados iguales a Mac, TLS válido, ausencia de proxy efectivo y reproducción desde worker-io. Controles HTTPS, gob.pe y conteo público PROD4 respondieron 200 desde la VM. El rechazo apunta a una política específica de acceso/protección; país frente a IP/ASN/cliente sigue sin aislarse.
