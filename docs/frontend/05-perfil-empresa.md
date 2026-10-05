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
8. Biblioteca de postulación:
   - datos reutilizables con nombre libre
   - tags escritos por el usuario, sin dropdowns ni categorias cerradas
   - documentos de respaldo con archivo, nombre, tags y descripcion
   - constancias RNP, CCI, certificados, facturas, contratos, consorcios y plantillas
   - vigencia, monto, entidad, consorcio u observaciones dentro de descripcion si aplica
   - todo lo guardado desde la UI alimenta el contexto del agente por defecto

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
features/profile/components/CompanyLibraryPanel/
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
GET /api/profile/library
POST /api/profile/library/knowledge
PATCH /api/profile/library/knowledge/:itemId
DELETE /api/profile/library/knowledge/:itemId
POST /api/profile/library/documents
PATCH /api/profile/library/documents/:documentId
GET /api/profile/library/documents/:documentId
DELETE /api/profile/library/documents/:documentId
```

Guardar perfil o lineas encola `match_profile` en backend.

## UX

### Rework de capacidad y equipo (2026-10-02)

Por solicitud del usuario, la composición pasa del rail estrecho de capacidad a seis secciones de ancho completo: Radar, Equipo, Experiencia, Recursos, Documentos y Empresa. Mantiene materiales, controles, jerarquía y shell de `09-perfil-empresa.png` y del Components Kit aprobado; la navegación progresiva sustituye la sábana de formularios. Esta especificación reemplaza las indicaciones de rail de capacidad de la versión anterior.

- Radar conserva líneas y keywords; Empresa contiene identidad/RNP/CCI y conexión SEACE independiente.
- Equipo: ficha por profesional con rol, nombre opcional (sin DNI), grado, carrera, experiencia en años, colegiatura y especialidad. Cada ficha admite múltiples experiencias, capacitaciones/certificados y respaldo por archivo. Perfiles contratables plegados y separados de profesionales disponibles.
- Experiencia: montos acreditables de servicios/bienes y contratos individuales con entidad, objeto, monto, año y conformidad.
- Recursos: certificaciones/seguros de empresa y equipamiento, con altas individuales. No convertir registros ricos en CSV.
- Se conserva la metadata desconocida y los strings heredados; solamente el registro editado se normaliza a objeto. No requiere migración SQL ni modifica workers/modelos.
- Persistencia en las columnas JSONB existentes por `PUT /api/profile`. Campos personales: `team_json[].{role,nombre,grado,carrera,experiencia_anios,colegiatura,especialidad,experience[],certifications[],documents[]}`. Contratos conservan `{objeto,entidad,monto,anio}`. Certificados usan `{nombre,entidad,fecha,valid_until,horas,descripcion}` según contexto.
- `documents[]` contiene referencias `{id,title,filename,downloadUrl}` devueltas por `POST /api/profile/library/documents`; la descarga usa la ruta tenant-safe por ID, no una URL externa introducida en JSON. Archivos hasta 10 MB en los formatos existentes. Desvincular una ficha no borra archivos de la biblioteca.
- Subir respaldo **no extrae automáticamente su contenido**: matching recibe datos declarados y referencias, no lectura de los archivos. No representar adjuntos como acreditación verificada.
- Borrado con confirmación, validación de nuevos registros incluso si están plegados o en otra sección, guardado bloqueado durante subida, prevención de salida con cambios pendientes y error recuperable. RUC existente no editable para evitar crear accidentalmente otro perfil.
- Guardar conserva keywords del radar, actualiza el resumen y encola el rematch existente. Los puntajes documentales requieren reevaluación, no se prometen inmediatos.
- «Secciones con datos» reemplaza «Completitud»: mide presencia, no calidad ni cumplimiento. Mobile reduce el resumen y revela el editor sin un rail lateral.

QA: `node --test scripts/profile_records.test.mjs` (8 casos); `scripts/qa_profile.py` (datos reales de lectura, todas las escrituras/subidas interceptadas; no altera producción). Capturas `docs/new-style/qa/profile-{radar,team,editor}-{desktop,laptop,mobile}-20261002.png` y `profile-team-list-mobile-20261002.png`. Guardado compatible y upload payload comprobados en navegador; no se realizó una subida real de QA a R2.

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
- La biblioteca debe aceptar solo el flujo simple visible: nombre, tags libres y descripcion; para documentos se suma archivo.
- No agregar dropdowns, vencimientos, montos, entidad o toggles visibles en la biblioteca salvo que exista una necesidad real validada.
- Los archivos de biblioteca alimentan al agente como metadata/contexto seleccionado; no enviar archivos completos por defecto si no son necesarios.
- Mostrar documentos y datos en una superficie de trabajo escaneable con busqueda, acciones claras y formulario progresivo.

## Estados

### Montos de experiencia por área (2026-10-05)

La sección Experiencia comienza con «Experiencia económica por área» y «Agregar área».
Cada registro de `econ_experience_json.areas[]` contiene `rubro`, `especialidad`,
`tipo_objeto`, `monto`, `moneda`, `descripcion` y referencias opcionales `documents[]`.
Las filas plegadas muestran su monto y moneda individual. Se requiere rubro,
monto no negativo y moneda PEN/USD/EUR; admite hasta 100 registros. Guardar conserva
los montos generales anteriores y cualquier metadata, sin sumar ni convertir monedas.
Los valores anteriores de bienes/servicios permanecen plegados y no se asignan a
áreas automáticamente. El resumen del perfil cuenta áreas, no presenta una suma.

Las áreas viajan como datos al LLM2. Si existen áreas, los guards PROD4/PROD6 ya no
usan el máximo global para acreditar un requisito económico: señalan revisión de
especialidad, moneda y respaldo. No se implementa aún la selección automática de
experiencia admisible por TDR. No se ejecuta reevaluación ni Gemini en el despliegue.
QA de guardado y preservación: `scripts/qa_profile_economic_areas.py`, escrituras
interceptadas y capturas desktop/laptop/mobile.

### Experiencia por contrato y especialidad (2026-10-05)

Experiencia prioriza contratos agregables, con tres grupos de edición: Contrato y especialidad, Monto y participación, Fechas y respaldo. Se persisten en `experience_json` los campos `objeto`, `entidad`, `numero_contrato`, `rubro`, `especialidad`, `tipo_objeto`, `actividades`, `monto`, `moneda`, `modalidad_participacion`, `porcentaje_participacion`, `alcance_participacion`, `fecha_inicio`, `fecha_fin`, `fecha_conformidad`, `anio`, `acreditacion`, `conformidad`, `descripcion` y `documents[]`.

Los montos anteriores de `econ_experience_json` se conservan bajo divulgación progresiva, sin sumar contratos ni convertir monedas automáticamente. No se declara un contrato como «general» o «específico» de forma universal: su compatibilidad depende de cada convocatoria. `documentada` significa respaldo declarado, no verificación externa. El worker ya recibe `experience_json`, pero esta ampliación no cambia todavía sus pesos ni la regla económica heredada. No requiere migración SQL.

La API y el cliente validan moneda/objeto/participación/respaldo, montos no negativos, porcentaje de 0 a 100 y fechas válidas. Registros legacy y metadata desconocida se conservan sin reescritura. QA local intercepta todas las escrituras: `scripts/qa_profile_experience.py`.

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
- El usuario puede guardar datos libres y documentos de respaldo con tags, descargarlos y eliminarlos de forma tenant-safe.
