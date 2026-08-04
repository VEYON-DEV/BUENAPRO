# Nueva direccion visual

Esta carpeta contiene la migracion de BuenaPro al sistema **BuenaPro Glass**. No contiene una segunda aplicacion ni una rama visual paralela.

## Fuente de verdad

El orden de autoridad es:

1. [`PRODUCT.md`](../../PRODUCT.md): usuario, propósito y principios de producto.
2. [`DESIGN.md`](../../DESIGN.md): tokens, materiales, lenguaje visual y reglas de componentes.
3. [`generated-glass/`](./generated-glass/README.md): diez referencias aprobadas y su asignacion por vista.
4. [`docs/stitch/buenapro-functional-context.md`](../stitch/buenapro-functional-context.md): comportamiento, datos y recorridos sin decisiones visuales.
5. [`docs/frontend/architecture.md`](../frontend/architecture.md): propiedad por dominios, componentes y estilos.
6. Specs de cada vista en [`docs/frontend/`](../frontend/README.md).
7. [`reference-register.md`](./reference-register.md): origen de referencias Stitch.
8. [`migration-map.md`](./migration-map.md): orden de adopción y definición de terminado.

Si una captura contradice `DESIGN.md`, prevalece `DESIGN.md`. Las capturas sirven para estudiar composición, densidad o un patrón concreto; no son especificaciones completas.

## Nombre y alcance

`new-style` es un espacio transitorio de preparación. Cuando todas las vistas adopten el sistema, los documentos de esta carpeta seguirán funcionando como registro histórico y de decisiones, mientras `DESIGN.md` permanecerá como norma viva.

La fase actual aplica BuenaPro Glass sobre los tokens, primitives y vistas productivas. El catálogo interno `/design-system` permite validar los componentes compartidos antes de incorporarlos a cada dominio.

## Flujo para rediseñar una vista

1. Leer el contexto funcional y la spec de la ruta.
2. Leer `DESIGN.md` completo.
3. Consultar el contrato visual de la vista en `migration-map.md`.
4. Elegir únicamente referencias aprobadas para el patrón que se va a resolver.
5. Migrar primero primitives compartidos si la vista los necesita.
6. Implementar con datos reales y todos los estados.
7. Revisar desktop amplio, laptop y mobile.
8. Guardar capturas y actualizar `tasks/05-frontend.md`.

## Regla de aislamiento

- No crear archivos `New`, `V2` o `AppleStyle` dentro de features productivos.
- No duplicar una vista completa para probar colores.
- Las variantes se exploran en una rama y se integran por dominio.
- Los componentes existentes se evolucionan mediante tokens y contratos estables.
- No mezclar el sistema anterior y el nuevo dentro de una misma pantalla terminada.
