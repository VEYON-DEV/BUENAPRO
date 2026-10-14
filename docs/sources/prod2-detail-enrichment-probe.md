# Prueba de ficha y documento PROD2

La ficha CONV-PROC-84-2026-SCI-BID-1 del Ministerio Público permite enriquecer un resultado de búsqueda y descargar su documento desde una sesión normal de Chrome. El botón Bases Administrativas descargó un PDF cuyo contenido es un TDR de cinco páginas. Una prueba aislada de LLM1 y revisión LLM2 confirmó que puede leerse, pero identificó discrepancias y errores de extracción que requieren conservar fuentes y revisar calidad antes de evaluar empresas.

Actualización del 05/10/2026: el [recorrido HTTP completo](prod2-http-flow-20261005.md) abrió esta ficha desde una búsqueda propia, sin navegador, y descargó los cuatro archivos de su inventario. Se verificaron procedimiento1255251/convocatoria1046958, cuatro etapas y estado Adjudicado. Esos datos corrigen las limitaciones de identidad/estado de la primera exploración descrita abajo. El TDR conserva su SHA; los dos ZIP tienen CRC válido. Esta nueva comprobación no ejecutó LLM ni escrituras a BD.

## Ficha y descarga verificadas

Lectura del 5 de octubre de 2026: servicio Analista técnico de software, convocatoria 1, cuantía S/180.000 y publicación 05/10/2026 11:51. El cronograma muestra registro hasta 25/08/2026 10:00 y presentación de propuestas el 10/09/2026. La hora final de propuestas no consta; no se completa con 23:59.

El inventario contiene Resumen ejecutivo, Bases Administrativas, Documentos de Presentación de Propuestas y Documentos de Otorgamiento de Buena Pro. Solo se descargó el archivo de la fila Bases Administrativas. Los otros documentos no se analizaron y su presencia no acredita su contenido.

La descarga se produjo al accionar el enlace JSF oficial de esa fila y esperar el evento de descarga del navegador. El archivo recibido se llama `2.1+TDR+Analista+Tecnico+de+software_20261005_114903_853 (1).pdf`, tiene 316.451 bytes, empieza con `%PDF-`, no está cifrado y contiene cinco páginas con texto legible. SHA-256: `e587c3b76fca62f050ca86da8fe1f929db622e6a988c32b2a80f504c43356595`.

La primera descarga se verificó por navegador. La revisión posterior del servicio de documentos, descrita abajo, también descargó el mismo PDF por HTTP sin sesión de navegador. Esto no convierte la búsqueda o la ficha en una API JSON autónoma. La acción de listado de ítems devolvió una pantalla de buscador; volver atrás recuperó la ficha. No se verificó el estado oficial del ítem ni su identidad numérica.

## Servicio documental y enlaces

El 05/10/2026 se inspeccionaron los atributos de las acciones visibles. Los cuatro botones tienen `href="#"` y llaman `descargaDocGeneral(uuid, '3', nombre)`. La función delega en `jsCmsSeaceUtil.descargaPriv(uuid)`. El [JavaScript oficial de descarga](https://prod2.seace.gob.pe/seacebus-uiwd-pub/recurso?nombre=cmsDescarga.js) define este flujo:

```text
GET https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc
    ?id=<uuid documental observado>&doc=<nombre callback>&guest=false
→ respuesta JSON/JSONP con result y downloadUrl
→ GET de la URL entregada por el servicio
```

Se probó exactamente el UUID del TDR observado en la ficha: `efdf87fb-9e04-4fa7-9c34-5912db454f7f`. Ambas peticiones se hicieron desde el Mac con verificación TLS normal, sin cookies del navegador ni cabeceras de autenticación. El resolver respondió HTTP 200, `result: "200"` y `downloadUrl`; la segunda petición respondió HTTP 200 y `application/pdf`, con 316.451 bytes y el mismo SHA-256 que la descarga del navegador. Normalizar el código de resultado, que llegó como texto. No se cambió el parámetro `guest` usado por el portal.

La URL final contiene `alf_ticket`; es una referencia con credencial de acceso, no un enlace público permanente. No se publica ni se incorpora a la configuración o logs. Su duración no se midió. Persistir UUID documental, nombre, procedencia y endpoint resolver; obtener una URL autorizada nueva cuando se necesite descargar. El acceso se verificó para este documento desde el Mac. La [prueba posterior desde la VM productiva](prod2-vm-access-probe.md) rechazó buscador, ficha y los cuatro resolvers con HTTP 403; cero archivos descargados. Ante 401/403, detener y registrar el fallo.

La [ficha oficial](https://prod2.seace.gob.pe/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml?id=feeb2d77-cd94-4b3c-8e5f-0d4d90fad52c&ptoRetorno=LOCAL) sí se leyó en el navegador. Un GET independiente a esa misma URL devolvió HTTP 200 sin la nomenclatura ni el UUID documental esperado. Registrar esta referencia como dependiente del contexto JSF y regenerarla desde el resultado oficial si deja de funcionar. No guardar cookies, ViewState o tokens en snapshots destinados a persistencia/consumo público.

Evidencia local ignorada por Git: `public-download-probe.json` contiene estados, tamaño y hash sin ticket; `http-tdr-download.pdf` contiene el PDF contrastado; `standalone-fiche-probe.json` conserva la comprobación negativa del enlace de ficha. La respuesta cruda del resolver queda solo en el temporal privado con permiso 600.

## Lectura y revisión del documento

Se ejecutaron dos llamadas aisladas con `gemini-3.1-flash-lite`: LLM1 recibió el PDF usando el extractor existente; LLM2 recibió PDF original, extracción y campos públicos de ficha para revisar consistencia. No recibió perfiles privados ni evaluó cumplimiento empresarial. Resultados locales en `tmp/prod2-detail-probe-20261005/`, ignorados por Git y con permiso 600 para PDF y JSON.

| Etapa | Tokens de entrada | Tokens de salida | Resultado |
|---|---:|---:|---|
| LLM1 | 4277 | 1461 | Datos de contrato, actividades, pago, requisitos y participación |
| LLM2 | 4869 | 707 | Dos discrepancias, cinco requisitos obligatorios y dos deseables en su lista |

El TDR identifica consultoría individual, dedicación exclusiva y tiempo completo. El plazo inicial es tres meses desde el día siguiente de la firma, con posibilidad de prórroga. Los honorarios son S/10.000 mensuales. S/30.000 es una derivación para el plazo inicial, no un total contractual explícito ni una corrección automática de los S/180.000 de ficha.

En páginas 4 y 5 se exigen formación compatible, mínimo 90 horas acumuladas en cursos afines, cuatro años de experiencia general y tres de experiencia específica. Certificación Scrum Master o Product Owner y proyecto nacional en el sector Justicia son expresamente deseables. Las carreras, materias y roles contienen alternativas; no se exige cumplir todas a la vez. El servicio también exige disponibilidad a tiempo completo y equipo propio.

La fecha de publicación reciente no demuestra que la participación siga abierta: las fechas visibles ya pasaron. Conservar discrepancia y verificar cronograma/versiones. Afinidad con software tampoco demuestra que una empresa pueda postular a una consultoría individual; no se verificó la admisión de personas jurídicas, consorcio o subcontratación en el expediente completo.

## Correcciones de calidad necesarias

La revisión humana de las cinco páginas, con contraste visual de páginas 4 y 5, conservó los outputs crudos y creó `validated-enrichment.json` como capa separada. No reemplaza una prueba amplia de precisión.

- LLM1 convirtió tres meses en 90 días y un informe mensual en 30 días: conservar unidad original y dejar equivalencias exactas desconocidas.
- LLM1 clasificó documentos para el pago como documentos de propuesta y omitió el carácter condicional de suspensión de cuarta categoría. Mantener etapa y condición.
- LLM1 omitió los dos requisitos deseables. LLM2 los recuperó al leer también el original; el segundo modelo basado solo en el JSON incompleto podría no detectarlos.
- LLM2 atribuyó parte de la discrepancia temporal a página 1, aunque las fechas discutidas proceden de la ficha. Validar que cada cita sostenga la afirmación.
- Preservar el cálculo S/30.000 como derivado y ambos montos originales, sin elegir uno como cuantía final.

## Etapa de enriquecimiento propuesta

Después de Jev, abrir la ficha de candidatos aceptados y de ambiguos que necesiten contexto. Primero capturar y guardar sus datos estructurados sin LLM: snapshot sanitizado, fecha de lectura, versión/hash del parser, convocatoria, cuantía/moneda, cronograma completo y canales, estados/ítems/CUBSO cuando estén disponibles, e inventario documental. Guardar referencia de ficha y de documentos con método de acceso, dependencia de sesión y fecha/resultado de verificación. Campos ausentes o acciones fallidas quedan desconocidos. Esta persistencia precede al enriquecimiento semántico de documentos; el registro mínimo de discovery sigue guardándose para todos antes de Jev.

Buena Pro tiene tres campos independientes: etapa/fechas del cronograma, existencia de documentos de esa etapa y resultado adjudicado confirmado. Ganador, monto adjudicado y fecha efectiva solo se completan con una fuente oficial explícita por convocatoria/ítem. En este caso siguen desconocidos; el inventario documental no acredita un ganador. Una fecha sin hora conserva precisión de día, sin completar un cierre ficticio.

Separar afinidad, participación y vigencia. Si el cronograma/estado muestra que no es accionable, conservar metadatos y detener lectura documental automática; esta descarga histórica fue una prueba solicitada del flujo. Un expediente con discrepancias conserva motivo y próxima verificación.

Para documentos pertinentes, resolver la descarga por el servicio oficial observado o mediante la acción normal de ficha si corresponde; validar contenido/tamaño, distinguir etiqueta del portal de clase real y guardar hash, procedencia y versión. LLM1 estructura y cita; reglas comparan datos/evidencia; LLM2 revisa compatibilidad. La caché documental depende de hash y extractor, y la de evaluación depende también del perfil/evidencia. Un PDF reemplazado se relee aunque el título no cambie. Un cambio exclusivo de cronograma actualiza metadatos y vigencia sin volver a ejecutar Jev o lectura documental.

Esta prueba verifica descarga y lectura de un expediente. El adaptador de enriquecimiento PROD2 y su persistencia productiva siguen pendientes. No se ejecutó Jev, no hubo writes de negocio, no se crearon jobs productivos ni se modificó la pausa de Dinaut.
