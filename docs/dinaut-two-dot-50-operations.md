# DIN: cohorte de 50 oportunidades con dos puntos

El ejecutor opt-in reutiliza el motor del lote de tres puntos. Su carpeta única es
`tmp/dinaut-two-dot-50-20261015`; conserva las 50 identidades congeladas (25 SEACE y
25 contratos menores), exclusivamente nuevas y con lectura utilizable. Las
entradas, recibos y respaldos privados permanecen fuera de Git.

Preparación local, sin PG ni inferencias:

```sh
.venv/bin/python scripts/run_dinaut_two_dot_matches.py
```

Después de revisar los tests, confirmar el código operativo en Git y ejecutar el
preflight de lectura sobre las 50 fuentes y el perfil actuales, el piloto explícito
y la continuación usan la misma carpeta y los mismos recibos:

```sh
.venv/bin/python scripts/run_dinaut_two_dot_matches.py --execute --limit 1
.venv/bin/python scripts/run_dinaut_two_dot_matches.py --execute
```

`--jev-only` permite completar únicamente la etapa reservada de puntaje. Se usa JEV
`jev-1.13.0` con el estado serializado como JSON compacto y recomendaciones de
`gemini-3.1-flash-lite` mediante la API Google existente. No cambia modelos por un
error. Las recomendaciones usan un esquema OBJECT con claves exactas y referencias
documentales/profesionales D/P; no pueden modificar el puntaje ni los estados.

En esta cohorte JEV recibe descripción, todos los requisitos canónicos, checklist
administrativo y flags; el resumen narrativo de la fuente se conserva en el freeze
y se agrega completo a Flash-Lite mediante `explanation_context(..., scope=TWO_DOT_50_SCOPE)`,
con la misma sanitización de metadatos personales del contexto. La división de
funciones evita repetir participación y narrativa administrativa dentro del
scorer; no elimina requisitos ni reduce el margen del gate de contexto. Es
independiente de la equivalencia exacta del transporte JSON compacto de JEV.

Los lotes contienen hasta dos casos y hasta 16 requisitos entre dos casos. Un caso
individual con más requisitos se conserva íntegro. El conteo gratuito del request
completo precede a la generación; no se recortan requisitos para hacerlo entrar.

La preparación rechaza matches previos y todo historial de reservas, jobs o ledger
que excluyó el congelamiento. Antes de inferir y publicar se verifica de nuevo
perfil, líneas comerciales, fuente, extracción, facetas y propiedad de reservas.
La normalización ISO de datetimes/UUID/Decimal replica exclusivamente el freeze de
dos puntos; el protocolo predeterminado de tres puntos conserva sus hashes.

Una respuesta pagada válida se reutiliza. Un envío incierto o rechazo nuevo detiene
el proceso y conserva evidencias; copiar la carpeta, borrar recibos o volver a
enviar el request no constituye recuperación. La publicación nueva es atómica y no
autoriza reemplazar análisis existentes. Ambas cohortes comparten el lock PG de DIN
para impedir coordinadores simultáneos.

El modo productivo exige un checkout con todos los módulos operativos importados y
el selector SQL canónico confirmados en Git. No despliega servicios, migra datos,
enciende workers ni crea colas. La ejecución operativa solo se cierra al verificar
las 50 publicaciones, sus recibos únicos, costos conocidos e inciertos y la
preservación de fuentes y análisis ajenos.

La continuación se detuvo con 31 publicaciones por una respuesta Flash-Lite
completa HTTP200: en el lote `batch-5acc9e45d7005336`, el caso
`seace_prod2-1029693` colocó `Pág. 14, numeral 21.A` y `Pág. 10, numeral 9` en
`evidence_refs`. Son citas exactas de los requisitos 1 y 3 de la extracción
congelada, no alias de evidencia de la empresa. No representan D14 ni D10. El
recibo pagado conserva 14 943 tokens de entrada y 753 de salida; este error de
decodificación local no autoriza repetir la llamada.

Para esta cohorte, `dinaut_source_citation_refs.py` aplica la regla versionada
`exact_requirement_source_citation_v1` en ambos decoders. Retira solamente una
referencia desconocida idéntica a `detalle.evidencia` del mismo `source_index`.
Preserva todos los alias D/P válidos, textos, acciones, puntajes y estados;
cualquier otra referencia desconocida o cobertura incompleta bloquea el lote.
El comportamiento predeterminado de tres puntos conserva su validador estricto.

Si hubo esta corrección, `bind_saved_batch` crea antes del binding el recibo
privado inmutable `source-citation-decoder-proof.private.json`: vincula los SHA
del request y response originales, el payload JEV de cada caso, las citas
retiradas y los SHA de las recomendaciones derivadas. El decoder individual
exige ese proof para reutilizar un caso corregido. El raw del proveedor, su uso,
los manifiestos y los bindings anteriores permanecen intactos. Este paso no
publica resultados, no acredita evidencia empresarial y no cambia el costo de la
inferencia ya realizada.

## Cierre verificado

Las 50 publicaciones nuevas cerraron el 15/10/2026 (Lima): 25 PROD2 y 25 contratos
menores, sin reemplazar análisis. La auditoría PostgreSQL de solo lectura del
16/10 a las 04:28 UTC reconstruyó puntajes y costos desde los recibos originales,
verificó las fuentes actuales y comprobó que los 47 matches anteriores seguían
idénticos, incluidos los 33 de tres puntos. El modelo respondió a 50 solicitudes
JEV y 31 solicitudes compartidas Flash-Lite; costo calculado conocido
US$0.247578762, sin nuevos rechazos ni envíos pendientes de respuesta. La factura
del proveedor no se ha conciliado. La recuperación del decoder usó la respuesta
pagada guardada y no añadió una llamada.

Los 50 puntajes siguen provisionales; 38 tienen cobertura parcial y 25 conservan
la revisión de la lectura fuente. Al seleccionar había 17 cierres futuros, 11
fechas desconocidas y 22 oportunidades ya cerradas. Estos análisis no convierten
fechas históricas en oportunidades abiertas ni certifican la documentación de la
empresa.

El readback autenticado del frontend desplegado confirma las 50 publicaciones:
44 puntajes almacenados totales en PROD2 y 53 en menores para DIN. Ambas listas
priorizan el puntaje numérico del perfil activo, descendente antes de paginar; la
cápsula «Con puntaje» excluye los casos sin puntaje. Para ver también los menores
cerrados se usan «Todos» y «Cualquier cierre». El despliegue web sigue en
`05db6c9`, imagen `buenapro-web:score-first-05db6c9`; los commits del operador
`a269154` y `1d984c9` no requieren reconstruir esa imagen.

El reporte agregado está en `docs/dinaut-two-dot-results-20261015.json`. Las
entradas congeladas, recibos pagados, proof de decodificación y respaldos se
conservan en la carpeta privada del lote para revisión y trazabilidad. No volver
a ejecutar las 50 ni los 33 históricos para actualizar esta documentación.
