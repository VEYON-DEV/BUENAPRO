# DIN: 30 oportunidades adicionales de dos puntos, solo SEACE

El scope inmutable `TWO_DOT_SEACE_30_SCOPE` autoriza hasta 30 publicaciones nuevas
de `seace_prod2`, con afinidad de exactamente dos puntos y documentos ya leídos.
La carpeta exclusiva es `tmp/dinaut-two-dot-seace-30-20261016`. Las carpetas de los
33 casos anteriores y del lote mixto de 50 no se reutilizan ni se copian.

`scripts/run_dinaut_two_dot_seace_matches.py` prepara sin inferencias por defecto.
La preparación valida el hash del freeze y del selector PROD2, el perfil DIN,
las fuentes de todos los casos, su readiness, el máximo de 30 y la ausencia de
matches, reservas, jobs o ledger anteriores. El selector conserva el SQL canónico
de dos puntos con `published_at`; `selector_sql_sha256` contiene únicamente la
clave `seace_prod2`.

La ejecución explícita reutiliza el protocolo ya validado: estado JEV como JSON
compacto, requisitos canónicos completos, resumen original restaurado para las
explicaciones de Flash-Lite, normalización ISO del límite JSON y CAS de perfil,
líneas comerciales, fuente y reserva. No modifica prompts ni documentos. Las
explicaciones usan hasta dos casos por request y hasta 16 requisitos entre dos
casos; un caso individual mayor se conserva íntegro. Las citas TDR exactas se
separan de referencias D/P mediante el normalizador y proof privado existentes.

Las nuevas reservas y eventos llevan el prefijo `dinaut_two_dot_seace_30_`; el
evento de publicación es `dinaut_two_dot_seace_30_analysis`. Ese evento solo acepta
PROD2, fit 2 y publicación nueva. La fuente se comprueba también antes de
lecturas, dispatch, reutilización de caché y publicación. Se conserva el mismo
lock PG de DIN para impedir coordinadores simultáneos entre cohortes.

Antes de ejecutar, confirmar y subir el código operativo, congelar las entradas,
revisar las pruebas offline y el preflight vivo de las 30 fuentes. El wrapper fija
la carpeta y `gemini-3.1-flash-lite`; `--limit` admite de 1 a 30 y `--jev-only`
delega solo la etapa de puntaje al mismo scope. Un piloto y su continuación deben
usar los recibos de esta carpeta. Un rechazo o envío incierto detiene el proceso;
no autoriza copiar carpetas, borrar recibos ni repetir llamadas pagadas.

Esta extensión no despliega servicios, migra datos, activa workers o colas,
reemplaza análisis ni autoriza contratos menores. La finalización operativa exige
verificar las 30 publicaciones, sus recibos y costos únicos, y la conservación de
las cohortes y fuentes anteriores. Las entradas pendientes se conservan para
reanudar sin repetir pagos.

## Ejecución verificada del 16 de octubre

El operador local confirmado y subido `44592c0c56bf980328767204034540d40eb9c49d`
publicó las 30 oportunidades nuevas, sin reemplazos ni contratos menores nuevos.
El preflight productivo verificó perfil, presencia de 43 documentos de empresa,
11 líneas comerciales y las 30 fuentes; el piloto se reutilizó en la continuación sin
repetir su llamada. Los manifiestos de las cohortes previas de 33 y 50 se
reconstruyeron idénticos y sus 395 artefactos protegidos permanecieron intactos.

El readback PG REPEATABLE READ READ ONLY del 16/10 a las 05:21:55 UTC verificó
30 evaluaciones DONE, sus fuentes/provenance y 46 eventos únicos de proveedor.
Los 97 matches anteriores (44 PROD2 y 53 menores) conservaron sus filas completas.
La reconstrucción independiente desde recibos originales coincide con las
asignaciones publicadas: 30 JEV por US$0.036492204 y 16 Flash-Lite por
US$0.08537075; total calculado US$0.121862954. No hubo rechazos de proveedor ni
envíos pendientes de respuesta; la factura del proveedor no se ha conciliado.

Los puntajes van de 15 a 67 y todos siguen provisionales; 19 tienen cobertura
parcial. Las 30 lecturas fuente conservan su revisión y en el freeze sus 30 fechas
de cierre eran desconocidas. No se certifica vigencia ni aprobación documental.
El scoring preserva todos los 101 requisitos sustantivos seleccionados y excluye
papeleo administrativo del puntaje. No hubo relecturas PDF, nuevas ingestas,
embeddings, migraciones, notificaciones ni cambios de servicios o frontend;
la imagen web continúa siendo `buenapro-web:score-first-05db6c9`.

La API productiva confirmó las 30 publicaciones tanto en lista como en detalle,
con 74 puntajes SEACE totales de DIN y orden descendente; fueron 32 GET, sin
escrituras ni llamadas a modelos. El cotejo exige el SHA raw del payload verificado
en PG y su backup antes de comparar el árbol JSON de la API. JavaScript serializa
flotantes integrales como `47.0` en `47`: se acepta esa equivalencia numérica
exacta, con tipos booleanos/texto separados y sin redondeo ni tolerancia. Se
conservan ambos hashes raw y los diagnósticos originales; 104 pruebas puras
verifican también rechazo de cambios reales y manipulación de los respaldos.

El reporte agregado `docs/dinaut-two-dot-seace-30-results-20261016.json` vincula los
SHA de inputs, manifiesto y readbacks. Solo se retiraron dos bytecodes propios
innecesarios (59.292 bytes); entradas congeladas, respuestas pagadas, respaldos,
diagnósticos y recibos se retienen para revisar evidencia y conciliar la factura.
