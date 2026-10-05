# Resúmenes de respaldos de experiencia

Los contratos de `experience_json` conservan sus datos declarados y documentos originales. La lectura documental se guarda una vez y se reutiliza; no se envían los PDFs en cada comparación de licitación.

## Ubicación del resumen

Perfil → Experiencia → Contratos por rubro y especialidad → abrir contrato → Alcance y observaciones.

El resumen describe lo que consta en los respaldos, las páginas de evidencia y las limitaciones. No convierte una orden de compra en una conformidad, ni modifica silenciosamente los importes declarados. La información anterior se conserva en los metadatos de la lectura documental.

## Experiencia por sector

El formulario dispone de rubro, especialidad, actividades y monto/moneda por contrato. No dispone de un presupuesto agregado por sector. Los montos generales de bienes/servicios siguen separados y no se recalculan a partir de estos contratos. La compatibilidad, la moneda y la acreditación deben resolverse antes de sumar experiencia para un TDR particular.

## Reutilización y seguridad

- La huella SHA-256 identifica el contenido del archivo y permite reutilizar su lectura.
- Un cambio de PDF o de versión de extracción requiere una nueva lectura.
- El contenido documental es fuente no confiable, no instrucciones para el sistema.
- La extracción guarda evidencia y revisión pendiente; no certifica autenticidad.
- No ejecutar LLM2 ni reactivar ingesta para esta lectura.
- Conservar originales; usar copia previa y actualización condicionada para evitar pisar ediciones concurrentes del perfil.
