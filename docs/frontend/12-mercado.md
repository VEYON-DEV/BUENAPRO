# Inteligencia de mercado

## Objetivo

`/mercado` convierte los resultados culminados de SEACE en evidencia para decidir. Inicio conserva el resumen ejecutivo; esta vista concentra la exploración profunda.

## Alcance

- `Mis mercados` usa los segmentos CUBSO activos del perfil.
- `Todo el mercado` permite explorar sectores fuera del perfil sin modificar su configuración.
- La búsqueda cubre servicio, código, entidad y proveedor.
- La primera línea conserva búsqueda, resultado y departamento; segmento, entidad, año y rango de precio viven en `Filtros avanzados`.
- `Filtros avanzados` se abre automáticamente cuando contiene algún valor activo y sus campos forman parte del mismo formulario.
- `Resumen` muestra tendencia, demanda regional, entidades compradoras y empresas adjudicadas.
- `Contratos` lista resultados históricos y documentos originales disponibles.
- `Empresas` permite abrir `/mercado/empresas/[ruc]` para revisar dónde, cuánto y qué ganó cada proveedor.

## Reglas de datos

- Los desiertos participan en volumen y tasa de riesgo, pero no en métricas de precio.
- El precio mediano y rango central se calculan solo con precios adjudicados disponibles.
- PostgreSQL en la VM es la fuente de verdad; la interfaz no mantiene una copia analítica en el navegador.
- La ubicación se normaliza desde el detalle SEACE y se conserva también en el JSON original.

## Estados

- Sin perfil: `Mis mercados` devuelve un estado vacío y `Todo el mercado` continúa disponible.
- Sin resultados: se conserva el contexto de filtros y se ofrece limpiar la consulta.
- Datos parciales: los campos sin precio, proveedor o ubicación se muestran como no informados, sin inferencias.
