# Vista 03 - Oportunidades

## Objetivo

Mostrar licitaciones/contratos menores cargados por BuenaPro, con filtros utiles y prioridad por match cuando exista perfil.

## Usuario

Proveedor que revisa oportunidades vigentes. Puede estar antes o despues de configurar su perfil.

## Ruta

```text
/feed
```

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
