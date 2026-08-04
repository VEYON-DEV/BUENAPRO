# Vista 05 - Perfil de empresa

## Objetivo

Construir el perfil que permite comparar oportunidades contra capacidad real. Es la vista mas importante para que el matching tenga sentido.

## Usuario

Proveedor que quiere recibir oportunidades relevantes. Puede no saber CUBSO ni tener todo ordenado, asi que el formulario debe guiar sin abrumar.

## Ruta

```text
/perfil
```

## Secciones

1. Identidad:
   - RUC
   - razon social
   - RNP
   - CCI
2. Lineas de negocio:
   - nombre
   - segmentos CUBSO
   - keywords
   - regiones/ubigeos
   - rango de monto
   - umbral de score
3. Experiencia economica:
   - monto general acreditable
4. Contratos previos:
   - entidad
   - objeto
   - monto
   - anio
   - conformidad
5. Equipo:
   - rol
   - formacion
   - experiencia
   - licencias
   - capacitaciones
6. Roles contratables:
   - perfiles que el usuario puede conseguir.
7. Equipamiento/certificaciones/seguros.

## Componentes

```text
features/profile/components/ProfileWizard/
features/profile/components/ProfileSectionNav/
features/profile/components/IdentityForm/
features/profile/components/BusinessLinesEditor/
features/profile/components/BusinessLineCard/
features/profile/components/EconomicExperienceEditor/
features/profile/components/PastContractsEditor/
features/profile/components/TeamEditor/
features/profile/components/HireableRolesEditor/
features/profile/components/EquipmentEditor/
features/profile/components/CertificationsEditor/
features/profile/components/ProfileCompleteness/
```

## Backend

```text
GET /api/profile
PUT /api/profile
GET /api/profiles
POST /api/profiles
PATCH /api/profiles/:id
GET /api/lines
POST /api/lines
PATCH /api/lines/:id
DELETE /api/lines/:id
GET /api/catalogs/cubso/segments
GET /api/catalogs/enabled-cubso-segments
GET /api/catalogs/ubigeo
GET /api/integrations/seace
PUT /api/integrations/seace
DELETE /api/integrations/seace
```

Guardar perfil o lineas encola `match_profile` en backend.

## UX

- No mostrar todo como una sabana interminable.
- Usar secciones colapsables o tabs verticales.
- Guardado por seccion.
- Mostrar progreso de completitud.
- Avisar que el feed puede tardar unos segundos en recalcular.
- Mantener la conexión SEACE separada del perfil empresarial. La contraseña se envía una sola vez, se cifra en servidor y nunca vuelve al navegador.
- Priorizar líneas de negocio sobre datos secundarios: son el radar principal del producto.
- Mostrar cada línea con segmentos CUBSO, cobertura y keywords; editarla inline sin navegar ni abrir modal.
- Permitir hasta 30 keywords por línea mediante chips editables; Enter o coma agrega una keyword.
- Mantener identidad y capacidad en un rail secundario compacto, con equipo/recursos y contratos bajo divulgación progresiva.
- No pedir facturación anual: el matching económico usa exclusivamente experiencia acreditable (`econ_experience_json`).
- Ocultar la acción de guardado mientras el formulario no tenga cambios.
- Usar la misma jerarquía BuenaPro Glass de Inicio y Mercado: resumen ambiental claro, líneas sobre superficie de trabajo blanca y capacidad/conexión en rail secundario.
- Mantener chips, inputs, disclosures, botones, radios y profundidad coherentes con el resto del producto; evitar cabeceras oscuras o paneles planos heredados.

## Estados

- Sin perfil: formulario inicial.
- Perfil incompleto: mostrar pendientes.
- Guardando: bloquear solo la seccion.
- Error de validacion: mensaje cerca del campo.
- Rematch en progreso: estado discreto.

## Criterios de done

- Crear perfil desde cero.
- Editar perfil existente.
- Crear/editar/desactivar lineas.
- Al guardar, el feed empieza a generar matches.
- La UI no requiere entender JSON.
