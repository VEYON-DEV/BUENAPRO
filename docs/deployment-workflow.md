# Publicación desde el código local

## Categorías preliminares PROD2: 20 de octubre de 2026

Columna Categoría y filtro simple publicados en web `ad744300c46cd2fa2c63885f2ec6699292575a88`, imagen `buenapro-web:title-categories-ad74430` / `sha256:c9c76f6a92abb0e1a64b1b8ddf761ae2c33525c1797d8b9c8e64d67a23f48b81`. Solo web fue recreada; 602 hashes Git, 51 variables de entorno y diez contenedores ajenos verificados. La migración 0046 y el lote JEV por título se ejecutaron por separado con autorización humana: 1.135 filas, 1.018 solicitudes únicas, US$0.040442136 calculados y cero inciertos de este lote. Los 144 matches anteriores quedaron idénticos.

Pasaron 39 pruebas Python, 195 Node/2 skip, TypeScript, arquitectura y builds; se revisaron 18 capturas locales/productivas. El guard productivo estricto conserva su fallo por bloquear analítica externa; diagnóstico acotado con SHA512 del stub confirma que la alerta SRI corresponde a esa sustitución y los checks de aplicación/filtros están aprobados. Los pendientes reales son 322 títulos por confirmar, una asignación dudosa de la muestra y un registro sin ficha enriquecida.

Se retiraron 882,409,021 bytes lógicos propios, sin sumar imágenes compartidas ni limpiezas previas. Entradas, recibos, captura final e imagen rollback se conservan. Detalle en `docs/prod2-title-categories-execution-20261020.{md,json}` y evidencias privadas en `tmp/prod2-title-categories-20261020/`.

## Consolidación parcial de lectores: 19 de octubre de 2026

Se retiraron únicamente los cinco contenedores legacy detenidos, conservando
sus registros comprimidos, imágenes y volúmenes ajenos. Los túneles locales de
los lectores obtienen ahora la configuración desde `docker-web-1`.

La corrección del diagnóstico PG inicial de PROD6 se construyó desde Git limpio
del commit remoto `595070655a9977aba734309e802300dcac5ff0f3`. Imagen
`buenapro-worker-prod6:pg-guard-5950706` /
`sha256:deead1c59babac691ae98d43f50ac10fa6a049afc91e7db49d6011b85753f134`.
Se verificaron seis hashes runtime y la importación sin red. Los 21 recibos que
faltaban en la VM se conciliaron con las 24 lecturas actuales exactas en PG;
Mac y VM conservan los mismos 73 hashes.

PROD2 cuenta ahora con servicio dedicado opt-in, construido desde Git limpio
remoto `2b1b0d6ed67f4c3abd013fea14048daa1d2e5209`. Imagen
`buenapro-worker-prod2:service-2b1b0d6` /
`sha256:f01641f05721dc18f118c95e8936d361beaa28c274d9f3f14c3024997f405f7f`.
Los 127 archivos Python instalados coinciden con Git. Pasaron dry-run y smoke
sin red ni credenciales: el hold mínimo `version=1, operator_hold=source_403`
guarda el bloqueo y sale antes de abrir PG, fuentes, colas o modelos.
Para habilitarlo se requieren configuración privada explícita de alcance,
ventana en hora Perú, primer epoch, límites finitos de ciclos/lecturas y claves.
El servicio solo avanza tras validar el reporte, la cola vacía y las guardas;
no adopta planes históricos ni reintenta ciclos incompletos automáticamente.

**Ambos contenedores están creados sin iniciar**: la fuente devuelve HTTP 403
desde la VM en PROD2 y PROD6, y también desde el Mac en PROD6. El HTTP 200 del
formulario PROD2 desde el Mac solo acredita ese bootstrap. Las 209 pruebas
offline aprobadas y las imágenes preparadas no acreditan recuperación del acceso
ni lectura activa. La tarea de activar ambos lectores permanece abierta.

Web, PG, Redis, configuración privada y contadores Redis anteriores siguen
intactos; login 200, PG/Redis saludables y cero coordinadores al corte del
19/10 a las 21:47 Lima (20/10 a las 02:47 UTC). La comprobación PG/Redis fresca
posterior confirma 307 jobs PROD6 done, cero abiertos, 73 hashes conservados y
contadores históricos PROD2 idénticos. No hubo modelos, migraciones, compras,
colas borradas ni originales nuevos en R2. Rollback PROD6:
`buenapro-worker-prod6:http-recovery-f2767e8`. Checkouts de build retirados:
182.818.324 bytes lógicos; bytecode local propio retirado: 210.535 bytes;
smoke temporal PROD2: 109 bytes. Son tamaños de artefactos, no diferencia física
total del disco; las dos imágenes nuevas permanecen disponibles. Las
entradas pendientes y los recibos privados permanecen fuera de Git.
Informe `docs/readers-consolidation-20261019.json` y evidencias privadas en
`tmp/readers-consolidation-20261019/`.

## Orden de fuentes del Cronograma: 16 de octubre de 2026

El Cronograma muestra **Buscador SEACE primero y Contratos menores después** en
desktop, laptop y móvil. La selección, los iconos y las fechas conservadas al
cambiar de fuente mantienen su comportamiento. Los enlaces tienen altura mínima
de 44 px, corrigiendo el target móvil de 38 px encontrado en la primera pasada.

Código publicado `f8504fb317fbaf00d0b71df1a09a6a87edfb2fd9`, imagen
`buenapro-web:timeline-tabs-f8504fb` /
`sha256:27a717b1e1b2f4fddbf1e97c339e3123419050f30b848fa384bce67c8988df1a`.
El build procede de Git limpio; etiqueta OCI y 588 hashes de runtime coinciden.
Pasaron 14 pruebas focales, TypeScript, arquitectura de 16 dominios y build local aislado.
Seis capturas locales y seis productivas autenticadas comprueban ambos estados
activos en tres tamaños, navegación por teclado, foco, fechas y ausencia de
overflow/errores. El principal revisó manualmente las seis locales y tres
productivas representativas contra el diseño Glass y la captura del pedido.

Solo se recreó web a las 07:43:53 Lima, con salud local 200. Entorno, configuración
material, monturas, puertos, archivos privados y ocho contenedores ajenos se
conservaron. La validación no escribió negocio ni llamó modelos/fuente. Las reglas
de inclusión del Gantt y el filtro de ofertas vencidas no cambiaron.

El primer build se inició con ubuntu, que no podía leer dos objetos root con modo 0400 y
Git los reportó como corruptos. La descompresión y SHA-1 confirmaron que estaban
íntegros; `git fsck --full --no-reflogs` con la cuenta de publicación devolvió 0.
Se repitió el build con esa cuenta, preservando el recibo del intento fallido,
sin reemplazar objetos ni cambiar sus permisos. Ningún servicio se había activado
en ese intento.

Se retiraron únicamente los worktrees propios de QA/build y dos capturas
iniciales duplicadas: 581.526.747 bytes lógicos (489.716.759 locales de checkout,
91.308.831 remotos y 501.157 de capturas). Las capturas finales, recibos compactos,
entradas activas de PROD6 e imagen rollback
`buenapro-web:rollback-c3aa3a4-before-timeline-tabs-f8504fb` se conservan.
Evidencia en `tmp/timeline-tab-order-20261016/` y
`/home/ubuntu/buenapro-releases/timeline-tabs-f8504fb-receipts/`.

## Texto completo del detalle: 16 de octubre de 2026

Web `c3aa3a4b5515697ec7324339efd91fc41b7ed00c` está publicada con
`buenapro-web:readable-detail-c3aa3a4`, imagen
`sha256:a3f7e539a9daf220043d2210a01c64ffd277a1242a0aacba5b44e06ca5d9b1ec`.
El resumen usa el texto completo ya guardado y las brechas PROD2 permiten abrir
el requisito íntegro; su vinculación evita confiar en posiciones históricas de
facets. No cambian los análisis ni los puntajes.

Pasaron 229 pruebas Node (5 omitidas), TypeScript, arquitectura de 16 dominios y
builds de 61 páginas. Se revisaron seis capturas locales y seis productivas en
desktop/laptop/mobile, con expansión por teclado y sin overflow ni errores.
El caso 1047247 conserva el resumen de 582 caracteres y las descripciones de
1497/334/411 caracteres. Las consultas locales usaron PostgreSQL read-only,
credenciales de modelos desactivadas y un guard de red/escrituras.

La imagen se construyó desde Git limpio y sus 588 hashes coinciden. Solo se
recreó web; ocho contenedores ajenos, los valores de entorno y archivos privados
se conservaron. La configuración material y HostConfig normalizada también
coinciden. Salud local `/login` devolvió 200 y la ficha pública autenticada pasó
QA real. El GET público desde `urllib` de la VM devolvió 403 en el edge; esa
limitación se conserva y no se presenta como una comprobación 200. No hubo
migraciones ni llamadas a modelos. Rollback conservado:
`buenapro-web:rollback-05db6c9-before-readable-detail-c3aa3a4`.

Recibos y capturas privados: `tmp/readable-detail-20261016/` y
`/home/ubuntu/buenapro-releases/readable-detail-c3aa3a4-receipts/`.

El flujo de BuenaPro es **local → validación → commit → push → checkout del commit en producción → build → verificación**. El repositorio Git es la fuente de verdad del código. Los archivos privados de configuración, entradas pendientes y recibos no son parte del código público.

## Preparación local

1. Revisar `git status`, el diff y `tasks/README.md`. Incluir los archivos nuevos que necesita el runtime y sus pruebas.
2. Ejecutar las pruebas relevantes, TypeScript, `npm run check:frontend-architecture` y el build web. Las pruebas de modelos usan mocks; los contratos productivos PG/Redis requieren opt-in aparte.
3. Revisar el contenido staged. No incluir `.env.local`, credenciales, `tmp/`, recibos privados, capturas de documentos de empresas o cachés de compilación.
4. Crear el commit y subirlo con un push normal. No forzar la rama ni desplegar un estado sin commit.

## Producción

1. Leer la revisión y el estado Git del servidor. Preservar cualquier configuración privada o cambio operativo antes de actualizar archivos en conflicto.
2. Obtener el commit desde `origin`, actualizar la rama con fast-forward y crear un worktree limpio de esa revisión para el build. Esto evita incorporar carpetas privadas del servidor.
3. Construir con `infra/docker/web.Dockerfile` y `--build-arg VCS_REF=<commit>`. La imagen declara `org.opencontainers.image.revision`.
4. Conservar la imagen anterior como rollback y recrear únicamente `web` con `--no-deps --no-build`. No reiniciar workers, PostgreSQL o Redis por una publicación frontend.
5. Comprobar salud, revisión de imagen, hashes del runtime contra Git, lecturas autenticadas y estados de UI. Conservar los recibos compactos fuera de Git.

No ejecutar migraciones, compras, inferencias, búsquedas o reintentos para comprobar un despliegue web. Un flag nuevo queda desactivado salvo autorización específica. `PROD2_OFFICIAL_DOCUMENTS_ENABLED=false` conserva el acceso histórico; Concursos SEACE permanece retirado.

Tras verificar el despliegue, actualizar las tareas y subir la documentación de cierre. Si este segundo commit modifica únicamente documentación/tareas, actualizar el checkout de producción y verificar que sus archivos runtime siguen idénticos a la imagen del commit de código registrado; no hace falta otra compilación.

## Sincronización verificada del 13 de octubre de 2026 (Lima)

El código acumulado quedó publicado en `main`, commit `d0468633fc3f5148bafb5e7b92356138c9edfc91`. La imagen web `sha256:ed8c43fba0ac0b03c03128500316130f282c1566517762f1506c63eb53d6f7cd` se construyó con el Dockerfile canónico desde un worktree limpio de ese commit. Su etiqueta OCI y los hashes de 542 archivos de la web y sus prompts coinciden con Git.

La validación local pasó 3371 pruebas Python (27 omitidas), 213 pruebas Node (5 omitidas), TypeScript, arquitectura de 16 dominios y build de 61 páginas. Las lecturas autenticadas posteriores conservaron exactamente 15 evaluaciones guardadas y sus estados provisionales; el aislamiento de perfil devolvió 404. Ocho capturas productivas desktop/mobile no mostraron overflow ni errores de cliente. No se ejecutaron modelos, migraciones ni escrituras de negocio como prueba del despliegue.

La aplicación conservó sus 47 variables configuradas y el estado de ocho contenedores ajenos a la web. La comparación inicial del entorno detectó que la imagen anterior añadía `NEXT_TELEMETRY_DISABLED=1`; se restauró esa única opción en `.env.local` antes de la verificación final. Las otras configuraciones privadas se conservaron. Concursos SEACE sigue retirado y el nuevo acceso documental oficial permanece bajo su flag desactivado por defecto.

El worktree remoto de compilación ya se retiró: se liberaron 90796380 bytes remotos y 238417 bytes de temporales locales propios. Git, la imagen anterior de rollback, el stash operativo y los recibos compactos permiten reproducir o revertir la publicación. Las capturas y las entradas privadas o pendientes permanecen fuera del repositorio público. Los recibos de validación, activación, QA y limpieza están en `tmp/git-sync-local-production-20261013/`.

## Publicación del 14 de octubre de 2026 (Lima)

Los últimos cambios locales de perfil maestro-detalle, guardadas PROD2/cronograma y layouts responsive se publicaron en `main`, commit `72e311404bcf0861048a2c46d35a4ada91697b89`. La revisión corrigió IDs fuera del rango BIGINT, fallback de tabla ausente demasiado amplio, respuestas tardías del guardado, estado/foco del editor y claves repetidas del ranking. Los cambios no recalculan evaluaciones ni alteran el contenido del perfil guardado.

Pasaron 233 pruebas Node (5 omitidas), TypeScript, arquitectura de 16 dominios y build de 61 páginas del source confirmado. El build local aislado usó webpack; producción se construyó desde un worktree Git limpio con el Dockerfile canónico/Turbopack. La imagen `sha256:f32334985876d245b3a6c7a09d88246bb08ba35ef0dc5f4c5f828bae608ec8cf` declara el commit de código y conserva exactamente los 546 hashes comprobados.

Se verificaron 34 capturas locales y 22 productivas, incluidos móvil y pantalla girada; las pruebas de transporte simulado confirmaron que una respuesta tardía no altera la siguiente oportunidad. Quince análisis persistidos conservaron sus puntajes provisionales, breakdown y acciones. Las comprobaciones de base de datos fueron de solo lectura: la migración 0044 ya estaba aplicada, y 0042 continúa pendiente. No se ejecutaron nuevas migraciones, modelos ni escrituras de negocio para publicar.

La salud web devolvió 200, la configuración privada y el entorno permanecieron iguales y ocho contenedores ajenos a web conservaron identidad/inicio/política. Se retiraron únicamente temporales propios: 410602996 bytes locales y 90867094 remotos. El servidor local del usuario siguió funcionando; capturas, recibos, imagen anterior y entradas pendientes permanecen disponibles. Evidencia privada en `tmp/latest-local-release-20261014/`.

## Puntajes del perfil primero: publicación del 15 de octubre de 2026 (Lima)

El commit web `05db6c902e7bc11d1a0f888627e62ac84dcba584`, subido en
`codex/dinaut-three-dot-matching`, se construyó desde un worktree Git limpio con el
Dockerfile canónico. La imagen `buenapro-web:score-first-05db6c9`,
`sha256:919b9e38dc63d017d0d7135ca27f7e24697bb4079fffd6448d131a2316c93d42`,
declara esa revisión OCI y sus 585 hashes de web/prompts coinciden con Git.

Ambas fuentes ordenan los puntajes válidos del perfil activo de mayor a menor
antes de paginar y ofrecen «Con puntaje». El perfil se conserva entre pestañas,
filtros, inspector y detalle; en menores se corrigió la selección de análisis de
otros perfiles. Se mantienen puntajes cero y provisionales y el filtro legacy
`analysis=done`. Pasaron 224 pruebas Node (5 omitidas), TypeScript, arquitectura de
16 dominios y builds de 61 páginas. Doce capturas locales con datos reales y seis
productivas desktop/laptop/mobile no mostraron overflow, errores de cliente ni
escrituras. Se verificaron aislamiento de perfil y orden entre páginas.

Se recreó únicamente web el 16/10 a las 04:14 UTC. Las lecturas de salud local y
pública devolvieron 200. Se conservaron los 51 valores de entorno, archivos
privados, puertos y política de reinicio; ocho contenedores ajenos conservaron
identidad/inicio/restarts. HostConfig coincide exactamente al normalizar el orden
de dos monturas. El recibo previo de Config conservó solo su hash: se acreditó
igualdad material de los campos contrastables, pero no igualdad binaria total ni
una causa concreta para esa diferencia de hash. No hubo migraciones ni reinicios
de workers. La imagen anterior `f3233498…` y su override permanecen como rollback.

El checkout operativo remoto avanzó por fast-forward desde `d613b16` a
`1d984c9`, con su único cambio privado `profile-ingestion.env` intacto. Los commits
posteriores del operador no alteran web/prompts/dependencias/Dockerfile frente a
`05db6c9`; el cierre documental conserva este runtime y esta imagen. La ejecución
autorizada de 50 análisis nuevos fue independiente del despliegue; el readback
final confirma 44 puntajes PROD2 y 53 menores para DIN, sin cambiar los 47
anteriores. Ver `docs/dinaut-two-dot-results-20261015.json`.

Se retiraron solo el checkout local de QA y el worktree remoto limpio de build,
más un temporal propio de diagnóstico: 487494984 bytes locales y 91177066 remotos
(578672050 bytes lógicos en total). Se conservaron entradas congeladas, recibos,
proofs, respaldos, capturas, configuración privada e imagen rollback. Evidencias
en `tmp/score-first-build-20261015/remote-receipts/` y
`tmp/dinaut-two-dot-50-20261015/`.
