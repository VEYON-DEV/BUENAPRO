# Publicación desde el código local

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
