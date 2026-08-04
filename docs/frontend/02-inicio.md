# Vista 02 - Inicio

## Objetivo

Dar al usuario una lectura diaria de lo que requiere atención sin duplicar el explorador de Oportunidades ni el análisis detallado de Mercado.

## Usuario

Proveedor que abre BuenaPro para revisar su lista guardada, detectar cierres próximos y continuar postulaciones.

## Ruta

```text
/
```

## Jerarquía

1. Saludo y empresa activa.
2. Aviso de oportunidades guardadas que cierran en las próximas 48 horas.
3. Resumen actualizado al cargar:
   - guardadas vigentes
   - postulaciones en preparación
   - mercado histórico analizado
4. Oportunidades guardadas, ordenadas por cierre.
5. Contexto lateral de actividad histórica y competidores frecuentes.

## Reglas funcionales

- La mesa principal consulta únicamente `saved_contracts`; guardar no crea una postulación ni altera el estado del match.
- Los conteos y listados se calculan desde PostgreSQL en cada carga de la vista y del endpoint `GET /api/dashboard`.
- Inicio no incluye búsqueda ni filtros avanzados; esas tareas pertenecen a `/feed` y `/mercado`.
- La actividad y los competidores respetan los segmentos CUBSO del perfil activo.
- Sin guardadas se muestra una orientación breve hacia Oportunidades.
- Sin perfil se muestra una acción para completar `Mi empresa y radar`.

## Navegación

- `Inicio` es el primer destino del sidebar y de la navegación inferior mobile.
- El pie del sidebar muestra la empresa activa y abre `/perfil`.

## Criterios de done

- Solo aparecen oportunidades guardadas en la mesa principal.
- La alerta de 24/48 horas se calcula sobre guardadas vigentes.
- Cada métrica enlaza con la lista o análisis correspondiente.
- El perfil permanece visible al fondo del sidebar en desktop.
- No existe overflow horizontal en desktop ni mobile.
