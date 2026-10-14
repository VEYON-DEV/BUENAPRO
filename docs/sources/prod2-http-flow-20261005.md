# PROD2: búsqueda, ficha y documentos por HTTP

El 05/10/2026 se verificó desde la laptop el recorrido completo con `httpx`, sin navegador: búsqueda por SOFTWARE → selección de una fila → ficha con identidad comprobada → descarga de sus cuatro archivos. No se usaron cookies/tokens de un navegador, modelos ni base de datos. [Evidencia sanitizada](evidence/prod2-http-flow-20261005.json).

## Recorrido implementado

1. GET al [buscador oficial](https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml): nueva cookie de sesión y ViewState.
2. POST JSF del evento nativo `tabChange` para Procedimientos de Selección. Selectores y componentes se resuelven del HTML recibido.
3. POST al mismo XHTML con keyword, objeto, año, fechas, ViewState y configuración `update` del botón. PrimeFaces envía tanto `javax.faces.source=<botón>` como `<botón>=<botón>`. Este segundo parámetro faltaba en la prueba anterior que mostraba cero; aquella petición incompleta no acreditaba ausencia de resultados ni bloqueo por CAPTCHA.
4. Lectura del XML parcial y HTML de resultados: comprobar contador, filas e identidades. Las acciones SNIP/CUI contienen el ID de procedimiento; la acción de ficha contiene el ID de convocatoria. No son intercambiables.
5. POST normal, sin AJAX, de la acción de ficha extraída de la fila, usando la misma sesión y el ViewState actualizado. La redirección nativa emite `http://` para el mismo host; el cliente solicita ese mismo destino por HTTPS. Rechaza saltos a otro host. No reutiliza una ficha UUID copiada de otra sesión.
6. Ficha: contrastar nomenclatura, entidad y descripción contra el resultado; extraer cronograma, estado de ítems e inventario. No exportar tablas de postores/personas. Comprobar `rowCount` documental antes de afirmar descarga completa.
7. GET al [resolver oficial de documentos](https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc) con UUID obtenido de la ficha, callback propio y parámetros nativos. Su `downloadUrl` relativa parte del contexto `/alfresco`. Descargar por HTTPS, validar host, estado, tamaño y firma, y guardar SHA-256. El ticket temporal queda únicamente en memoria.

El transporte es HTTP con postbacks JSF/PrimeFaces y XML/HTML; no se encontró una API REST JSON estable para buscar o abrir fichas. La interfaz carga reCAPTCHA v3; las peticiones de esta prueba fueron aceptadas sin generar ni reutilizar su token. Esto describe esta ejecución desde el Mac y no garantiza aceptación permanente desde cualquier origen.

## Resultado comprobado

SOFTWARE, Servicio, 2026, publicación 01–05/10/2026: **nueve resultados**, una página. Ficha seleccionada **CONV-PROC-84-2026-SCI-BID-1**, procedimiento **1255251**, convocatoria **1046958**. Ficha HTTP 200, cuatro etapas de cronograma y estado **Adjudicado**. La publicación reciente no implica que esté abierta para postular.

| Archivo observado | Formato | Bytes | Validación adicional |
| --- | --- | ---: | --- |
| Carta / resumen ejecutivo | PDF | 316466 | 2 páginas |
| TDR Analista Técnico de Software | PDF | 316451 | 5 páginas; SHA coincide con la descarga anterior |
| Documentos de Presentación de Propuestas | ZIP | 19531 | CRC válido; contiene 1 PDF |
| Documentos de Otorgamiento de Buena Pro | ZIP | 16458684 | CRC válido; contiene 2 PDF |

El inventario nativo declara cuatro archivos y se descargaron los cuatro. Los ZIP se verificaron sin extraerlos. Archivos y manifest privados `0600`, bajo `tmp/prod2-http-flow-20261005/validated/`, ignorados por Git. El manifest conserva datos públicos de ficha y hashes; no contiene tickets, cookies, ViewState ni identificadores de personas.

## Ejecutar el lote acotado

Cliente: [client.py](../../workers/seace/src/buenapro_worker/prod2/client.py). Descargador: [documents.py](../../workers/seace/src/buenapro_worker/prod2/documents.py). Entrada: [run_prod2_http.py](../../scripts/run_prod2_http.py).

```bash
.venv/bin/python scripts/run_prod2_http.py \
  --keyword SOFTWARE --year 2026 --object Servicio \
  --from-date 01/10/2026 --to-date 05/10/2026 \
  --nomenclature CONV-PROC-84-2026-SCI-BID-1 \
  --output-dir tmp/prod2-http-example
```

Selecciona una ficha de la primera página y descarga todos los archivos de su inventario cuando ese inventario esté completo. Los nombres locales usan UUID, no rutas proporcionadas por el portal. No sobrescribe documentos. Si el expediente requiere más páginas documentales, se detiene antes de afirmar descarga completa; falta implementar esa paginación. Tampoco ejecuta carga general, persistencia PostgreSQL, Jev o LLM.

Validación: 22 pruebas offline de transporte/identidad/contadores/ViewState/redirecciones/privacidad/descarga y ejecución real completa. La búsqueda Bien usa el mismo selector nativo con código 62; Servicio, 65. El acceso de la VM actual continúa sujeto al rechazo 403 documentado en el [diagnóstico de origen](prod2-network-diagnostic.md); esta prueba no lo resuelve ni reactiva workers antiguos.

Reprueba VM, 05/10/2026 20:45 Lima: el mismo cliente corregido rechazó el GET inicial con403, sin formulario/ViewState; no alcanzó el POST ni la ficha. El resolver TDR conocido también respondió403. Control Mac contemporáneo completó nueve resultados, ficha y TDR. [Evidencia con código corregido](evidence/prod2-vm-corrected-client-20261005.json). No se reiniciaron workers ni se cambió el despliegue.
