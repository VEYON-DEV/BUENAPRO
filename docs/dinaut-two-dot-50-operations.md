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
