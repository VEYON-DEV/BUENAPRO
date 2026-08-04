# AGENTS.md - BuenaPro

## Regla principal

Cada vez que un agente complete una tarea del proyecto, debe marcar el checkbox correspondiente en `tasks/`.

No se considera terminada una tarea si el checkbox no fue actualizado.

## Flujo obligatorio

1. Antes de trabajar, revisar `tasks/README.md`.
2. Identificar el archivo de tareas correspondiente:
   - `tasks/01-database.md`
   - `tasks/02-worker.md`
   - `tasks/03-backend-web.md`
   - `tasks/04-qa.md`
   - `tasks/05-frontend.md`
3. Implementar la tarea.
4. Validar con prueba manual o automatizada.
5. Marcar el checkbox de la tarea completada.
6. Si aparece una tarea nueva necesaria, agregarla al archivo adecuado.

## Uso de subagentes

Usar subagentes cuando una tarea sea repetitiva, paralelizable o requiera revisar muchos elementos similares.

Casos esperados:

- probar varios endpoints o variantes de payload
- revisar multiples PDFs, JSONs o golden cases
- auditar varios archivos de tareas o documentacion
- validar varias rutas/API endpoints
- comparar modulos similares del worker, backend o UI

Reglas:

- el agente principal sigue siendo responsable de integrar resultados
- el agente principal valida antes de marcar checkboxes
- no delegar decisiones de arquitectura sin revisar el resultado
- no marcar una tarea como completa solo porque un subagente reporto avance

## Criterios de done

Una tarea solo puede marcarse como completa si:

- el cambio esta implementado
- se verifico que funciona
- no rompe el flujo existente
- se actualizo documentacion si aplica
- no quedan secretos hardcodeados

## Seguridad

- No guardar API keys en el repo.
- Usar `.env.local` para secretos.
- No loggear datos personales innecesarios.
- No indexar DNIs salvo que sea estrictamente necesario para un requisito.

## Arquitectura

Principio tecnico:

```text
LLM para leer y estructurar.
Reglas para comparar.
LLM opcional para explicar.
```

PostgreSQL es la fuente de verdad. R2 guarda previews/documentos auxiliares. SEACE conserva el PDF original descargable por URL.

## Alcance MVP de ingesta

El worker inicial no debe intentar procesar todo SEACE.

Alcance operativo inicial:

- estado SEACE: solo `Vigente` (`lista_estado_contrato=2`)
- objeto SEACE: solo `Servicio` (`lista_codigo_objeto=2`)
- segmentos CUBSO: solo los configurados para tecnologia, transporte y legal

Los segmentos CUBSO no deben quedar hardcodeados en la logica. Deben salir de configuracion, base de datos o una lista versionada facil de cambiar.

## Desarrollo frontend

Antes de crear o modificar una vista, leer:

- `PRODUCT.md`
- `DESIGN.md`
- `docs/new-style/README.md`
- `docs/new-style/generated-glass/README.md`
- la imagen de la vista correspondiente en `docs/new-style/generated-glass/`
- `docs/frontend/architecture.md`
- las referencias Stitch registradas para el patron concreto
- `docs/frontend/README.md`
- el documento especifico de la vista en `docs/frontend/`
- `tasks/05-frontend.md`
- `.agents/skills/frontend-design/SKILL.md` cuando la tarea cambie UI, layout, colores, tipografia, componentes visuales o experiencia de usuario
- `.agents/skills/web-design-guidelines/SKILL.md` cuando la tarea implique auditar, revisar o cerrar una vista frontend
- `~/.codex/skills/impeccable/SKILL.md` cuando se vaya a diseñar, rediseñar, auditar, pulir o criticar cualquier interfaz frontend

Reglas:

- trabajar por dominios dentro de `apps/web/features` siguiendo `docs/frontend/architecture.md`
- mantener `apps/web/app/*/page.tsx` delgado e importar cada pagina desde `@/features/<domain>`
- cada vista vive en `features/<domain>/pages/<PageName>/`
- cada componente no trivial vive en `features/<domain>/components/<ComponentName>/`
- cada vista y componente visual debe tener `Component.tsx`, `Component.module.css` e `index.ts`
- exponer solo la API publica necesaria desde `features/<domain>/index.ts`
- no importar carpetas internas de otro dominio; usar su `index.ts`
- mantener primitives sin negocio en `apps/web/components/ui`, cada uno en su propia carpeta
- no crear estilos globales para componentes nuevos
- conectar cada vista contra los endpoints documentados en su spec
- ejecutar `npm run check:frontend-architecture` antes de cerrar cambios frontend
- construir y revisar primitives compartidos en `/design-system` antes de crear una variante local
- marcar checkboxes en `tasks/05-frontend.md` al completar

## Estándar visual obligatorio

La UI de BuenaPro debe tratarse como producto serio, no como maqueta funcional. Una vista que compila pero se ve cruda, generica o improvisada no esta terminada.

Mentalidad obligatoria para trabajo frontend:

- ser perfeccionista con jerarquia, espaciado, tipografia, ritmo, estados y densidad visual
- no aceptar el primer resultado funcional como resultado final
- hacer una segunda pasada de diseño despues de conectar datos reales
- reducir ruido visible: si una vista tiene demasiados controles, colapsar, agrupar o priorizar
- evitar tablas administrativas crudas cuando el usuario necesita decidir rapido
- cuidar la primera pantalla: debe comunicar producto, prioridad y accion sin parecer un panel tecnico
- los componentes deben sentirse parte de un sistema propio, no de un template SaaS generico
- usar iconografia consistente y con intencion; no depender de iconos o estilos por defecto que hagan la app indistinguible
- usar `DESIGN.md` como contrato de implementacion y `docs/new-style/generated-glass/` como referencia visual aprobada; ambos deben mantenerse alineados
- tomar de las referencias Glass su materialidad, radios, iconografia, profundidad, jerarquia y densidad; no reducir la migracion a un cambio de color
- seguir `docs/new-style/migration-map.md`; no crear arboles paralelos de componentes o vistas con sufijos `New` o `V2`
- si una vista no se reconoce como parte de la coleccion BuenaPro Glass, no esta lista
- usar glassmorphism en la capa ambiental y superficies solidas en tablas, formularios y edicion; nunca aplicar blur a texto o datos densos
- usar radios amplios de forma consistente segun `DESIGN.md`; evitar mezclar contenedores rectos heredados con controles capsula nuevos
- usar violeta para seleccion, negro para la accion primaria y colores pastel solo para estados semanticos
- usar Lucide como familia de iconos; no crear SVG manuales nuevos cuando exista un icono equivalente
- para producto/dashboard, preferir familiaridad excelente sobre rareza sin proposito: el usuario debe confiar en la interfaz como herramienta de trabajo

Checklist de cierre visual para cualquier vista:

- layout revisado en desktop amplio, laptop y mobile
- textos largos probados con datos reales
- estados empty/loading/error contemplados
- filtros y acciones principales no saturan la pantalla
- contraste y foco de teclado revisados
- no hay cards dentro de cards ni bordes innecesarios
- no hay texto cortado, solapado o botones deformados
- la vista se compara contra el brief visual y se ajusta si se siente generica
- la vista se compara lado a lado contra su PNG en `docs/new-style/generated-glass/`
- shell, radios, materiales, controles, iconos y densidad coinciden con el lenguaje de la referencia
- la similitud de composicion es obligatoria: deben coincidir la cantidad y prioridad de controles visibles, las columnas, la presencia del inspector y las proporciones generales; cambiar solo colores, radios o sombras no cuenta como rediseño
- si la captura implementada conserva mas controles, columnas o ruido que la referencia, simplificar antes de cerrar aunque la funcionalidad ya exista; las opciones secundarias pasan a revelado progresivo

## Capturas obligatorias para frontend

Toda tarea frontend que modifique una vista, layout o componente visual debe terminar con captura de pantalla.

Reglas:

- abrir la app local despues de implementar
- tomar al menos una captura desktop de la vista modificada
- tomar tambien una captura laptop cuando la vista tenga filtros, tablas o panel lateral
- si la vista es responsive o publica informacion densa, tomar tambien una captura mobile
- revisar la captura antes de marcar el checkbox
- si la captura evidencia una UI pobre, desbalanceada, saturada o distinta al diseño esperado, corregir y repetir captura
- no marcar la vista como completa solo porque compila; la captura real debe sostener una comparacion visual razonable con la referencia asignada
- guardar o referenciar la evidencia de QA cuando aplique en `tasks/05-frontend.md` o `tasks/04-qa.md`
- si el entorno no permite capturas, no marcar QA visual como completo; documentar explicitamente el bloqueo

Herramientas esperadas para capturas:

- Browser Plugin / navegador interno si esta disponible
- Playwright si esta instalado o si el proyecto ya lo usa
- screenshot manual del navegador como fallback

## Uso de skills de UI/UX

Para crear o rediseñar UI:

1. Leer y aplicar `.agents/skills/frontend-design/SKILL.md`.
2. Leer y aplicar `~/.codex/skills/impeccable/SKILL.md`; para BuenaPro usar su registro `reference/product.md` por tratarse de app/dashboard.
3. Ejecutar `context.mjs` de Impeccable una vez por sesion para recoger `PRODUCT.md` y `DESIGN.md`.
4. Consultar `docs/new-style/reference-register.md` y usar solo referencias aprobadas para el patron concreto.
5. Definir una direccion visual concreta antes de codear: paleta, tipografia, layout, elemento distintivo y reglas de densidad.
6. Construir contra esa direccion, no solo contra funcionalidad.
7. Criticar el resultado con captura y ajustar.

Para revisar UI antes de cerrar:

1. Leer `.agents/skills/web-design-guidelines/SKILL.md`.
2. Leer `~/.codex/skills/impeccable/SKILL.md` y aplicar su slop test sobre los archivos modificados.
3. Aplicar sus reglas sobre los archivos modificados.
4. Reportar o corregir hallazgos relevantes antes de marcar la tarea como completa.
