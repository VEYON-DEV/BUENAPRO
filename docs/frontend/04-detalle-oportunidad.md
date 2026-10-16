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

## Puntaje de capacidad y preparación documental

En contratos menores, el puntaje y las brechas prioritarias evalúan experiencia compatible, monto computable, personal y capacidades especializadas. RNP/RUC, declaraciones de impedimentos, propuesta económica, carta CCI y formatos ordinarios se presentan en «Documentos para presentar», plegado por defecto y sin impacto en el puntaje. El checklist conserva las obligaciones de presentación sin afirmar que la falta de un archivo en el perfil implica incapacidad o incumplimiento legal.

«Requisitos clave cubiertos» cuenta únicamente filas sustantivas obligatorias evaluadas en `breakdown.requisitos` y su estado `cumple`; excluye requisitos no aplicables, deseables, preparación administrativa y penalidades. Sin filas evaluadas no calcula cobertura a partir del inventario de facets. Cuando el desglose contiene `administrative_checklist` (alias `preparacion_documental`), sus requisitos ya son la clasificación sustantiva autorizada: una propuesta técnica API/SLA sigue evaluándose aunque conserve el tipo fuente `proposal_document`. El checklist muestra solo su lista explícita. Para evaluaciones anteriores se separan categorías administrativas explícitas, conservando licencias, certificados y propuestas técnicas; `source_index` se resuelve contra el inventario original íntegro antes de excluir penalidades.

Si la comparación económica o fuente requiere validación, por solicitud explícita del usuario se muestra `provisional_score` guardado de 0 a 100 en anillo rojo (<30), amarillo (30–80 inclusive) o verde (>80), con «Puntaje provisional» y «Estimación IA · pendiente de validación». El puntaje final conserva su protección y las revisiones no se aprueban por mostrar el número ni por su color. Sin valor provisional válido, mantiene «Puntaje por validar» sin inventar cero. Conserva el análisis sustantivo, brechas y cobertura; un cero provisional real sí se muestra como 0/100.

## Navegacion interna

```text
Decisión y requisitos
Documentos
Histórico
Cronograma
Ejecución
```

La navegación interna usa tabs horizontales reales y conserva la selección en el hash de la URL. La lectura ejecutiva y los requisitos pertenecen a una sola sección para que la decisión conserve inmediatamente su evidencia. Solo se renderiza visualmente una sección central a la vez; los grupos de requisitos, SEACE y datos operativos permanecen plegados hasta que el usuario los solicita.

## Subcontratación y consorcio

«Cómo puedes participar» aparece en la sección de decisión y requisitos de PROD2, PROD4 y contratos menores. Reutiliza las mismas clasificaciones del listado y permite abrir la cita, su página y las condiciones mediante teclado. Mantiene por separado el permiso detectado y la revisión humana pendiente. Sin lectura o evidencia suficiente muestra `Por leer` o `Por confirmar`; no infiere un permiso a partir de la ausencia de datos.

La evidencia pertenece a la extracción vigente de esa convocatoria/procedimiento/documento. PROD2 identifica la página de la lectura combinada. PROD4 hace lo mismo cuando el manifiesto contiene varios miembros; una lectura de un solo documento y PROD6 indican página del documento leído. No se convierte una página combinada en un enlace `#page` del original. Los enlaces oficiales/documentales existentes se conservan.

QA local/productivo y límites: [participation-columns-qa-20261013.md](../participation-columns-qa-20261013.md). Las tres fuentes están desplegadas y verificadas en desktop/laptop/móvil desde el 13/10/2026.

## Cronograma oficial determinista

La pestaña `Cronograma` (`#schedule`) presenta todas las etapas de PROD6 desde `uitContratoEtapaProjectionList`, con respaldo en `contract.cronograma.etapas`. No depende de TDR, extracción ni score Gemini. Distingue preguntas/consultas, respuestas de la entidad y presentación de ofertas/cotizaciones; conserva nombres oficiales y etapas desconocidas. Muestra inicio/cierre, zona `America/Lima` y momento de consulta del detalle. Una fecha sin hora no recibe una hora ficticia, ni una fecha ausente se considera abierta.

El componente de negocio compartido vive en `features/procurement`; el normalizador puro transversal en `lib/procurementSchedule.ts`. El refresco web PROD6 sincroniza fecha inicial/final de cotización en la misma escritura que cronograma/raw/hash. La UI no cambia la periodicidad de polling del worker ni envía consultas al SEACE.

El timeline muestra todas las etapas oficiales de la fuente, sin añadir fases propias de concursos mayores a contratos menores. Conecta visualmente los hitos y etiqueta `Plazo finalizado`, `En plazo`, `Próxima` o `Sin precisión suficiente`. El resumen admite ventanas simultáneas; no afirma que una etapa ocurrió porque venció su fecha. Fecha sin hora del día actual en Lima queda incierta. El reloj se inicia tras hidratación y se actualiza cada minuto. El dato oficial se refresca mediante la corrida documentada en `docs/sources/official-schedule-refresh.md`; `schedule_fetched_at` distingue la actualización parcial del detalle completo.

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
GET /api/contracts/:id?profile_id=
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

El inspector y el enlace desde menores conservan `profile_id`; el detalle y su API resuelven ese perfil activo del tenant antes de elegir el match y la afinidad. El retorno al feed mantiene el perfil. Un UUID ajeno/inactivo no habilita fallback al puntaje de otra empresa. Omitirlo selecciona el primer perfil activo por orden estable, igual que el listado.

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

### Extensión PROD4 (2026-10-02)

Ruta `/oportunidad/seace/[id]`, enlazada desde la vista rápida del radar. Reutiliza el shell Glass, DecisionOverview, navegación de secciones y cronograma oficial. El servicio tenant-scoped añade `analysis`: extracción vigente, facets/evidencia y match del perfil activo ligado a esa extracción. La afinidad preliminar no sustituye puntuación documental.

Las bases se leen con el ejecutor local documentado en `../sources/prod4-local-worker.md`. La vista muestra pendientes/revisión explícitos. Documentos abren el original en SEACE desde la conexión del usuario; no dependen del proxy bloqueado de la VM ni prometen preview R2. Las etapas provienen del cronograma oficial, no de fechas inferidas del PDF. Historial no se muestra hasta tener integración real.

### Análisis IA manual PROD4 (2026-10-05)

La franja de decisión prioriza `Analizar con IA` / `Volver a analizar con IA` usando el Button compartido. `Revisar en SEACE` pasa al encabezado junto a entidad/ubicación, conservando el enlace directo del concurso en pestaña nueva; las bases mantienen su acción secundaria.

`POST /api/prod4/opportunities/:id/analyze` encola el LLM2 existente con el perfil activo del tenant y bases LLM1 vigentes y validadas. No descarga documentos ni sustituye LLM1. `force:true` reevalúa un match existente sin reutilizar su resultado anterior. Los clics concurrentes reutilizan pending/claimed. `GET` en esa ruta con `job_id` consulta solo trabajos del tenant/procedimiento y confirma un match vigente antes de informar éxito. Sin perfil devuelve409; sin bases adecuadas o proceso vigente devuelve422. No requiere credenciales SEACE para comparar datos ya extraídos.

El cliente evita dobles clics, consulta cada3segundos durante hasta2minutos, actualiza la vista al completar y permite consultar nuevamente sin encolar otra evaluación si continúa pendiente. Falta de bases desactiva el botón con explicación; errores se anuncian de forma accesible. La implementación requiere desplegar web y worker-llm (soporte de force); no se ha desplegado en esta tarea.

- El detalle no depende de que exista match.
- El detalle no repite monto, responsable, notas ni checklist; la coordinación vive en la postulación y seguimiento.
- Los requisitos muestran evidencia expandible.
- La informacion larga vive en tabs/acordeones, no en el primer pantallazo.

### Detalle del buscador público PROD2 (2026-10-05)

Ruta `/oportunidad/prod2/[id_convocatoria]`, con `profile_id` opcional perteneciente al tenant. Reutiliza shell Glass, franja de decisión, navegación de secciones y cronograma. El componente de fuente muestra resumen, requisitos con evidencia, estado por miembro del convenio, documentos, etapas y ejecución aun cuando el match esté pendiente o gris. Los deseables conservan su etiqueta y se excluyen del conteo de requisitos obligatorios.

La afinidad por palabras clave es una señal comercial por línea de negocio, independiente del puntaje documental; ninguno acredita elegibilidad ni representa probabilidad de adjudicación. La fuente y los ítems publicados no se usan para afirmar vigencia. Los días sin hora conservan esa precisión. No existe acción que reactive las colas PROD4 o ejecute otro LLM desde esta vista.

`GET /api/prod2/opportunities/:id` devuelve ficha normalizada, `official_schedule`, `documents`, `analysis` y perfil seleccionado. Un match solo aparece con procedencia PROD2 vinculada a convocatoria, extracción vigente, hash del documento original, hash del perfil o convenio evaluado y evaluación terminada en el ledger. No hay fallback por fecha ni por coincidencia de un ID numérico con otra fuente.

La política sustantiva V4 se presenta de manera análoga a contratos menores: experiencia/capacidades y sus brechas determinan la cobertura; `administrative_checklist` vive en «Documentos para presentar», plegado y sin penalización. Una propuesta técnica API/SLA conserva su evaluación aunque provenga de `proposal_document`. `meta.score_validated=false` o una revisión de fuente mantienen el estado provisional; el número guardado se muestra solo mediante `provisional_score` ligado al mismo match. Sin ese número muestra «Puntaje por validar», sin sustituirlo por cero ni perder el análisis. Un cero validado conserva su presentación final. Los deseables, evidencia de las bases, atribuciones `member_gaps` de convenios anteriores y el perfil elegido en el enlace de retorno se conservan.

Las alternativas MYPE identificadas en la proyección se muestran como «Condicional · MYPE» y se excluyen del contador general cuando `required=false`; requieren confirmar que la condición aplica al postor. Un análisis manual de una lectura existente con revisión preservada continúa provisional y mantiene el aviso de las bases. Las filas de seguimiento por omisión del modelo declaran ese origen y no acreditan incumplimiento. Los preparativos ordinarios, incluidos pacto de integridad y promesa de consorcio cuando corresponda, permanecen fuera de la cobertura de capacidad.

`GET /api/prod2/opportunities/:id/documents/:codigo_alfresco` requiere sesión y redirige con `private, no-store` a un GET R2 firmado por 60 segundos. Solo firma keys verificadas `prod2/<convocatoria>/<UUID>/<sha_preview>.pdf`; admite vistas PDF convertidas de ZIP/DOCX sin confundir hash del original con hash del preview. Las credenciales, key privada y URL firmada no se entregan en el JSON de ficha. El vínculo a SEACE abre el buscador estable, conserva la procedencia y no inventa un enlace directo de sesión.
