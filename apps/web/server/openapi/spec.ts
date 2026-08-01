const json = (schema: object) => ({ "application/json": { schema } });

const listResponse = {
  description: "Listado",
  content: json({ $ref: "#/components/schemas/ListResponse" }),
};

const paginatedResponse = {
  description: "Listado paginado",
  content: json({ $ref: "#/components/schemas/PaginatedResponse" }),
};

const singleResponse = {
  description: "Recurso",
  content: json({ $ref: "#/components/schemas/SingleResponse" }),
};

const idPathParam = { name: "id", in: "path", required: true, schema: { type: "integer" } };
const uuidPathParam = { name: "id", in: "path", required: true, schema: { type: "string", format: "uuid" } };

const paginationParams = [
  { name: "page", in: "query", schema: { type: "integer", minimum: 1, default: 1 } },
  { name: "page_size", in: "query", schema: { type: "integer", minimum: 1, maximum: 200, default: 50 } },
];

export const openApiSpec = {
  openapi: "3.1.0",
  info: {
    title: "BuenaPro API",
    version: "0.1.0",
    description:
      "API interna MVP para feed, oportunidades, perfil, lineas de negocio, miembros de tenant, seguimiento, notificaciones, administracion interna y jobs del worker.",
  },
  servers: [{ url: "http://127.0.0.1:3000", description: "Local dev" }],
  security: [{ tenantHeader: [] }],
  tags: [
    { name: "Feed" },
    { name: "Contracts" },
    { name: "Catalogs" },
    { name: "Documents" },
    { name: "Facets" },
    { name: "Profile" },
    { name: "Profiles" },
    { name: "Tenant" },
    { name: "Lines" },
    { name: "Tenant Members" },
    { name: "Tracking" },
    { name: "Notifications" },
    { name: "Admin" },
    { name: "Internal" },
    { name: "OpenAPI" },
    { name: "Auth" },
  ],
  paths: {
    "/api/feed": {
      get: {
        tags: ["Feed"],
        summary: "Lista oportunidades matcheadas para el tenant actual",
        parameters: [
          { name: "verdict", in: "query", schema: { type: "string", enum: ["verde", "ambar", "rojo", "gris"] } },
          { name: "q", in: "query", schema: { type: "string" } },
          { name: "objeto", in: "query", schema: { type: "integer" } },
          { name: "estado", in: "query", schema: { type: "integer" } },
          { name: "segmento", in: "query", schema: { type: "string" } },
          { name: "region", in: "query", schema: { type: "string" } },
          { name: "tipo_pago", in: "query", schema: { type: "string" } },
          { name: "role", in: "query", schema: { type: "string" } },
          { name: "facet", in: "query", schema: { type: "string" } },
          { name: "closing_before", in: "query", schema: { type: "string", format: "date-time" } },
          { name: "page", in: "query", schema: { type: "integer", minimum: 1, default: 1 } },
          { name: "page_size", in: "query", schema: { type: "integer", minimum: 1, maximum: 100, default: 20 } },
        ],
        responses: {
          "200": { ...listResponse, description: "Feed de oportunidades" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
    },
    "/api/contracts": {
      get: {
        tags: ["Contracts"],
        summary: "Explora oportunidades SEACE cargadas, con match del tenant si existe",
        parameters: [
          ...paginationParams,
          { name: "q", in: "query", schema: { type: "string" } },
          { name: "objeto", in: "query", schema: { type: "integer" } },
          { name: "estado", in: "query", schema: { type: "integer" } },
          { name: "segmento", in: "query", schema: { type: "string" } },
          { name: "region", in: "query", schema: { type: "string" } },
          { name: "bucket", in: "query", schema: { type: "string", enum: ["tecnologia", "transporte", "legal"] } },
          { name: "pipeline_state", in: "query", schema: { type: "string" } },
          { name: "has_extraction", in: "query", schema: { type: "boolean" } },
          { name: "cotizar", in: "query", schema: { type: "boolean" } },
          { name: "saved", in: "query", description: "Filtra por oportunidades guardadas en el workspace", schema: { type: "boolean" } },
          { name: "closing_before", in: "query", schema: { type: "string", format: "date-time" } },
        ],
        responses: {
          "200": { ...paginatedResponse, description: "Contratos cargados" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
    },
    "/api/contracts/{id}": {
      get: {
        tags: ["Contracts"],
        summary: "Detalle de una oportunidad",
        parameters: [idPathParam],
        responses: {
          "200": {
            description: "Contrato con facets, documentos y match",
            content: json({ $ref: "#/components/schemas/ContractDetailResponse" }),
          },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/contracts/{id}/track": {
      post: {
        tags: ["Contracts", "Tracking"],
        summary: "Asegura match del tenant y mueve una oportunidad al embudo",
        parameters: [idPathParam],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/TrackContractInput" }) },
        responses: {
          "200": { ...singleResponse, description: "Match creado/actualizado" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "409": { description: "Se requiere perfil activo antes de hacer seguimiento" },
        },
      },
    },
    "/api/contracts/{id}/history": {
      get: {
        tags: ["Contracts"],
        summary: "Devuelve comparables históricos explicables y métricas de mercado",
        parameters: [idPathParam],
        responses: {
          "200": { ...singleResponse, description: "Comparables, precios, desiertos y proveedores frecuentes" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/dashboard": {
      get: {
        tags: ["Feed", "Tracking"],
        summary: "Radar diario del tenant con cierres, postulaciones y mercado",
        responses: {
          "200": { ...singleResponse, description: "Acciones y métricas accionables del perfil activo" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
    },
    "/api/contracts/{id}/saved": {
      put: {
        tags: ["Contracts"],
        summary: "Guarda una oportunidad en el workspace",
        parameters: [idPathParam],
        responses: {
          "200": { ...singleResponse, description: "Oportunidad guardada" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      delete: {
        tags: ["Contracts"],
        summary: "Quita una oportunidad de guardadas",
        parameters: [idPathParam],
        responses: {
          "200": { ...singleResponse, description: "Oportunidad retirada de guardadas" },
          "400": { $ref: "#/components/responses/BadRequest" },
        },
      },
    },
    "/api/contracts/{id}/original/{docId}": {
      get: {
        tags: ["Contracts"],
        summary: "Redirige al PDF original en SEACE",
        parameters: [
          idPathParam,
          { name: "docId", in: "path", required: true, schema: { type: "integer" } },
        ],
        responses: {
          "307": { description: "Redirect al archivo original de SEACE" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/contracts/{id}/documents": {
      get: {
        tags: ["Contracts", "Documents"],
        summary: "Lista documentos de una oportunidad accesible por el tenant",
        parameters: [idPathParam],
        responses: {
          "200": { ...listResponse, description: "Documentos del contrato" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/contracts/{id}/facets": {
      get: {
        tags: ["Contracts", "Facets"],
        summary: "Lista facets actuales de una oportunidad accesible por el tenant",
        parameters: [idPathParam],
        responses: {
          "200": { ...listResponse, description: "Facets del contrato" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      post: {
        tags: ["Contracts", "Facets"],
        summary: "Crea un facet para una oportunidad accesible por el tenant",
        parameters: [idPathParam],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/ContractFacetInput" }) },
        responses: {
          "201": { ...singleResponse, description: "Facet creado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/catalogs/business-lines": {
      get: {
        tags: ["Catalogs", "Lines"],
        summary: "Lista lineas de negocio del tenant con filtros de catalogo",
        parameters: [
          { name: "active", in: "query", schema: { type: "boolean" } },
          { name: "q", in: "query", schema: { type: "string" } },
        ],
        responses: {
          "200": { ...listResponse, description: "Lineas de negocio" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
      post: {
        tags: ["Catalogs", "Lines"],
        summary: "Crea una linea de negocio para un perfil del tenant",
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/CatalogBusinessLineInput" }) },
        responses: {
          "201": { ...singleResponse, description: "Linea creada" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
    },
    "/api/catalogs/business-lines/{id}": {
      get: {
        tags: ["Catalogs", "Lines"],
        summary: "Obtiene una linea de negocio del tenant",
        parameters: [idPathParam],
        responses: {
          "200": { ...singleResponse, description: "Linea de negocio" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      patch: {
        tags: ["Catalogs", "Lines"],
        summary: "Actualiza una linea de negocio del tenant",
        parameters: [idPathParam],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/BusinessLinePatch" }) },
        responses: {
          "200": { ...singleResponse, description: "Linea actualizada" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      delete: {
        tags: ["Catalogs", "Lines"],
        summary: "Elimina una linea de negocio del tenant",
        parameters: [idPathParam],
        responses: {
          "200": { ...singleResponse, description: "Linea eliminada" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/catalogs/cubso-segments": {
      get: {
        tags: ["Catalogs"],
        summary: "Lista segmentos CUBSO paginados",
        parameters: [
          ...paginationParams,
          { name: "anio", in: "query", schema: { type: "integer" } },
          { name: "q", in: "query", schema: { type: "string" } },
        ],
        responses: { "200": paginatedResponse },
      },
    },
    "/api/catalogs/cubso/segments": {
      get: {
        tags: ["Catalogs"],
        summary: "Lista segmentos CUBSO",
        parameters: [
          ...paginationParams,
          { name: "anio", in: "query", schema: { type: "integer" } },
          { name: "q", in: "query", schema: { type: "string" } },
          { name: "enabled", in: "query", schema: { type: "boolean" } },
        ],
        responses: { "200": { ...listResponse, description: "Segmentos CUBSO" } },
      },
      post: {
        tags: ["Catalogs"],
        summary: "Crea un segmento CUBSO",
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/CubsoSegmentInput" }) },
        responses: {
          "201": { ...singleResponse, description: "Segmento CUBSO creado" },
          "400": { $ref: "#/components/responses/BadRequest" },
        },
      },
    },
    "/api/catalogs/cubso/segments/{codigo}/{anio}": {
      get: {
        tags: ["Catalogs"],
        summary: "Obtiene un segmento CUBSO",
        parameters: [
          { name: "codigo", in: "path", required: true, schema: { type: "string" } },
          { name: "anio", in: "path", required: true, schema: { type: "integer" } },
        ],
        responses: {
          "200": { ...singleResponse, description: "Segmento CUBSO" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      patch: {
        tags: ["Catalogs"],
        summary: "Actualiza un segmento CUBSO",
        parameters: [
          { name: "codigo", in: "path", required: true, schema: { type: "string" } },
          { name: "anio", in: "path", required: true, schema: { type: "integer" } },
        ],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/CubsoSegmentPatch" }) },
        responses: {
          "200": { ...singleResponse, description: "Segmento CUBSO actualizado" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      delete: {
        tags: ["Catalogs"],
        summary: "Elimina un segmento CUBSO",
        parameters: [
          { name: "codigo", in: "path", required: true, schema: { type: "string" } },
          { name: "anio", in: "path", required: true, schema: { type: "integer" } },
        ],
        responses: {
          "200": { ...singleResponse, description: "Segmento CUBSO eliminado" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/catalogs/enabled-cubso-segments": {
      get: {
        tags: ["Catalogs"],
        summary: "Lista segmentos CUBSO habilitados para MVP",
        responses: { "200": { ...listResponse, description: "Segmentos habilitados" } },
      },
      post: {
        tags: ["Catalogs"],
        summary: "Crea o actualiza un segmento CUBSO habilitado",
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/EnabledCubsoSegmentInput" }) },
        responses: { "201": { ...singleResponse, description: "Segmento habilitado guardado" } },
      },
    },
    "/api/catalogs/enabled-cubso-segments/{codigo}": {
      patch: {
        tags: ["Catalogs"],
        summary: "Actualiza un segmento CUBSO habilitado por codigo y bucket",
        parameters: [{ name: "codigo", in: "path", required: true, schema: { type: "string" } }],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/EnabledCubsoSegmentPatch" }) },
        responses: {
          "200": { ...singleResponse, description: "Segmento habilitado actualizado" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      delete: {
        tags: ["Catalogs"],
        summary: "Elimina un segmento CUBSO habilitado por codigo y bucket",
        parameters: [
          { name: "codigo", in: "path", required: true, schema: { type: "string" } },
          { name: "bucket", in: "query", required: true, schema: { type: "string" } },
        ],
        responses: {
          "200": { ...singleResponse, description: "Segmento habilitado eliminado" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/catalogs/entities": {
      get: {
        tags: ["Catalogs"],
        summary: "Lista entidades",
        parameters: [...paginationParams, { name: "q", in: "query", schema: { type: "string" } }],
        responses: { "200": paginatedResponse },
      },
    },
    "/api/catalogs/objects": {
      get: {
        tags: ["Catalogs"],
        summary: "Lista objetos SEACE",
        parameters: [...paginationParams, { name: "q", in: "query", schema: { type: "string" } }],
        responses: { "200": paginatedResponse },
      },
    },
    "/api/catalogs/states": {
      get: {
        tags: ["Catalogs"],
        summary: "Lista estados SEACE",
        parameters: [...paginationParams, { name: "q", in: "query", schema: { type: "string" } }],
        responses: { "200": paginatedResponse },
      },
    },
    "/api/catalogs/ubigeo": {
      get: {
        tags: ["Catalogs"],
        summary: "Lista ubigeos",
        parameters: [
          ...paginationParams,
          { name: "departamento", in: "query", schema: { type: "string" } },
          { name: "provincia", in: "query", schema: { type: "string" } },
          { name: "distrito", in: "query", schema: { type: "string" } },
        ],
        responses: { "200": paginatedResponse },
      },
    },
    "/api/catalogs/seace/{kind}": {
      get: {
        tags: ["Catalogs"],
        summary: "Lista catalogo SEACE por tipo",
        parameters: [
          { name: "kind", in: "path", required: true, schema: { type: "string", enum: ["objects", "states"] } },
          ...paginationParams,
          { name: "q", in: "query", schema: { type: "string" } },
        ],
        responses: {
          "200": { ...listResponse, description: "Catalogo SEACE" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      post: {
        tags: ["Catalogs"],
        summary: "Crea entrada de catalogo SEACE",
        parameters: [{ name: "kind", in: "path", required: true, schema: { type: "string", enum: ["objects", "states"] } }],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/SeaceCatalogInput" }) },
        responses: {
          "201": { ...singleResponse, description: "Entrada creada" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/catalogs/seace/{kind}/{codigo}": {
      get: {
        tags: ["Catalogs"],
        summary: "Obtiene entrada de catalogo SEACE",
        parameters: [
          { name: "kind", in: "path", required: true, schema: { type: "string", enum: ["objects", "states"] } },
          { name: "codigo", in: "path", required: true, schema: { type: "integer" } },
        ],
        responses: {
          "200": { ...singleResponse, description: "Entrada de catalogo" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      patch: {
        tags: ["Catalogs"],
        summary: "Actualiza entrada de catalogo SEACE",
        parameters: [
          { name: "kind", in: "path", required: true, schema: { type: "string", enum: ["objects", "states"] } },
          { name: "codigo", in: "path", required: true, schema: { type: "integer" } },
        ],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/SeaceCatalogPatch" }) },
        responses: {
          "200": { ...singleResponse, description: "Entrada actualizada" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      delete: {
        tags: ["Catalogs"],
        summary: "Elimina entrada de catalogo SEACE",
        parameters: [
          { name: "kind", in: "path", required: true, schema: { type: "string", enum: ["objects", "states"] } },
          { name: "codigo", in: "path", required: true, schema: { type: "integer" } },
        ],
        responses: {
          "200": { ...singleResponse, description: "Entrada eliminada" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/profile": {
      get: {
        tags: ["Profile"],
        summary: "Obtiene el perfil principal del tenant",
        responses: {
          "200": { ...singleResponse, description: "Perfil o null" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
      put: {
        tags: ["Profile"],
        summary: "Crea o actualiza perfil de empresa y encola rematch",
        requestBody: {
          required: true,
          content: json({ $ref: "#/components/schemas/ProfileInput" }),
        },
        responses: {
          "200": { ...singleResponse, description: "Perfil guardado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
    },
    "/api/profile/keywords": {
      put: {
        tags: ["Profile"],
        summary: "Actualiza las keywords generales de identidad y encola rematch",
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/CompanyKeywordsInput" }) },
        responses: {
          "200": { ...singleResponse, description: "Identidad del radar actualizada" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
    },
    "/api/profiles": {
      get: {
        tags: ["Profile"],
        summary: "Lista perfiles de empresa del tenant",
        responses: {
          "200": { ...listResponse, description: "Perfiles del tenant" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
      post: {
        tags: ["Profile"],
        summary: "Crea un perfil de empresa y encola rematch",
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/ProfileCreateInput" }) },
        responses: {
          "201": { ...singleResponse, description: "Perfil creado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
    },
    "/api/profiles/{id}": {
      get: {
        tags: ["Profile"],
        summary: "Obtiene un perfil de empresa del tenant",
        parameters: [{ name: "id", in: "path", required: true, schema: { type: "string", format: "uuid" } }],
        responses: {
          "200": { ...singleResponse, description: "Perfil" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      patch: {
        tags: ["Profile"],
        summary: "Actualiza un perfil de empresa y encola rematch",
        parameters: [{ name: "id", in: "path", required: true, schema: { type: "string", format: "uuid" } }],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/ProfilePatchInput" }) },
        responses: {
          "200": { ...singleResponse, description: "Perfil actualizado" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      delete: {
        tags: ["Profile"],
        summary: "Desactiva un perfil de empresa",
        parameters: [{ name: "id", in: "path", required: true, schema: { type: "string", format: "uuid" } }],
        responses: {
          "200": { ...singleResponse, description: "Perfil desactivado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/lines": {
      get: {
        tags: ["Lines"],
        summary: "Lista lineas de negocio del perfil",
        responses: {
          "200": { ...listResponse, description: "Lineas" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
      post: {
        tags: ["Lines"],
        summary: "Crea una linea de negocio",
        requestBody: {
          required: true,
          content: json({ $ref: "#/components/schemas/BusinessLineInput" }),
        },
        responses: {
          "201": { ...singleResponse, description: "Linea creada" },
          "400": { description: "Se requiere perfil antes de crear lineas" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
    },
    "/api/lines/{id}": {
      get: {
        tags: ["Lines"],
        summary: "Obtiene una linea de negocio",
        parameters: [idPathParam],
        responses: {
          "200": { ...singleResponse, description: "Linea" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      patch: {
        tags: ["Lines"],
        summary: "Actualiza una linea de negocio",
        parameters: [idPathParam],
        requestBody: {
          required: true,
          content: json({ $ref: "#/components/schemas/BusinessLinePatch" }),
        },
        responses: {
          "200": { ...singleResponse, description: "Linea actualizada" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      delete: {
        tags: ["Lines"],
        summary: "Desactiva una linea de negocio",
        parameters: [idPathParam],
        responses: {
          "200": { ...singleResponse, description: "Linea desactivada" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/tenant": {
      get: {
        tags: ["Tenant"],
        summary: "Obtiene el tenant actual",
        responses: {
          "200": { ...singleResponse, description: "Tenant o null" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
      patch: {
        tags: ["Tenant"],
        summary: "Actualiza nombre o plan del tenant actual",
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/TenantPatch" }) },
        responses: {
          "200": { ...singleResponse, description: "Tenant actualizado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
    },
    "/api/tenant/members": {
      get: {
        tags: ["Tenant Members"],
        summary: "Lista miembros del tenant actual",
        responses: {
          "200": { ...listResponse, description: "Miembros del tenant" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
      post: {
        tags: ["Tenant Members"],
        summary: "Agrega o actualiza un miembro del tenant",
        requestBody: {
          required: true,
          content: json({ $ref: "#/components/schemas/TenantMemberInput" }),
        },
        responses: {
          "201": { ...singleResponse, description: "Miembro creado o actualizado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
    },
    "/api/tenant/members/{userId}": {
      patch: {
        tags: ["Tenant Members"],
        summary: "Actualiza el rol de un miembro del tenant",
        parameters: [{ name: "userId", in: "path", required: true, schema: { type: "string", format: "uuid" } }],
        requestBody: {
          required: true,
          content: json({ $ref: "#/components/schemas/TenantMemberPatch" }),
        },
        responses: {
          "200": { ...singleResponse, description: "Miembro actualizado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      delete: {
        tags: ["Tenant Members"],
        summary: "Elimina un miembro del tenant",
        parameters: [{ name: "userId", in: "path", required: true, schema: { type: "string", format: "uuid" } }],
        responses: {
          "200": { ...singleResponse, description: "Miembro eliminado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/documents/{id}": {
      get: {
        tags: ["Documents"],
        summary: "Obtiene un documento accesible por el tenant",
        parameters: [idPathParam],
        responses: {
          "200": { ...singleResponse, description: "Documento" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      patch: {
        tags: ["Documents"],
        summary: "Actualiza clase y capa de texto de un documento",
        parameters: [idPathParam],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/DocumentPatch" }) },
        responses: {
          "200": { ...singleResponse, description: "Documento actualizado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/documents/contracts/{id}": {
      get: {
        tags: ["Documents"],
        summary: "Lista documentos de un contrato accesible por el tenant",
        parameters: [idPathParam],
        responses: {
          "200": { ...listResponse, description: "Documentos del contrato" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      post: {
        tags: ["Documents"],
        summary: "Crea un documento para un contrato accesible por el tenant",
        parameters: [idPathParam],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/DocumentInput" }) },
        responses: {
          "201": { ...singleResponse, description: "Documento creado" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/documents/contracts/{id}/{docId}": {
      get: {
        tags: ["Documents"],
        summary: "Obtiene un documento de un contrato",
        parameters: [
          idPathParam,
          { name: "docId", in: "path", required: true, schema: { type: "integer" } },
        ],
        responses: {
          "200": { ...singleResponse, description: "Documento" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      patch: {
        tags: ["Documents"],
        summary: "Actualiza un documento de un contrato",
        parameters: [
          idPathParam,
          { name: "docId", in: "path", required: true, schema: { type: "integer" } },
        ],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/DocumentPatch" }) },
        responses: {
          "200": { ...singleResponse, description: "Documento actualizado" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      delete: {
        tags: ["Documents"],
        summary: "Elimina un documento de un contrato",
        parameters: [
          idPathParam,
          { name: "docId", in: "path", required: true, schema: { type: "integer" } },
        ],
        responses: {
          "200": { ...singleResponse, description: "Documento eliminado" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/facets/{id}": {
      patch: {
        tags: ["Facets"],
        summary: "Actualiza un facet accesible por el tenant",
        parameters: [idPathParam],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/FacetPatch" }) },
        responses: {
          "200": { ...singleResponse, description: "Facet actualizado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      delete: {
        tags: ["Facets"],
        summary: "Marca un facet como no actual",
        parameters: [idPathParam],
        responses: {
          "200": { ...singleResponse, description: "Facet desactivado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/facets/contracts/{id}": {
      get: {
        tags: ["Facets"],
        summary: "Lista facets de un contrato",
        parameters: [
          idPathParam,
          { name: "current", in: "query", schema: { type: "boolean", default: true } },
          { name: "facet", in: "query", schema: { type: "string" } },
        ],
        responses: {
          "200": { ...listResponse, description: "Facets del contrato" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      post: {
        tags: ["Facets"],
        summary: "Crea un facet para un contrato",
        parameters: [idPathParam],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/FacetInput" }) },
        responses: {
          "201": { ...singleResponse, description: "Facet creado" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/facets/contracts/{id}/{facetId}": {
      get: {
        tags: ["Facets"],
        summary: "Obtiene un facet de un contrato",
        parameters: [
          idPathParam,
          { name: "facetId", in: "path", required: true, schema: { type: "integer" } },
        ],
        responses: {
          "200": { ...singleResponse, description: "Facet" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      patch: {
        tags: ["Facets"],
        summary: "Actualiza un facet de un contrato",
        parameters: [
          idPathParam,
          { name: "facetId", in: "path", required: true, schema: { type: "integer" } },
        ],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/FacetPatch" }) },
        responses: {
          "200": { ...singleResponse, description: "Facet actualizado" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      delete: {
        tags: ["Facets"],
        summary: "Elimina un facet de un contrato",
        parameters: [
          idPathParam,
          { name: "facetId", in: "path", required: true, schema: { type: "integer" } },
        ],
        responses: {
          "200": { ...singleResponse, description: "Facet eliminado" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/matches/{id}": {
      patch: {
        tags: ["Tracking"],
        summary: "Actualiza seguimiento de un match",
        parameters: [idPathParam],
        requestBody: {
          required: true,
          content: json({ $ref: "#/components/schemas/MatchPatch" }),
        },
        responses: {
          "200": { ...singleResponse, description: "Match actualizado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/matches/{id}/events": {
      get: {
        tags: ["Tracking"],
        summary: "Lista eventos de un match",
        parameters: [idPathParam],
        responses: {
          "200": { ...listResponse, description: "Eventos del match" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      post: {
        tags: ["Tracking"],
        summary: "Registra un evento en un match",
        parameters: [idPathParam],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/MatchEventInput" }) },
        responses: {
          "201": { ...singleResponse, description: "Evento creado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/matches/{id}/tasks": {
      get: {
        tags: ["Tracking"],
        summary: "Lista checklist de postulacion de un match",
        parameters: [
          idPathParam,
          { name: "ensure_defaults", in: "query", schema: { type: "boolean" } },
        ],
        responses: {
          "200": { ...listResponse, description: "Tareas del match" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      post: {
        tags: ["Tracking"],
        summary: "Crea una tarea manual de postulacion",
        parameters: [idPathParam],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/MatchTaskInput" }) },
        responses: {
          "201": { ...singleResponse, description: "Tarea creada" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/matches/{id}/tasks/{taskId}": {
      patch: {
        tags: ["Tracking"],
        summary: "Actualiza una tarea de postulacion",
        parameters: [
          idPathParam,
          { name: "taskId", in: "path", required: true, schema: { type: "integer" } },
        ],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/MatchTaskPatch" }) },
        responses: {
          "200": { ...singleResponse, description: "Tarea actualizada" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      delete: {
        tags: ["Tracking"],
        summary: "Elimina una tarea de postulacion",
        parameters: [
          idPathParam,
          { name: "taskId", in: "path", required: true, schema: { type: "integer" } },
        ],
        responses: {
          "200": { ...singleResponse, description: "Tarea eliminada" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/tracking": {
      get: {
        tags: ["Tracking"],
        summary: "Lista matches en embudo de seguimiento",
        parameters: [{ name: "state", in: "query", schema: { type: "string" } }],
        responses: {
          "200": { ...listResponse, description: "Matches" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
    },
    "/api/notifications": {
      get: {
        tags: ["Notifications"],
        summary: "Lista notificaciones del tenant",
        parameters: [
          ...paginationParams,
          { name: "status", in: "query", schema: { type: "string" } },
          { name: "channel", in: "query", schema: { type: "string", enum: ["email", "telegram", "in_app"] } },
        ],
        responses: {
          "200": paginatedResponse,
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
      post: {
        tags: ["Notifications"],
        summary: "Crea una notificacion para un usuario del tenant",
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/NotificationInput" }) },
        responses: {
          "201": { ...singleResponse, description: "Notificacion creada" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/notifications/{id}": {
      get: {
        tags: ["Notifications"],
        summary: "Obtiene una notificacion del tenant",
        parameters: [idPathParam],
        responses: {
          "200": { ...singleResponse, description: "Notificacion" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      patch: {
        tags: ["Notifications"],
        summary: "Actualiza una notificacion del tenant",
        parameters: [idPathParam],
        requestBody: { required: true, content: json({ $ref: "#/components/schemas/NotificationPatch" }) },
        responses: {
          "200": { ...singleResponse, description: "Notificacion actualizada" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
      delete: {
        tags: ["Notifications"],
        summary: "Elimina una notificacion del tenant",
        parameters: [idPathParam],
        responses: {
          "200": { ...singleResponse, description: "Notificacion eliminada" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
        },
      },
    },
    "/api/notifications/prefs": {
      get: {
        tags: ["Notifications"],
        summary: "Lista preferencias de notificacion",
        responses: {
          "200": { ...listResponse, description: "Preferencias" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
      put: {
        tags: ["Notifications"],
        summary: "Crea o actualiza preferencia de notificacion",
        requestBody: {
          required: true,
          content: json({ $ref: "#/components/schemas/NotificationPreferenceInput" }),
        },
        responses: {
          "200": { ...singleResponse, description: "Preferencia guardada" },
          "400": { description: "Se requiere usuario/tenant" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
    },
    "/api/admin/worker_jobs": {
      get: {
        tags: ["Admin"],
        summary: "Lista jobs del worker",
        security: [{ internalToken: [] }],
        parameters: [
          ...paginationParams,
          { name: "status", in: "query", schema: { type: "string" } },
          { name: "queue_name", in: "query", schema: { type: "string", enum: ["io", "llm", "match", "notify"] } },
          { name: "job_type", in: "query", schema: { type: "string" } },
          { name: "q", in: "query", schema: { type: "string" } },
          { name: "sort", in: "query", schema: { type: "string", enum: ["id", "created_at", "run_after", "priority", "attempts", "status"] } },
          { name: "direction", in: "query", schema: { type: "string", enum: ["asc", "desc"] } },
        ],
        responses: {
          "200": paginatedResponse,
          "401": { $ref: "#/components/responses/Unauthorized" },
          "500": { $ref: "#/components/responses/InternalServerError" },
        },
      },
    },
    "/api/admin/batches/start": {
      post: {
        tags: ["Admin"],
        summary: "Encola batch MVP de ingesta por rubro",
        security: [{ internalToken: [] }],
        requestBody: {
          required: false,
          content: json({
            type: "object",
            properties: {
              year: { type: "integer", default: 2026 },
              per_bucket: { type: "integer", default: 150, minimum: 1, maximum: 500 },
            },
          }),
        },
        responses: {
          "201": { ...singleResponse, description: "Batch encolado" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "500": { $ref: "#/components/responses/InternalServerError" },
        },
      },
    },
    "/api/admin/batches/status": {
      get: {
        tags: ["Admin"],
        summary: "Estado, progreso, costo y ETA del ultimo batch o de un batch especifico",
        security: [{ internalToken: [] }],
        parameters: [{ name: "batch_id", in: "query", schema: { type: "string" } }],
        responses: {
          "200": { ...singleResponse, description: "Estado del batch" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "500": { $ref: "#/components/responses/InternalServerError" },
        },
      },
    },
    "/api/admin/worker_jobs/{id}": {
      get: {
        tags: ["Admin"],
        summary: "Obtiene un job del worker con eventos recientes",
        security: [{ internalToken: [] }],
        parameters: [idPathParam],
        responses: {
          "200": { description: "Job y eventos", content: json({ $ref: "#/components/schemas/WorkerJobDetailResponse" }) },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
          "500": { $ref: "#/components/responses/InternalServerError" },
        },
      },
    },
    "/api/admin/worker_jobs/{id}/retry": {
      post: {
        tags: ["Admin"],
        summary: "Reintenta un job fallido, muerto o claimado",
        security: [{ internalToken: [] }],
        parameters: [idPathParam],
        responses: {
          "200": { ...singleResponse, description: "Job reintentado" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
          "500": { $ref: "#/components/responses/InternalServerError" },
        },
      },
    },
    "/api/admin/worker_jobs/{id}/dead": {
      post: {
        tags: ["Admin"],
        summary: "Marca un job como dead",
        security: [{ internalToken: [] }],
        parameters: [idPathParam],
        requestBody: {
          required: false,
          content: json({ $ref: "#/components/schemas/MarkWorkerJobDeadInput" }),
        },
        responses: {
          "200": { ...singleResponse, description: "Job marcado como dead" },
          "400": { $ref: "#/components/responses/BadRequest" },
          "401": { $ref: "#/components/responses/Unauthorized" },
          "404": { $ref: "#/components/responses/NotFound" },
          "500": { $ref: "#/components/responses/InternalServerError" },
        },
      },
    },
    "/api/admin/pipeline_events": {
      get: {
        tags: ["Admin"],
        summary: "Lista eventos del pipeline",
        security: [{ internalToken: [] }],
        parameters: [
          ...paginationParams,
          { name: "id_contrato", in: "query", schema: { type: "integer" } },
          { name: "job_id", in: "query", schema: { type: "integer" } },
          { name: "stage", in: "query", schema: { type: "string" } },
          { name: "status", in: "query", schema: { type: "string" } },
        ],
        responses: {
          "200": paginatedResponse,
          "401": { $ref: "#/components/responses/Unauthorized" },
          "500": { $ref: "#/components/responses/InternalServerError" },
        },
      },
    },
    "/api/admin/api_contract_checks": {
      get: {
        tags: ["Admin"],
        summary: "Lista checks de contrato de API",
        security: [{ internalToken: [] }],
        parameters: [
          ...paginationParams,
          { name: "endpoint", in: "query", schema: { type: "string" } },
          { name: "ok", in: "query", schema: { type: "boolean" } },
        ],
        responses: {
          "200": paginatedResponse,
          "401": { $ref: "#/components/responses/Unauthorized" },
          "500": { $ref: "#/components/responses/InternalServerError" },
        },
      },
    },
    "/api/admin/tdr_extractions/requires_review": {
      get: {
        tags: ["Admin"],
        summary: "Lista extracciones TDR que requieren revision humana",
        security: [{ internalToken: [] }],
        parameters: [
          ...paginationParams,
          { name: "quality", in: "query", schema: { type: "string" } },
          { name: "id_contrato", in: "query", schema: { type: "integer" } },
        ],
        responses: {
          "200": paginatedResponse,
          "401": { $ref: "#/components/responses/Unauthorized" },
          "500": { $ref: "#/components/responses/InternalServerError" },
        },
      },
    },
    "/api/internal/jobs": {
      post: {
        tags: ["Internal"],
        summary: "Encola job manual protegido por token interno",
        security: [{ internalToken: [] }],
        requestBody: {
          required: true,
          content: json({ $ref: "#/components/schemas/InternalJobInput" }),
        },
        responses: {
          "202": { ...singleResponse, description: "Job encolado o deduplicado" },
          "401": { $ref: "#/components/responses/Unauthorized" },
        },
      },
    },
    "/api/openapi.json": {
      get: {
        tags: ["OpenAPI"],
        summary: "Obtiene la especificacion OpenAPI",
        security: [],
        responses: {
          "200": { description: "Documento OpenAPI", content: json({ type: "object", additionalProperties: true }) },
        },
      },
    },
    "/api/auth/{nextauth}": {
      get: {
        tags: ["Auth"],
        summary: "Rutas Auth.js/NextAuth",
        security: [],
        parameters: [{ name: "nextauth", in: "path", required: true, schema: { type: "string" } }],
        responses: { "200": { description: "Respuesta Auth.js" } },
      },
      post: {
        tags: ["Auth"],
        summary: "Rutas Auth.js/NextAuth",
        security: [],
        parameters: [{ name: "nextauth", in: "path", required: true, schema: { type: "string" } }],
        responses: { "200": { description: "Respuesta Auth.js" } },
      },
    },
  },
  components: {
    securitySchemes: {
      tenantHeader: { type: "apiKey", in: "header", name: "x-tenant-id" },
      internalToken: { type: "apiKey", in: "header", name: "x-internal-token" },
    },
    responses: {
      BadRequest: { description: "Solicitud invalida" },
      Unauthorized: { description: "No autorizado o falta contexto de tenant" },
      NotFound: { description: "No encontrado" },
      InternalServerError: { description: "Error interno del servidor" },
    },
    schemas: {
      ListResponse: {
        type: "object",
        required: ["data"],
        properties: { data: { type: "array", items: { type: "object", additionalProperties: true } } },
      },
      SingleResponse: {
        type: "object",
        required: ["data"],
        properties: { data: { anyOf: [{ type: "object", additionalProperties: true }, { type: "null" }] } },
      },
      PaginatedResponse: {
        type: "object",
        required: ["data", "meta"],
        properties: {
          data: { type: "array", items: { type: "object", additionalProperties: true } },
          meta: {
            type: "object",
            required: ["page", "page_size", "count"],
            properties: {
              page: { type: "integer", minimum: 1 },
              page_size: { type: "integer", minimum: 1 },
              count: { type: "integer", minimum: 0 },
              total: { type: "integer", minimum: 0 },
            },
          },
        },
      },
      ContractDetailResponse: {
        type: "object",
        properties: {
          contract: { type: "object", additionalProperties: true },
          facets: { type: "array", items: { type: "object", additionalProperties: true } },
          documents: { type: "array", items: { type: "object", additionalProperties: true } },
        },
      },
      WorkerJobDetailResponse: {
        type: "object",
        properties: {
          data: { type: "object", additionalProperties: true },
          events: { type: "array", items: { type: "object", additionalProperties: true } },
        },
      },
      ProfileInput: {
        type: "object",
        required: ["ruc", "razon_social"],
        properties: {
          ruc: { type: "string" },
          razon_social: { type: "string" },
          identity_json: { type: "object", additionalProperties: true },
          finance_json: { type: "object", additionalProperties: true },
          experience_json: { type: "array", items: { type: "object", additionalProperties: true } },
          econ_experience_json: { type: "object", additionalProperties: true },
          team_json: { type: "array", items: { type: "object", additionalProperties: true } },
          hireable_roles_json: { type: "array", items: { type: "object", additionalProperties: true } },
          equipment_json: { type: "array", items: { type: "object", additionalProperties: true } },
          certifications_json: { type: "array", items: { type: "object", additionalProperties: true } },
          company_keywords: { type: "array", maxItems: 12, items: { type: "string", maxLength: 80 } },
        },
      },
      ProfileCreateInput: {
        allOf: [
          { $ref: "#/components/schemas/ProfileInput" },
          {
            type: "object",
            properties: {
              is_active: { type: "boolean", default: true },
            },
          },
        ],
      },
      ProfilePatchInput: {
        type: "object",
        properties: {
          ruc: { type: "string" },
          razon_social: { type: "string" },
          identity_json: { type: "object", additionalProperties: true },
          finance_json: { type: "object", additionalProperties: true },
          experience_json: { type: "array", items: { type: "object", additionalProperties: true } },
          econ_experience_json: { type: "object", additionalProperties: true },
          team_json: { type: "array", items: { type: "object", additionalProperties: true } },
          hireable_roles_json: { type: "array", items: { type: "object", additionalProperties: true } },
          equipment_json: { type: "array", items: { type: "object", additionalProperties: true } },
          certifications_json: { type: "array", items: { type: "object", additionalProperties: true } },
          company_keywords: { type: "array", minItems: 1, maxItems: 12, items: { type: "string", maxLength: 80 } },
          is_active: { type: "boolean" },
        },
      },
      CompanyKeywordsInput: {
        type: "object",
        required: ["company_keywords"],
        properties: {
          company_keywords: { type: "array", minItems: 1, maxItems: 12, items: { type: "string", maxLength: 80 } },
        },
      },
      BusinessLineInput: {
        type: "object",
        required: ["nombre"],
        properties: {
          nombre: { type: "string" },
          cubso_segmentos: { type: "array", items: { type: "string" } },
          keywords: { type: "array", items: { type: "string" } },
          keyword_phrases: { type: "array", maxItems: 8, items: { type: "string" } },
          keyword_terms: { type: "array", maxItems: 8, items: { type: "string" } },
          ubigeos: { type: "array", items: { type: "string" } },
          monto_min: { anyOf: [{ type: "number" }, { type: "null" }] },
          monto_max: { anyOf: [{ type: "number" }, { type: "null" }] },
          score_umbral: { type: "integer", minimum: 0, maximum: 100 },
        },
      },
      BusinessLinePatch: {
        type: "object",
        properties: {
          nombre: { type: "string" },
          cubso_segmentos: { type: "array", items: { type: "string" } },
          keywords: { type: "array", items: { type: "string" } },
          keyword_phrases: { type: "array", maxItems: 8, items: { type: "string" } },
          keyword_terms: { type: "array", maxItems: 8, items: { type: "string" } },
          ubigeos: { type: "array", items: { type: "string" } },
          monto_min: { anyOf: [{ type: "number" }, { type: "null" }] },
          monto_max: { anyOf: [{ type: "number" }, { type: "null" }] },
          score_umbral: { type: "integer", minimum: 0, maximum: 100 },
          is_active: { type: "boolean" },
        },
      },
      CatalogBusinessLineInput: {
        allOf: [
          { $ref: "#/components/schemas/BusinessLineInput" },
          {
            type: "object",
            properties: {
              profile_id: { type: "string", format: "uuid" },
              is_active: { type: "boolean", default: true },
            },
          },
        ],
      },
      CubsoSegmentInput: {
        type: "object",
        required: ["codigo", "anio", "nombre"],
        properties: {
          codigo: { type: "string" },
          anio: { type: "integer", minimum: -32768, maximum: 32767 },
          nombre: { type: "string" },
          raw_json: { type: "object", additionalProperties: true },
        },
      },
      CubsoSegmentPatch: {
        type: "object",
        properties: {
          nombre: { type: "string" },
          raw_json: { type: "object", additionalProperties: true },
        },
      },
      EnabledCubsoSegmentInput: {
        type: "object",
        required: ["codigo", "anio", "bucket"],
        properties: {
          codigo: { type: "string" },
          anio: { type: "integer" },
          bucket: { type: "string" },
          enabled: { type: "boolean", default: true },
          notes: { anyOf: [{ type: "string" }, { type: "null" }] },
        },
      },
      EnabledCubsoSegmentPatch: {
        type: "object",
        required: ["bucket"],
        properties: {
          bucket: { type: "string" },
          enabled: { type: "boolean" },
          notes: { anyOf: [{ type: "string" }, { type: "null" }] },
        },
      },
      SeaceCatalogInput: {
        type: "object",
        required: ["codigo", "nombre"],
        properties: {
          codigo: { type: "integer", minimum: -32768, maximum: 32767 },
          nombre: { type: "string" },
        },
      },
      SeaceCatalogPatch: {
        type: "object",
        required: ["nombre"],
        properties: {
          nombre: { type: "string" },
        },
      },
      DocumentInput: {
        type: "object",
        required: ["id_contrato_archivo", "categoria", "filename", "sha256_original", "seace_download_url"],
        properties: {
          id_contrato_archivo: { type: "integer", minimum: 1 },
          categoria: { type: "integer", minimum: -32768, maximum: 32767 },
          filename: { type: "string" },
          mime: { anyOf: [{ type: "string" }, { type: "null" }] },
          doc_class: { anyOf: [{ type: "string" }, { type: "null" }] },
          has_text_layer: { type: "boolean" },
          size_original_bytes: { type: "integer" },
          size_preview_bytes: { type: "integer" },
          sha256_original: { type: "string" },
          sha256_preview: { anyOf: [{ type: "string" }, { type: "null" }] },
          r2_preview_key: { anyOf: [{ type: "string" }, { type: "null" }] },
          seace_download_url: { type: "string" },
          raw_file_json: { type: "object", additionalProperties: true },
        },
      },
      DocumentPatch: {
        type: "object",
        properties: {
          id_contrato_archivo: { type: "integer", minimum: 1 },
          categoria: { type: "integer", minimum: -32768, maximum: 32767 },
          filename: { type: "string" },
          mime: { anyOf: [{ type: "string" }, { type: "null" }] },
          doc_class: { anyOf: [{ type: "string" }, { type: "null" }] },
          has_text_layer: { type: "boolean" },
          size_original_bytes: { type: "integer" },
          size_preview_bytes: { type: "integer" },
          sha256_original: { type: "string" },
          sha256_preview: { anyOf: [{ type: "string" }, { type: "null" }] },
          r2_preview_key: { anyOf: [{ type: "string" }, { type: "null" }] },
          seace_download_url: { type: "string" },
          raw_file_json: { type: "object", additionalProperties: true },
        },
      },
      ContractFacetInput: {
        type: "object",
        required: ["facet", "label"],
        properties: {
          facet: { type: "string" },
          label: { type: "string" },
          required: { type: "boolean", default: true },
          details_json: { type: "object", additionalProperties: true },
          evidence_json: { type: "array", items: { type: "object", additionalProperties: true } },
        },
      },
      FacetInput: {
        type: "object",
        required: ["facet", "label"],
        properties: {
          extraction_id: { anyOf: [{ type: "integer" }, { type: "null" }] },
          facet: { type: "string" },
          label: { type: "string" },
          required: { type: "boolean", default: true },
          details_json: { type: "object", additionalProperties: true },
          evidence_json: { type: "array", items: { type: "object", additionalProperties: true } },
          facet_hash: { type: "string" },
          is_current: { type: "boolean", default: true },
        },
      },
      FacetPatch: {
        type: "object",
        properties: {
          extraction_id: { anyOf: [{ type: "integer" }, { type: "null" }] },
          facet: { type: "string" },
          label: { type: "string" },
          required: { type: "boolean" },
          details_json: { type: "object", additionalProperties: true },
          evidence_json: { type: "array", items: { type: "object", additionalProperties: true } },
          facet_hash: { type: "string" },
          is_current: { type: "boolean" },
        },
      },
      TenantMemberInput: {
        type: "object",
        required: ["email"],
        properties: {
          email: { type: "string", format: "email" },
          name: { type: "string" },
          role: { type: "string", default: "member" },
        },
      },
      TenantMemberPatch: {
        type: "object",
        properties: {
          role: { type: "string" },
        },
      },
      TenantPatch: {
        type: "object",
        properties: {
          name: { type: "string" },
          plan: { type: "string" },
        },
      },
      MatchPatch: {
        type: "object",
        properties: {
          user_state: { type: "string" },
          responsable_id: { anyOf: [{ type: "string", format: "uuid" }, { type: "null" }] },
          monto_ofertado: { anyOf: [{ type: "number" }, { type: "null" }] },
          notas: { anyOf: [{ type: "string" }, { type: "null" }] },
        },
      },
      TrackContractInput: {
        type: "object",
        properties: {
          profile_id: { type: "string", format: "uuid" },
          user_state: {
            type: "string",
            enum: [
              "inbox",
              "en_evaluacion",
              "interesada",
              "en_preparacion",
              "postulada",
              "ganada",
              "perdida",
              "desierta",
              "en_ejecucion",
              "cobrada",
              "descartada",
            ],
            default: "interesada",
          },
          responsable_id: { anyOf: [{ type: "string", format: "uuid" }, { type: "null" }] },
          monto_ofertado: { anyOf: [{ type: "number" }, { type: "null" }] },
          notas: { anyOf: [{ type: "string" }, { type: "null" }] },
        },
      },
      MatchEventInput: {
        type: "object",
        required: ["event_type"],
        properties: {
          event_type: { type: "string" },
          payload: { type: "object", additionalProperties: true },
        },
      },
      MatchTaskInput: {
        type: "object",
        required: ["title"],
        properties: {
          title: { type: "string" },
          status: { type: "string", enum: ["pending", "done", "blocked", "skipped"], default: "pending" },
          source: { type: "string", enum: ["system", "facet", "manual"], default: "manual" },
          due_at: { anyOf: [{ type: "string", format: "date-time" }, { type: "null" }] },
          metadata_json: { type: "object", additionalProperties: true },
        },
      },
      MatchTaskPatch: {
        type: "object",
        properties: {
          title: { type: "string" },
          status: { type: "string", enum: ["pending", "done", "blocked", "skipped"] },
          due_at: { anyOf: [{ type: "string", format: "date-time" }, { type: "null" }] },
          metadata_json: { type: "object", additionalProperties: true },
        },
      },
      NotificationInput: {
        type: "object",
        required: ["user_id", "channel", "reason"],
        properties: {
          user_id: { type: "string", format: "uuid" },
          match_id: { anyOf: [{ type: "integer" }, { type: "null" }] },
          channel: { type: "string", enum: ["email", "telegram", "in_app"] },
          reason: { type: "string" },
          payload: { type: "object", additionalProperties: true },
          status: { type: "string", default: "queued" },
        },
      },
      NotificationPatch: {
        type: "object",
        properties: {
          channel: { type: "string", enum: ["email", "telegram", "in_app"] },
          reason: { type: "string" },
          payload: { type: "object", additionalProperties: true },
          status: { type: "string" },
          sent_at: { anyOf: [{ type: "string", format: "date-time" }, { type: "null" }] },
        },
      },
      NotificationPreferenceInput: {
        type: "object",
        required: ["channel"],
        properties: {
          channel: { type: "string", enum: ["email", "telegram", "in_app"] },
          mode: { type: "string", enum: ["realtime", "digest"], default: "realtime" },
          max_alerts_per_day: { type: "integer", minimum: 1, default: 5 },
          quiet_hours_json: { type: "object", additionalProperties: true },
        },
      },
      InternalJobInput: {
        type: "object",
        required: ["job_type"],
        properties: {
          job_type: { type: "string" },
          queue_name: { type: "string", enum: ["io", "llm", "match", "notify"], default: "io" },
          payload: { type: "object", additionalProperties: true },
          dedup_key: { anyOf: [{ type: "string" }, { type: "null" }] },
          priority: { type: "integer", minimum: 1, maximum: 10, default: 5 },
        },
      },
      MarkWorkerJobDeadInput: {
        type: "object",
        properties: {
          reason: { type: "string" },
        },
      },
    },
  },
} as const;
