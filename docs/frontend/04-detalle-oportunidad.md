# Vista 04 - Detalle de oportunidad

## Objetivo

Responder: que es, si puedo postular, que me falta y que debo hacer despues.

## Usuario

Proveedor que abrio una oportunidad desde el feed. Necesita decidir en pocos minutos.

## Ruta

```text
/oportunidad/[id]
```

## Jerarquia vigente

1. Encabezado:
   - codigo
   - entidad
   - descripcion corta
   - estado SEACE
   - cierre
   - verdict/score si hay match
2. Franja de decisión:
   - gauge del score únicamente cuando existe evaluación IA
   - veredicto y afinidad
   - cierre
   - progreso de requisitos cubiertos
   - mediana y rango histórico
   - comenzar/continuar postulación y descargar TDR
3. Lectura ejecutiva según el perfil y lo solicitado por la entidad.
4. Brechas prioritarias en rail; las adicionales viven en expansión.
5. Requisitos agrupados por categoría y con evidencia desplegable.
6. Documentos visibles y secciones operativas plegables: ficha, entregables, penalidades, cronograma y actividad.
7. Histórico comparable compacto: tres casos visibles y los demás bajo expansión.
8. Copilot contextual en drawer con preguntas sugeridas, conversación, fuentes y confirmación explícita antes de aplicar cambios.

## Navegacion interna

```text
Decisión y requisitos
Documentos
Histórico
Ejecución
```

La navegación interna usa tabs horizontales reales y conserva la selección en el hash de la URL. La lectura ejecutiva y los requisitos pertenecen a una sola sección para que la decisión conserve inmediatamente su evidencia. Solo se renderiza visualmente una sección central a la vez; los grupos de requisitos, SEACE y datos operativos permanecen plegados hasta que el usuario los solicita.

## Componentes

```text
features/opportunity-detail/components/OpportunityHeader/
features/opportunity-detail/components/SummaryStrip/
features/opportunity-detail/components/VerdictPanel/
features/opportunity-detail/components/RequirementChecklist/
features/opportunity-detail/components/RequirementItem/
features/opportunity-detail/components/DocumentPreview/
features/opportunity-detail/components/ActionPanel/
features/opportunity-detail/components/EventTimeline/
```

## Backend

```text
GET /api/contracts/:id
GET /api/contracts/:id/documents
GET /api/contracts/:id/facets
GET /api/contracts/:id/original/:docId
POST /api/contracts/:id/track
POST /api/contracts/:id/analyze
GET /api/matches/:id/events
POST /api/matches/:id/events
GET /api/contracts/:id/seace/consultations
GET /api/contracts/:id/seace/quote-context
```

## Acciones principales

- Marcar como interesada.
- Iniciar preparacion.
- Descargar documento original.
- Abrir preview.
- Consultar preguntas oficiales de SEACE bajo demanda.
- Cargar los campos y formatos oficiales de cotización bajo demanda.
- Comenzar o continuar la postulación en `/postulaciones/[matchId]`.
- Evaluar perfil vs. licitación directamente en el backend web; guarda el match antes de responder y no necesita `worker-llm`.

## Estados

- Sin match: mostrar resumen y boton `Evaluar con mi perfil`.
- Sin perfil: CTA `Completar perfil para evaluar`.
- Extraccion parcial: mostrar datos disponibles y aviso discreto.
- Sin PDF preview: mostrar boton al original.
- Error de detalle: volver al feed.
- Sin conexión SEACE: explicar que debe conectarla en Perfil; no bloquear el resto del detalle.
- Documento DOCX/XLSX: descarga directa; el visor inline se reserva para PDF.

## Criterios de done

- El detalle no depende de que exista match.
- El detalle no repite monto, responsable, notas ni checklist; la coordinación vive en la postulación y seguimiento.
- Los requisitos muestran evidencia expandible.
- La informacion larga vive en tabs/acordeones, no en el primer pantallazo.
