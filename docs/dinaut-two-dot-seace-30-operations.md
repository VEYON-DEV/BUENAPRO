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
