# Vista 03 - Oportunidades

## Objetivo

Mostrar licitaciones/contratos menores cargados por BuenaPro, con filtros utiles y prioridad por match cuando exista perfil.

## Usuario

Proveedor que revisa oportunidades vigentes. Puede estar antes o despues de configurar su perfil.

## Ruta

```text
/feed
```

`/feed?source=prod4` abre la fuente complementaria de procedimientos SEACE vigentes. No mezcla sus filas con los contratos menores de PROD6: ambos usan identidad nativa distinta y se muestran en pestañas de fuente.

## Concursos SEACE (PROD4)

- `GET /api/prod4/opportunities?object=good|service&q=&state=current|exited|all&page=&page_size=`: solo procedimientos marcados como tecnología por CUBSO; la vista usa `current` por defecto.
- `GET /api/prod4/opportunities/:id`: ficha con ítems, cronograma y metadatos documentales.
- El detalle devuelve `official_schedule`: todas las etapas de `raw_detail.listaCronograma` normalizadas por reglas, además de `process.detail_fetched_at`. Conserva `schedule` para compatibilidad; sus timestamps históricos pueden tener horas inferidas, por lo que la nueva sección usa exclusivamente `official_schedule`.
- `Cronograma oficial` en la vista rápida usa un timeline conectado en el orden publicado: primeras tres etapas y desplegable con todas las siguientes. El resumen identifica todas las ventanas simultáneamente en plazo, o la próxima fecha conocida. Diferencia absolución/respuestas de formulación de consultas y observaciones. Inicio/cierre usan hora de Perú, con fecha y hora separadas; las fechas sin hora indican `Hora no informada` y las ausentes `No informado`. No interviene Gemini.
- Estados temporales: `Plazo finalizado`, `En plazo`, `Próxima`, `Sin precisión suficiente`. Son posiciones en las fechas publicadas, no constancias de ejecución/adjudicación ni elegibilidad. Una fecha sin hora correspondiente al día actual en Lima queda incierta. Se recalculan cada minuto en navegador; no se persisten estados que quedarían obsoletos. La consulta del cronograma usa `schedule_fetched_at` y, si falta, `detail_fetched_at`.
- `GET /api/prod4/opportunities/:id/documents/:code`: proxy autenticado de PDF oficial, sin almacenar el archivo.
- La fecha de cierre de registro no equivale al cierre de presentación de propuestas. Mostrar ambas por separado y priorizar la segunda solo cuando la ficha oficial la indique.
- `exited` significa que salió del listado PROD4, no que fue adjudicado, cancelado ni cerrado. No inferir estado jurídico desde esa ausencia.
- La vista rápida es informativa: no afirma elegibilidad, no ejecuta postulación y remite al módulo oficial para comprobar cronograma y requisitos.
- El listado autenticado compara CUBSO y texto de los ítems con las líneas de negocio activas del perfil. Ordena por afinidad preliminar y muestra 1–3 puntos: general, relacionado o rubro exacto. Si no hay segmento compatible, deja la afinidad sin calcular; no inventa el nivel 1.
- El anillo 0–100 y el veredicto solo aparecen cuando el worker extrajo requisitos de un PDF oficial de bases/EETT/TDR y ejecutó el análisis con Gemini. Los puntos preliminares no significan que el proveedor cumpla los requisitos.

## Dos modos

### Explorar

Usa contratos crudos de SEACE ya cargados.

```text
GET /api/contracts
```

Sirve cuando el usuario aun no tiene perfil o quiere revisar todo.

### Prioritario

Usa matches calculados contra el perfil.

```text
GET /api/feed
```

Sirve cuando ya existe perfil y lineas de negocio.

## Layout

- Header de pagina: titulo corto, contador y accion secundaria.
- Toolbar con busqueda y filtros principales.
- Tabla/lista densa y limpia.
- Drawer o panel lateral para filtros avanzados.

## Columnas recomendadas

- Codigo.
- Objeto/resumen corto.
- Entidad.
- Ubicacion.
- Cierre.
- Estado SEACE.
- Match: verdict + score si existe.
- Accion: abrir / seguir.

## Filtros

Base:

- guardadas del workspace `saved=true`
- texto `q`
- objeto
- estado
- segmento CUBSO
- region
- bucket MVP: tecnologia, transporte, legal
- cierra antes de fecha
- tiene extraccion IA
- se puede cotizar

El panel de filtros expone `Tipo` como tres opciones visibles: `Todos`, `Bienes` y `Servicios` (`objeto=1/2`). Al elegir una opción se aplica de inmediato y se marca la activa; `Aplicar`, la búsqueda y las vistas rápidas conservan el tipo seleccionado.

Inteligentes:

- verdict
- facet
- role
- tipo_pago

## Componentes

```text
features/opportunities/components/OpportunityToolbar/
features/opportunities/components/OpportunityTable/
features/opportunities/components/OpportunityRow/
features/opportunities/components/OpportunityFiltersDrawer/
features/opportunities/components/OpportunityVerdictBadge/
features/opportunities/components/DeadlineCell/
features/opportunities/components/EmptyOpportunities/
```

## Backend

```text
GET /api/contracts?page=&page_size=&q=&objeto=&estado=&segmento=&region=&bucket=&has_extraction=&cotizar=
PUT /api/contracts/:id/saved
DELETE /api/contracts/:id/saved
GET /api/feed?page=&page_size=&verdict=&q=&objeto=&estado=&segmento=&region=&role=&facet=&tipo_pago=
POST /api/contracts/:id/track
GET /api/catalogs/objects
GET /api/catalogs/states
GET /api/catalogs/cubso/segments
GET /api/catalogs/enabled-cubso-segments
```

## Acciones

- Abrir detalle.
- Guardar o quitar una oportunidad sin incorporarla al seguimiento.
- Marcar como `interesada`.
- Pasar directo a `en_preparacion`.
- Limpiar filtros.

## Estados

- Sin perfil: mostrar modo Explorar y CTA discreta a Perfil.
- Con perfil pero sin matches: mostrar Explorar + aviso de que el match se esta calculando.
- Sin resultados por filtros: permitir limpiar filtros.
- Error de API: retry.

## Criterios de done

- La tabla no se rompe con textos largos.
- Paginacion funciona.
- Los filtros reflejan query params.
- La vista es usable con 0 matches y con matches.
