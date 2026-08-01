# BuenaPro: contexto funcional completo

## 1. Propósito de este documento

Este documento describe BuenaPro únicamente desde el punto de vista funcional.

Explica:

- qué problema resuelve el producto;
- quiénes lo usan;
- qué información administra;
- cuáles son sus rutas y módulos;
- qué contiene cada pantalla;
- qué campos tienen los formularios;
- qué columnas tienen las tablas;
- qué ocurre después de cada acción;
- qué estados vacíos, de carga y de error deben existir;
- qué funciones están disponibles actualmente;
- qué funciones todavía no deben considerarse disponibles.

Este documento no define colores, tipografías, tamaños, espaciados, distribución visual, componentes gráficos ni ninguna otra decisión de diseño. Puede utilizarse como contexto funcional para explorar una capa de diseño en una herramienta externa sin alterar las reglas del producto.

## 2. Resumen del producto

BuenaPro es una plataforma de inteligencia y operación para proveedores que participan en contratos menores publicados en SEACE, el sistema peruano de contrataciones públicas.

El producto ayuda a una empresa proveedora a responder cinco preguntas:

1. ¿Qué oportunidades públicas están vigentes?
2. ¿Cuáles pertenecen realmente a los rubros que atiende mi empresa?
3. ¿Qué requisitos exige cada oportunidad y qué tan preparada está mi empresa para cumplirlos?
4. ¿Qué antecedentes existen para estimar precio, demanda, riesgo de desierto y competidores?
5. ¿Qué falta completar para preparar una postulación?

BuenaPro no reemplaza a SEACE. SEACE continúa siendo la fuente oficial de contratos, fechas, documentos, consultas, estados y resultados. BuenaPro organiza esa información, la analiza y ayuda al usuario a preparar una decisión y un borrador de postulación.

## 3. Alcance operativo actual

El alcance principal está centrado en contratos menores de servicios.

La ingesta operativa inicial cubre segmentos CUBSO configurados, con énfasis actual en:

- tecnología;
- transporte;
- servicios legales y profesionales.

El histórico de mercado disponible cubre principalmente los segmentos tecnológicos 43 y 81. La base histórica se amplía mediante procesos automáticos de backfill y seguimiento de cierres.

El sistema puede:

- descubrir oportunidades nuevas;
- guardar oportunidades;
- incorporarlas a seguimiento;
- analizar un TDR contra el perfil de una empresa;
- mostrar documentos originales;
- mostrar requisitos extraídos;
- consultar información oficial de SEACE bajo demanda;
- comparar una oportunidad con contratos históricos;
- preparar precios, RTM, contacto y adjuntos en un borrador;
- conservar actividad, responsables y avance;
- generar alertas por afinidad;
- explorar contratos adjudicados, desiertos, entidades y proveedores.

El sistema todavía no puede enviar oficialmente una cotización o una consulta a SEACE. Toda postulación permanece como borrador hasta que el usuario realice el envío por el canal oficial correspondiente.

## 4. Usuarios y espacios de trabajo

### 4.1 Empresa proveedora

Es la organización que busca oportunidades y prepara postulaciones. Cada empresa opera dentro de un espacio de trabajo o `tenant`.

El espacio de trabajo contiene:

- identidad de la empresa;
- perfil de capacidades;
- líneas de negocio;
- keywords generales y específicas;
- oportunidades guardadas;
- análisis realizados;
- postulaciones y borradores;
- preferencias de alerta;
- conexión SEACE;
- integrantes y responsables;
- conversaciones con el copiloto.

### 4.2 Propietario de la cuenta

Es el usuario que registra inicialmente la empresa. Al crear la cuenta se crean:

- el usuario;
- el espacio de trabajo;
- la membresía del usuario como propietario.

### 4.3 Integrante del equipo

Puede ser asignado como responsable de una postulación. El alcance futuro puede incluir diferentes permisos, pero el flujo actual se concentra en la responsabilidad operativa del expediente.

### 4.4 Operador interno de BuenaPro

Es un usuario técnico que revisa workers, lotes, errores de ingesta, extracciones y salud de SEACE. Sus funciones no pertenecen a la navegación normal de una empresa proveedora.

## 5. Conceptos del dominio

### 5.1 Oportunidad

Es una contratación publicada en SEACE que BuenaPro ha importado. Su identificador estable es `idContrato` de SEACE.

Una oportunidad puede incluir:

- código completo, por ejemplo `CM-110-2026-UNAAT`;
- entidad contratante;
- área usuaria;
- descripción del objeto;
- tipo de objeto;
- segmento y código CUBSO;
- ubicación;
- fecha de publicación;
- inicio y fin de consultas;
- inicio y fin de cotización;
- estado oficial;
- indicación de si permite cotizar;
- documentos oficiales;
- ítems;
- requisitos;
- entregables;
- condiciones de pago;
- penalidades;
- experiencia económica exigida.

### 5.2 Código de contratación

Un código como `CM-110-2026-UNAAT` se conserva completo y también puede separarse en:

- tipo: `CM`;
- correlativo: `110`;
- año: `2026`;
- sigla de entidad: `UNAAT`.

El código sirve para búsqueda y reconocimiento humano. No sustituye al `idContrato` como llave estable.

### 5.3 Línea de negocio

Representa una familia concreta de servicios que ofrece la empresa.

Cada línea contiene:

- nombre;
- entre uno y tres segmentos CUBSO;
- hasta treinta keywords;
- estado activo o inactivo.

Los segmentos abren el universo de contratos y las keywords ayudan a priorizar coincidencias dentro de ese universo.

### 5.4 Perfil de empresa

Es el conjunto de evidencias empresariales usadas para evaluar requisitos:

- RUC y razón social;
- RNP y CCI;
- experiencia económica acreditable;
- contratos previos;
- equipo;
- roles que se pueden contratar;
- equipamiento;
- certificaciones y seguros;
- líneas de negocio y keywords.

### 5.5 Afinidad de rubro

Es una señal previa calculada con CUBSO y keywords. Sirve para ordenar oportunidades antes de realizar un análisis completo.

No significa por sí sola que la empresa cumple todos los requisitos.

### 5.6 Análisis contra el perfil

Es una evaluación bajo demanda de una oportunidad contra el perfil activo de la empresa.

Produce:

- veredicto;
- score de 0 a 100;
- resumen;
- estado de cada requisito;
- brechas;
- acciones recomendadas.

Los veredictos disponibles son:

- verde: alta viabilidad;
- ámbar: viable con acciones;
- rojo: existe una brecha crítica;
- gris: requiere revisión.

### 5.7 Postulación

Es el seguimiento comercial y operativo de una oportunidad. Puede tener responsable, estado, monto ofertado, tareas, comentarios y un borrador de expediente.

### 5.8 Borrador de postulación

Es el expediente editable que reúne:

- ítems seleccionados;
- precios unitarios y totales;
- vigencia de la oferta;
- datos de contacto;
- respuestas RTM;
- documentos propios;
- formatos oficiales de la entidad;
- responsable;
- avance.

El borrador no equivale a una presentación oficial.

### 5.9 Resultado histórico

Es una contratación culminada clasificada como:

- `ADJUDICADO`: tiene precio y/o proveedor ganador;
- `DESIERTO`: terminó sin adjudicatario;
- `SIN_RESULTADO`: culminó sin información suficiente para confirmar ganador o desierto.

Los desiertos se cuentan como señal de riesgo y volumen, pero no se usan para calcular precios.

### 5.10 Comparable histórico

Es un resultado anterior relacionado con una oportunidad actual mediante:

- mismo CUBSO;
- familia CUBSO relacionada;
- keywords o frases coincidentes;
- misma entidad;
- recencia.

Cada comparable debe indicar por qué fue considerado similar.

## 6. Origen y recorrido de los datos

### 6.1 Fuente oficial

SEACE es la fuente de verdad externa. BuenaPro obtiene del servicio oficial:

- listados de contrataciones;
- detalle de una contratación;
- cronograma;
- ubicación;
- CUBSO;
- documentos;
- consultas oficiales;
- contexto de cotización;
- estados y resultados.

### 6.2 Procesos automáticos

El sistema opera varios procesos:

1. `poll_search`: busca oportunidades vigentes nuevas o modificadas.
2. `process_contract`: obtiene detalle, documentos y datos operativos.
3. extracción de TDR: estructura requisitos y condiciones a partir de documentos PDF.
4. `poll_lifecycle`: revisa cambios de estado, cronograma y archivos.
5. `recent_closures`: revisa contratos cuyo cierre ocurrió recientemente.
6. `historical_backfill`: recorre resultados culminados para poblar el histórico.
7. matching y alertas: recalcula señales cuando cambian contratos o perfiles.

### 6.3 Persistencia

PostgreSQL en la VM es la fuente de verdad interna de BuenaPro. El navegador consulta los datos mediante las APIs de la aplicación y no mantiene una copia histórica independiente.

Los documentos originales siguen siendo descargados desde SEACE. BuenaPro conserva sus referencias y metadatos.

## 7. Mapa de navegación

### 7.1 Navegación principal

La navegación principal tiene exactamente tres destinos:

| Opción | Ruta | Función |
|---|---|---|
| Oportunidades | `/feed` | Descubrir, filtrar, guardar y evaluar contrataciones. |
| Mercado | `/mercado` | Explorar resultados históricos, precios, demanda y competidores. |
| Postulaciones | `/postulaciones` | Gestionar oportunidades incorporadas al seguimiento y continuar borradores. |

### 7.2 Accesos de cuenta

El menú de cuenta contiene:

| Opción | Ruta o acción | Función |
|---|---|---|
| Mi empresa y radar | `/perfil` | Editar identidad, capacidad, líneas, keywords y conexión SEACE. |
| Alertas | `/alertas` | Configurar la exigencia del radar y revisar avisos recientes. |
| Cerrar sesión | acción de autenticación | Finalizar la sesión y regresar al login. |

También existe un acceso directo a `Mi empresa y radar` asociado a la identidad de la empresa.

### 7.3 Accesos globales

- La marca BuenaPro lleva a `/feed`.
- La campana lleva a `/alertas`.
- El indicador `SEACE` comunica el contexto de origen de los contratos.
- El contexto de cada pantalla muestra el nombre del módulo actual.

### 7.4 Rutas derivadas

| Ruta | Comportamiento |
|---|---|
| `/` | Redirige a `/feed`. |
| `/seguimiento` | Redirige a `/postulaciones`. |
| `/configuracion` | Redirige a `/alertas`. |

No existe una pantalla de Inicio separada. La entrada principal del producto es Oportunidades.

### 7.5 Rutas internas no visibles para el usuario normal

| Ruta | Función |
|---|---|
| `/admin` | Diagnóstico técnico de ingesta y workers. |
| `/docs` | Documentación OpenAPI de los endpoints. |

Estas rutas no deben agregarse a la navegación habitual de una empresa proveedora.

## 8. Autenticación

## 8.1 Iniciar sesión

Ruta: `/login`.

### Campos

| Campo | Tipo | Obligatorio | Regla |
|---|---|---:|---|
| Email | correo | Sí | Debe tener formato de email. |
| Contraseña | contraseña | Sí | Mínimo ocho caracteres. |

### Acciones

- `Entrar`: valida las credenciales.
- `Crear cuenta`: navega a `/registro`.

### Resultado

- Si las credenciales son correctas, el usuario entra a `/feed`.
- Si son incorrectas, se informa que no fue posible iniciar sesión con esos datos.
- Si falta configuración del proveedor de autenticación, se informa un error de configuración.
- Mientras se procesa la solicitud, la acción permanece en estado de procesamiento.

## 8.2 Crear cuenta

Ruta: `/registro`.

### Campos

| Campo | Tipo | Obligatorio | Regla |
|---|---|---:|---|
| Tu nombre | texto | Sí | Nombre de la persona propietaria. |
| Empresa | texto | Sí | Nombre inicial del espacio de trabajo. |
| Email | correo | Sí | Será la credencial de acceso. |
| Contraseña | contraseña | Sí | Mínimo ocho caracteres. |

### Acciones

- `Crear cuenta`: crea usuario, empresa y membresía.
- `Ingresar`: vuelve a `/login` cuando el usuario ya tiene una cuenta.

### Resultado

Después de un registro correcto, el usuario es enviado a `/onboarding`.

## 8.3 Protección de rutas

Las rutas privadas requieren una sesión válida. Sin sesión, el usuario debe ser redirigido a `/login`.

Todas las consultas empresariales deben usar el `tenant_id` de la sesión. Un usuario no puede leer ni editar perfil, matches, postulaciones, archivos o alertas de otra empresa.

## 9. Onboarding inicial

Ruta: `/onboarding`.

El onboarding tiene cuatro pasos. Su objetivo es crear la información mínima para descubrir oportunidades y evaluarlas.

## 9.1 Paso 1: Empresa

### Campos

| Campo | Tipo | Obligatorio | Regla |
|---|---|---:|---|
| Razón social | texto | Sí | Nombre legal o comercial de la empresa. |
| RUC | numérico | Sí | Hasta once dígitos; se eliminan caracteres no numéricos. |
| Sitio web | URL | Condicional | Es opcional si se proporciona una descripción. |
| Descripción de servicios | texto largo | Condicional | Máximo 1,200 caracteres; es obligatoria cuando no hay sitio web. |

### Acción principal

`Preparar mi perfil` envía la información para analizar la empresa.

### Validaciones

- Deben existir razón social y RUC.
- Debe existir sitio web o descripción.

### Resultado del análisis

El sistema puede sugerir:

- resumen de la empresa;
- keywords generales;
- líneas de negocio;
- keywords por línea;
- segmentos CUBSO relacionados.

El usuario conserva control sobre lo que finalmente se guarda.

### Acción secundaria

`Completar después` lleva al usuario a `/feed` sin terminar el onboarding.

## 9.2 Paso 2: Líneas de negocio

### Keywords generales

- Representan la identidad transversal de la empresa.
- Se aplican a todas las líneas.
- Se pueden registrar hasta doce.
- Se agregan al confirmar con Enter o coma.
- Se pueden eliminar individualmente.

### Cada línea de negocio contiene

| Campo | Regla |
|---|---|
| Nombre | Obligatorio. |
| Segmentos CUBSO | Mínimo uno, máximo tres. |
| Keywords | Mínimo una, máximo treinta. |

El usuario puede:

- editar el nombre;
- agregar segmentos;
- quitar segmentos;
- revisar si un segmento tiene cobertura operativa;
- agregar y quitar keywords;
- eliminar la línea;
- crear una nueva línea.

### Validación para avanzar

- Debe existir al menos una keyword general.
- Debe existir al menos una línea.
- Cada línea debe tener nombre, segmento y keywords.

### Acciones

- `Atrás`: vuelve al paso Empresa.
- `Confirmar líneas`: valida y avanza a Capacidad.

## 9.3 Paso 3: Capacidad

### Campos

| Campo | Tipo | Regla |
|---|---|---|
| Monto de experiencia económica acreditable | moneda | Debe ser cero o positivo. |
| Equipo humano | lista de términos | Roles o perfiles disponibles. |
| Recursos y equipamiento | lista de términos | Herramientas, vehículos o activos. |

Los términos se agregan con Enter o coma y se pueden eliminar individualmente.

### Acciones

- `Atrás`: vuelve a Líneas.
- `Revisar perfil`: valida y avanza a Revisión.

## 9.4 Paso 4: Revisión

La revisión resume:

- razón social;
- RUC;
- número de líneas activas;
- cantidad total de keywords únicas;
- monto acreditable;
- cantidad de perfiles de equipo;
- cantidad de recursos;
- keywords generales;
- nombre, CUBSO y principales keywords de cada línea.

### Acciones

- `Atrás`: vuelve a Capacidad.
- `Activar y ver oportunidades`: guarda el perfil, las líneas y la capacidad; luego navega a `/feed?onboarding=completed`.

### Efectos posteriores

- El perfil queda activo.
- Las líneas comienzan a controlar el radar.
- Las oportunidades pueden ordenarse por afinidad.
- Se puede realizar el análisis completo de una oportunidad.

## 10. Oportunidades

Ruta: `/feed`.

## 10.1 Objetivo

Permitir que la empresa descubra contratos, priorice coincidencias y seleccione una oportunidad para evaluarla o incorporarla a seguimiento.

## 10.2 Modos funcionales

### Explorar

Se utiliza cuando no hay perfil o cuando no existen coincidencias calculadas. Muestra contratos importados desde SEACE.

### Priorizado

Se utiliza cuando existen líneas y señales de afinidad. Ordena las oportunidades considerando análisis, afinidad y fecha de cierre.

## 10.3 Contexto mostrado

- cantidad total de resultados para el filtro;
- nombre de la empresa;
- hasta tres líneas de negocio activas;
- modo actual: Explorar o Priorizado.

## 10.4 Filtros rápidos

| Filtro | Resultado |
|---|---|
| Guardadas | Solo oportunidades marcadas por el usuario. |
| No cerradas | Contratos cuyo cierre aún no pasó. |
| Cierran en 24 h | Contratos abiertos que terminan dentro de veinticuatro horas. |
| Esta semana | Contratos abiertos que terminan dentro de siete días. |
| Ya cerradas | Contratos cuyo cierre ya pasó. |
| Verdes | Oportunidades analizadas con veredicto verde. |
| Revisión | Oportunidades analizadas con veredicto gris. |
| Con monto | Oportunidades con experiencia económica informada. |
| Cotizables | Oportunidades que SEACE reporta como disponibles para cotizar. |

El histórico no aparece como una señal o filtro en esta pantalla. Los comparables se consultan dentro del detalle de cada oportunidad.

## 10.5 Filtros principales

| Campo | Opciones o comportamiento |
|---|---|
| Buscar | Busca por código, entidad u objeto. |
| Rubro | Mis rubros, Tecnología, Transporte o Legal. |
| Estado | Vigentes, Todos, En evaluación o Culminados. |
| Cierre | No cerradas, cualquier cierre, 24 horas, esta semana o ya cerradas. |

La acción `Filtrar` aplica los valores y actualiza la URL.

## 10.6 Filtros adicionales

| Campo | Opciones o comportamiento |
|---|---|
| Análisis | Con IA, Todos o Sin IA. |
| Match | Todos, Verde, Ámbar, Rojo o Revisión. |
| Cotización | Todas, Permite cotizar o No cotizable. |
| Requisito | Texto, por ejemplo licencia o equipo. |
| Rol | Texto del rol requerido. |

`Limpiar filtros` restablece la vista principal.

## 10.7 Tabla de oportunidades

Cada fila corresponde a una oportunidad.

| Columna | Contenido |
|---|---|
| Código | Código completo y estado operativo de cotización. |
| Objeto | Descripción resumida y, cuando existe, forma de pago o rol principal. |
| Entidad | Nombre de la entidad y ubicación. |
| Exp. econ. | Monto de experiencia económica exigida o ausencia de dato. |
| Plazo | Días o meses de ejecución y número de entregables. |
| Cierre | Tiempo restante y fecha/hora exacta. |
| Match | Score y veredicto, o afinidad previa cuando todavía no existe análisis completo. |
| Guardar | Acción para guardar o quitar la oportunidad. |

## 10.8 Acciones sobre una fila

### Seleccionar fila

Abre una vista rápida sin abandonar la lista.

La vista rápida carga:

- estado de cotización;
- código;
- descripción;
- score o afinidad;
- entidad;
- ubicación;
- cierre;
- experiencia económica;
- forma de pago;
- plazo;
- personal clave;
- penalidad máxima;
- cantidad de requisitos extraídos;
- cantidad de documentos.

### Abrir detalle

Navega a `/oportunidad/[idContrato]`.

### Seguir

Crea o reutiliza un match para la oportunidad y la incorpora al flujo de Postulaciones.

### Guardar

Marca la oportunidad como favorita sin incorporarla necesariamente al seguimiento. La acción es reversible.

## 10.9 Paginación

La lista muestra una página del resultado filtrado. `Anterior` y `Siguiente` conservan los filtros actuales.

## 10.10 Estados

- Sin perfil: se permite explorar y se invita a completar el perfil.
- Con perfil pero sin matches: se muestran contratos mientras se calculan señales.
- Sin resultados: se ofrece limpiar filtros.
- Error al cargar: se ofrece reintentar.
- Texto largo: debe conservarse completo en el detalle aunque se resuma en la lista.

## 11. Detalle de oportunidad

Ruta: `/oportunidad/[idContrato]`.

## 11.1 Objetivo

Responder en una sola vista:

- qué está comprando la entidad;
- cuándo cierra;
- si la empresa puede postular;
- qué requisitos existen;
- qué falta resolver;
- qué antecedentes similares existen;
- qué documentos debe revisar;
- cómo comenzar la preparación.

## 11.2 Identificación del proceso

Se muestra:

- tipo de contratación;
- tipo de objeto;
- código completo;
- estado oficial;
- estado operativo de cotización;
- descripción completa;
- entidad;
- ubicación;
- fecha y hora de cierre.

## 11.3 Acciones principales

| Acción | Resultado |
|---|---|
| Descargar TDR | Descarga el documento original desde SEACE. |
| Evaluar con mi perfil | Ejecuta el análisis de requisitos contra el perfil activo. |
| Re-evaluar | Actualiza un análisis previo cuando corresponde. |
| Comenzar postulación | Crea o reutiliza el borrador y navega al workspace. |
| Continuar postulación | Abre un borrador ya existente. |

No se debe habilitar el inicio de una cotización cerrada, salvo que ya exista una preparación en curso.

## 11.4 Resumen de decisión

Cuando existe análisis, se muestran:

- veredicto;
- score;
- principal brecha;
- experiencia económica exigida;
- resumen ejecutivo;
- acciones recomendadas.

Cuando todavía no existe análisis, se muestran:

- afinidad de rubro;
- experiencia económica exigida;
- forma de pago;
- plazo.

## 11.5 Índice funcional del detalle

| Sección | Función |
|---|---|
| Decisión | Resumen para decidir si continuar. |
| Histórico | Comparables y precios anteriores. |
| Requisitos | Requisitos extraídos del TDR. |
| Documentos | Archivos oficiales. |
| Consultas oficiales | Lectura bajo demanda desde SEACE. |
| Preparación | Brechas y acciones para postular. |
| Actividad | Comentarios y eventos del equipo. |

## 11.6 Histórico comparable

### Métricas

- cantidad total de similares;
- rango frecuente entre percentiles 25 y 75 de adjudicados;
- precio mediano;
- número de adjudicados;
- número y porcentaje de desiertos;
- proveedores frecuentes.

### Lista de comparables

Cada resultado puede mostrar:

- código completo;
- estado histórico;
- descripción;
- entidad;
- fecha de publicación;
- precio, si existe;
- razones de similitud;
- acceso al TDR antiguo, si existe documento.

Un desierto aparece como señal de mercado, pero no participa en rango ni mediana de precios.

## 11.7 Requisitos

Los requisitos se agrupan cuando corresponde en:

- capacidad legal;
- RUC;
- RNP;
- experiencia económica;
- experiencia general;
- experiencia específica;
- personal clave;
- formación académica;
- colegiatura;
- capacitación;
- licencias;
- equipamiento;
- seguros;
- certificaciones;
- documentos de propuesta;
- condiciones de pago;
- penalidades;
- condiciones de entrega;
- otros requisitos.

Cada requisito contiene un nombre y el detalle extraído. Si todavía no existe extracción, se informa que el TDR no fue procesado.

## 11.8 Ficha técnica

La ficha de referencia puede incluir:

- área usuaria;
- tipo de invitación;
- código y nombre CUBSO;
- objeto específico;
- lugar de ejecución;
- plazo;
- forma de pago;
- roles requeridos.

## 11.9 Entregables

Cada entregable muestra:

- descripción o producto;
- plazo de presentación;
- cantidad, si una condición se repite.

## 11.10 Penalidades y riesgos

Puede incluir:

- descripción de penalidad;
- fórmula de cálculo;
- tope porcentual;
- otras condiciones relevantes identificadas en el TDR.

## 11.11 Cronograma SEACE

Cada etapa muestra:

- nombre de la etapa;
- fecha y hora de inicio;
- fecha y hora de fin.

## 11.12 Documentos

Cada documento contiene:

- nombre del archivo;
- clasificación, por ejemplo TDR, especificación, anexo o cotización;
- tamaño cuando está disponible;
- acción de descarga.

Los PDF pueden tener vista previa. Los documentos Word o Excel se descargan directamente.

## 11.13 Consultas oficiales SEACE

La información se solicita a SEACE únicamente cuando el usuario lo pide.

### Acción

`Consultar SEACE` obtiene hasta diez consultas oficiales inicialmente.

### Cada consulta puede contener

- estado;
- fecha;
- pregunta;
- respuesta de la entidad, si existe.

### Estados

- sin consultas;
- consultando;
- error con reintento;
- consultas pendientes de respuesta;
- consultas respondidas.

Esta sección es de lectura. No existe envío de nuevas consultas en la versión actual.

## 11.14 Actividad y comentarios

Cuando existe un match se muestra la actividad asociada. Puede registrar decisiones, avances y comentarios. Sin match se informa que la actividad estará disponible al comenzar la preparación.

## 11.15 Qué falta

Cada brecha del análisis puede mostrar:

- estado: cumple, accionable, no cumple o revisar;
- requisito;
- diferencia encontrada;
- acción recomendada.

## 11.16 TDR

Se permite:

- abrir el original;
- ver una previsualización cuando es PDF;
- descargarlo.

## 11.17 Copiloto de licitación

El detalle permite conversar sobre:

- objeto de la contratación;
- requisitos;
- TDR;
- perfil de la empresa;
- riesgos;
- brechas;
- antecedentes disponibles.

Las respuestas deben distinguir evidencia, datos empresariales e inferencias. El copiloto no puede afirmar que una postulación fue enviada.

## 12. Postulaciones

Ruta: `/postulaciones`.

## 12.1 Objetivo

Mostrar la cartera de oportunidades que la empresa decidió seguir y permitir continuar el siguiente trabajo pendiente.

## 12.2 Estados del proceso

- inbox;
- en evaluación;
- interesada;
- en preparación;
- postulada;
- ganada;
- perdida;
- desierta;
- en ejecución;
- cobrada;
- descartada.

La lista normal excluye el estado `inbox`.

## 12.3 Resumen

Se calcula:

- total en seguimiento;
- cantidad en preparación;
- cantidad que cierra dentro de cuarenta y ocho horas.

## 12.4 Tabla de postulaciones

| Columna | Contenido |
|---|---|
| Proceso | Código, descripción y entidad. |
| Estado | Etapa actual del embudo. |
| Cierre | Tiempo restante y fecha/hora. |
| Avance | Tareas realizadas sobre total de tareas. |
| Monto | Monto ofertado o pendiente de definir. |
| Responsable | Nombre, email o sin asignar. |
| Próximo paso | Primera tarea pendiente o recomendación derivada del estado. |

Los procesos se ordenan primero por fecha de cierre y luego por actualización reciente.

## 12.5 Navegación desde una fila

- Si existe borrador y el estado es `en preparación` o `postulada`, abre `/postulaciones/[matchId]`.
- En otro caso, abre `/oportunidad/[idContrato]`.

## 12.6 Estado vacío

Cuando no hay oportunidades en seguimiento se ofrece volver a Oportunidades para marcar una como interesada o comenzar una postulación.

## 13. Workspace de postulación

Ruta: `/postulaciones/[matchId]`.

## 13.1 Objetivo

Reunir en un expediente editable todo lo necesario para preparar una cotización, sin realizar todavía el envío oficial.

## 13.2 Encabezado del expediente

Contiene:

- código del proceso;
- estado del borrador;
- descripción;
- entidad;
- fecha de cierre;
- total preparado calculado con los ítems seleccionados.

## 13.3 Índice funcional

- Oferta;
- Precio y mercado;
- RTM, incluyendo cantidad de requisitos;
- Archivos, incluyendo cantidad de adjuntos.

## 13.4 Oferta: tabla de ítems y precios

| Columna | Contenido o acción |
|---|---|
| Incluir | Selecciona si el ítem forma parte de la oferta. |
| Descripción | Nombre del ítem y unidad. |
| Cantidad | Cantidad oficial recibida desde SEACE. |
| Moneda | Moneda del ítem. |
| Precio unitario | Valor editable cuando el ítem está seleccionado. |
| Total | Cantidad multiplicada por precio unitario. |

Reglas:

- Un ítem no seleccionado no forma parte del total preparado.
- El precio debe ser cero o positivo.
- El total se calcula automáticamente.
- Los cambios se guardan en el borrador.
- Si SEACE no devuelve ítems, se informa explícitamente.

## 13.5 Vigencia y contacto

| Campo | Tipo |
|---|---|
| Vigencia de la cotización | fecha |
| Correo de contacto | correo |
| Celular de contacto | teléfono |

Los cambios se guardan automáticamente después de una breve espera.

## 13.6 Precio y mercado

Se muestran, cuando existen datos suficientes:

- cantidad de comparables;
- rango frecuente;
- precio mediano;
- porcentaje de desiertos;
- comparación del total preparado contra el rango;
- hasta cinco comparables utilizados.

La señal puede indicar:

- oferta por debajo del rango;
- oferta dentro del rango;
- oferta por encima del rango;
- necesidad de completar precios.

La referencia histórica no reemplaza la estructura de costos de la empresa.

También existe un acceso para volver a las consultas oficiales del detalle.

## 13.7 RTM

Cada requisito técnico mínimo contiene:

- número correlativo;
- descripción;
- condición solicitada, cuando existe;
- campo `RTM ofertado`.

La respuesta se guarda al salir del campo. El usuario debe registrar únicamente información que pueda acreditar.

Si no existen RTM adicionales, se informa que la licitación no los registra.

## 13.8 Tu propuesta y anexos

El usuario puede subir:

- PDF;
- Word;
- Excel.

Reglas:

- tamaño máximo: 10 MB por archivo;
- cada archivo pertenece al espacio de trabajo;
- el usuario puede descargarlo;
- el usuario puede eliminarlo;
- el sistema informa nombre y tamaño.

## 13.9 Formatos de la entidad

Se listan los archivos oficiales recibidos desde SEACE.

Cada fila contiene:

- nombre;
- tipo o estado;
- acción de descarga o indicación de que no está disponible.

Estos archivos son material de referencia. Descargarlos no modifica el avance del expediente.

## 13.10 Responsable

Un selector muestra los integrantes del espacio de trabajo.

Opciones:

- sin asignar;
- un integrante por nombre o email.

La asignación se guarda al cambiar el valor. Si falla, se restablece el valor anterior.

## 13.11 Avance

El expediente considera tres bloques:

1. Oferta y precios: hay ítems seleccionados con precios y datos de contacto completos.
2. RTM respondidos: todos los RTM tienen respuesta o no existen RTM.
3. Propuesta adjunta: existe al menos un archivo propio.

Se informa cuántos bloques están completos y si los cambios están guardándose o ya fueron guardados.

## 13.12 Envío oficial

No está habilitado. El sistema debe informar que el borrador queda preparado en BuenaPro y que la presentación en SEACE continúa bajo control del usuario.

## 13.13 Copiloto dentro del workspace

Además de responder preguntas, puede proponer cambios estructurados en:

- precios;
- RTM;
- vigencia;
- datos de contacto.

Flujo obligatorio:

1. El copiloto propone cambios.
2. El sistema valida campos e identificadores.
3. Los cambios quedan pendientes.
4. El usuario revisa la propuesta.
5. Solo la acción `Aplicar al borrador` modifica los datos.
6. El expediente conserva estado de borrador.

El copiloto no puede enviar a SEACE ni modificar el borrador sin confirmación.

## 14. Mercado

Ruta: `/mercado`.

## 14.1 Objetivo

Permitir explorar resultados culminados para entender:

- cuántos procesos existen;
- cuántos fueron adjudicados o desiertos;
- qué precios se adjudicaron;
- qué entidades compran;
- en qué regiones existe demanda;
- qué empresas ganan;
- qué contratos ganó cada empresa.

## 14.2 Alcance

### Mis mercados

Limita los resultados a los segmentos CUBSO activos del perfil de la empresa.

### Todo el mercado

Permite explorar los segmentos históricos disponibles sin modificar el perfil.

Si no existe perfil, `Mis mercados` puede quedar vacío, pero `Todo el mercado` continúa disponible.

## 14.3 Filtros

| Campo | Comportamiento |
|---|---|
| Buscar | Busca por servicio, código, entidad o proveedor. |
| Segmento | Filtra por código y nombre CUBSO. |
| Resultado | Todos, Adjudicados, Desiertos o Sin resultado. |
| Departamento | Todo el Perú o un departamento disponible. |
| Entidad | Todas o una entidad compradora. |
| Año | Todos o un año disponible. |
| Precio desde | Monto mínimo. |
| Hasta | Monto máximo o sin límite. |

`Aplicar filtros` actualiza todo el contenido de Mercado. `Limpiar` conserva el alcance y la sección actual, pero elimina los filtros.

## 14.4 Secciones

| Sección | Función |
|---|---|
| Resumen | Métricas, actividad, regiones, entidades y empresas. |
| Contratos | Tabla paginada de resultados históricos. |
| Empresas | Ranking de proveedores adjudicados. |

## 14.5 Resumen del mercado

### Métricas

| Métrica | Definición |
|---|---|
| Procesos | Total de resultados del filtro. |
| Adjudicados | Procesos con ganador confirmado. |
| Tasa desierta | Desiertos divididos por total de procesos. |
| Competidores | Proveedores adjudicados únicos. |
| Entidades | Entidades compradoras únicas. |
| Precio mediano | Mediana de precios adjudicados disponibles. |
| Rango central | Percentiles 25 y 75 de precios adjudicados. |

### Actividad del mercado

Agrupa resultados por mes y separa:

- adjudicados;
- desiertos.

### Demanda por región

Ordena departamentos por cantidad de procesos publicados.

### Entidades compradoras

Ordena entidades por cantidad de procesos dentro del filtro.

### Empresas adjudicadas

Ordena proveedores por cantidad de adjudicaciones. Cada proveedor permite abrir su detalle.

## 14.6 Tabla de contratos históricos

| Columna | Contenido |
|---|---|
| Contrato | Código completo y segmento. |
| Objeto | Descripción y nombre CUBSO. |
| Entidad y región | Entidad compradora y departamento. |
| Resultado | Adjudicado, Desierto o Sin resultado. |
| Ganador | Razón social con acceso a la empresa, cuando existe. |
| Precio | Precio total adjudicado, cuando existe. |
| Publicación | Fecha y acceso al documento histórico, cuando existe. |

La tabla muestra veinte resultados por página y conserva los filtros al navegar.

## 14.7 Lista de empresas

Cada empresa adjudicada muestra:

- posición en el ranking;
- razón social;
- RUC;
- número de adjudicaciones;
- monto acumulado;
- precio mediano;
- cantidad de entidades atendidas;
- cantidad de regiones con resultados.

Seleccionar una empresa abre `/mercado/empresas/[ruc]`.

## 14.8 Detalle de empresa

Ruta: `/mercado/empresas/[ruc]`.

### Identificación

- razón social;
- RUC;
- referencia a actividad adjudicada en SEACE.

### Métricas

- número de adjudicaciones;
- monto acumulado;
- precio mediano;
- fecha de última aparición.

### Agrupaciones

- entidades donde gana;
- regiones donde gana;
- servicios o segmentos adjudicados.

### Tabla de licitaciones ganadas

| Columna | Contenido |
|---|---|
| Contrato | Código completo. |
| Servicio | Descripción y CUBSO. |
| Entidad | Entidad compradora. |
| Región | Departamento. |
| Precio | Monto adjudicado. |
| Fecha | Fecha de publicación. |
| Documento | Acceso al documento histórico, si existe. |

## 14.9 Reglas de cálculo del mercado

- Los desiertos participan en volumen y tasa de riesgo.
- Los desiertos no participan en mediana ni rangos de precio.
- Los campos faltantes se muestran como no informados.
- No se inventan precios, proveedores o ubicaciones.
- Todos los resultados provienen de la base histórica de PostgreSQL.

## 15. Mi empresa y radar

Ruta: `/perfil`.

## 15.1 Objetivo

Administrar la información que BuenaPro utiliza para descubrir oportunidades y evaluar requisitos.

## 15.2 Resumen del perfil

Se muestra:

- razón social;
- RUC;
- porcentaje de completitud;
- número de líneas;
- número de keywords únicas;
- mayor monto de experiencia acreditable;
- cantidad de perfiles del equipo;
- cantidad de oportunidades evaluadas.

La completitud considera identidad, RNP, experiencia económica, equipo, roles contratables, contratos previos, equipamiento y certificaciones.

## 15.3 Identidad del radar

Las keywords generales describen a la empresa en todas sus líneas.

Reglas:

- máximo doce;
- se normalizan a minúsculas;
- se eliminan duplicados;
- se agregan con Enter o coma;
- debe existir al menos una para guardar;
- al guardar se actualizan las señales del radar.

## 15.4 Líneas de negocio

La empresa puede:

- agregar una línea;
- editar una línea;
- definir nombre;
- elegir entre uno y tres segmentos CUBSO;
- registrar hasta treinta keywords;
- ver si un segmento no tiene cobertura actual;
- cancelar cambios;
- guardar cambios.

Guardar una línea actualiza la afinidad de las oportunidades.

## 15.5 Capacidad empresarial

### Identidad

| Campo | Contenido |
|---|---|
| RUC | Número de la empresa. |
| Razón social | Nombre legal. |
| RNP | Categorías o registros separados por coma. |
| CCI | Cuenta interbancaria. |

### Experiencia económica

| Campo | Contenido |
|---|---|
| Monto acreditable en servicios | Valor utilizado para comparar con lo exigido por el TDR. |

### Equipo y recursos

| Campo | Contenido |
|---|---|
| Equipo actual | Roles separados por coma. |
| Roles contratables | Perfiles que la empresa puede incorporar. |
| Equipamiento | Activos disponibles. |
| Certificaciones y seguros | Acreditaciones disponibles. |

### Contratos previos

Cada línea de texto representa un contrato con el formato:

`Objeto | Entidad | Monto | Año`

### Guardado

La acción aparece cuando existen cambios. Al guardar:

- se actualiza el perfil;
- se conservan datos ricos que no fueron editados en este formulario;
- se programa el recálculo de matching;
- se confirma el resultado o se informa el error.

## 15.6 Conexión SEACE

### Sin conexión

Campos:

- usuario SEACE;
- contraseña.

Acción: `Conectar una vez`.

La contraseña se cifra en el servidor y nunca se devuelve al navegador.

### Con conexión

Se muestra:

- estado conectado;
- usuario enmascarado;
- explicación de renovación automática;
- acción `Desconectar` con confirmación.

La conexión permite consultar información privada de SEACE y preparar postulaciones sin volver a iniciar sesión en cada consulta.

## 16. Alertas

Ruta: `/alertas`.

## 16.1 Objetivo

Avisar únicamente cuando una oportunidad alcanza el nivel de afinidad elegido por la empresa.

## 16.2 Preferencias

| Campo | Opciones |
|---|---|
| Alertas activas | Activadas o desactivadas. |
| Nivel mínimo | Solo afinidad alta, o afinidad alta y posible. |
| Máximo diario | 3, 5 o 10 alertas. |

La acción `Guardar alertas` actualiza las preferencias del canal interno.

## 16.3 Actividad reciente

Se muestran hasta cuarenta alertas recientes.

Cada alerta puede contener:

- motivo: nueva oportunidad relevante o afinidad actualizada;
- fecha;
- descripción;
- código;
- entidad;
- score de afinidad;
- tiempo restante hasta el cierre;
- acceso al detalle de la oportunidad.

## 16.4 Reglas de notificación

- Solo se notifican oportunidades que cumplen el umbral configurado.
- Se respeta el máximo diario.
- Se evitan duplicados por usuario, contrato, canal y motivo.
- Un cambio de veredicto puede producir una nueva alerta.
- Si no existen alertas, se informa que aparecerán cuando una oportunidad supere la afinidad configurada.

## 17. Administración técnica interna

Ruta: `/admin`.

Esta pantalla es solo para el equipo técnico de BuenaPro.

Puede contener:

### Estado de lotes

- total esperado;
- procesados;
- fallidos;
- tiempo estimado.

### Jobs de workers

- pendientes;
- reclamados;
- completados;
- fallidos;
- dead letter;
- reintento manual;
- marcado manual como dead.

### Eventos del pipeline

- contrato;
- etapa;
- duración;
- resultado;
- error.

### Extracciones para revisión

- documentos con baja calidad;
- validaciones fallidas;
- necesidad de revisión humana.

### Salud de SEACE

- resultado de pruebas de contrato;
- disponibilidad de endpoints;
- cambios incompatibles.

La administración técnica no debe aparecer como una sección para clientes.

## 18. Documentación técnica de API

Ruta: `/docs`.

Expone la documentación OpenAPI para desarrollo e integración. Su audiencia es el equipo técnico, no el usuario proveedor.

Incluye contratos de endpoints para:

- autenticación;
- oportunidades;
- detalle y documentos;
- perfil y líneas;
- mercado;
- seguimiento;
- borradores;
- alertas;
- integraciones SEACE;
- administración interna.

## 19. Recorridos funcionales completos

## 19.1 Usuario nuevo hasta primera oportunidad

1. Abre `/registro`.
2. Registra nombre, empresa, email y contraseña.
3. El sistema crea usuario, tenant y membresía.
4. Entra a `/onboarding`.
5. Registra razón social y RUC.
6. Proporciona web o descripción de servicios.
7. Revisa keywords y líneas sugeridas.
8. Corrige CUBSO y keywords.
9. Registra experiencia, equipo y recursos.
10. Revisa el resumen.
11. Activa el perfil.
12. Entra a Oportunidades.
13. La lista se ordena usando sus líneas y señales.

## 19.2 Descubrir y guardar una oportunidad

1. Entra a Oportunidades.
2. Aplica búsqueda o filtros.
3. Revisa código, objeto, entidad, experiencia, plazo y cierre.
4. Selecciona una fila para abrir la vista rápida.
5. Guarda la oportunidad.
6. Puede recuperarla luego mediante el filtro Guardadas.

Guardar no crea obligatoriamente una postulación.

## 19.3 Evaluar si conviene postular

1. Abre el detalle.
2. Revisa cierre, entidad y descripción.
3. Ejecuta `Evaluar con mi perfil` si no existe análisis.
4. Revisa veredicto, score y brechas.
5. Revisa requisitos y TDR.
6. Revisa comparables, rango de precios y desiertos.
7. Decide descartar, guardar, seguir o comenzar postulación.

## 19.4 Comenzar una postulación

1. Desde el detalle selecciona `Comenzar postulación`.
2. El sistema crea o reutiliza match y borrador.
3. Sincroniza ítems, RTM y documentos desde SEACE.
4. Navega a `/postulaciones/[matchId]`.
5. El usuario selecciona ítems y registra precios.
6. Registra vigencia y contacto.
7. Responde RTM.
8. Adjunta propuesta y anexos.
9. Asigna responsable.
10. Revisa el avance.
11. Presenta oficialmente por el canal SEACE fuera del envío automatizado de BuenaPro.

## 19.5 Investigar un mercado

1. Entra a Mercado.
2. Elige Mis mercados o Todo el mercado.
3. Busca por servicio, entidad, código o proveedor.
4. Filtra segmento, resultado, departamento, entidad, año o precio.
5. Revisa volumen, desiertos, competidores, entidades y precio.
6. Abre Contratos para revisar resultados individuales.
7. Abre Empresas para identificar ganadores frecuentes.
8. Selecciona una empresa para revisar dónde, qué y por cuánto ganó.

## 19.6 Ajustar el radar

1. Abre Mi empresa y radar.
2. Corrige keywords generales.
3. Agrega o edita líneas.
4. Ajusta segmentos y keywords.
5. Actualiza capacidad empresarial.
6. Guarda.
7. BuenaPro recalcula la afinidad de las oportunidades.

## 19.7 Configurar alertas

1. Abre Alertas desde la campana o el menú de cuenta.
2. Activa o desactiva avisos.
3. Elige el nivel mínimo.
4. Define el máximo diario.
5. Guarda.
6. Revisa el historial y abre una oportunidad notificada.

## 20. Estados globales que debe soportar el producto

Todas las pantallas de datos deben considerar:

- carga inicial;
- carga parcial;
- resultado vacío;
- error recuperable;
- acción en proceso;
- acción exitosa;
- error de validación;
- sesión vencida;
- falta de perfil;
- falta de conexión SEACE;
- datos incompletos provenientes de SEACE;
- documentos no disponibles;
- ausencia de comparables;
- ausencia de precio;
- ausencia de proveedor;
- operación no permitida por estado o fecha.

## 21. Reglas de seguridad y confianza

- Todas las operaciones de empresa están aisladas por tenant.
- Las credenciales SEACE se cifran en servidor.
- La contraseña SEACE nunca vuelve al navegador.
- Los documentos y mensajes se tratan como contenido no confiable.
- La IA no puede enviar postulaciones.
- La IA no puede modificar borradores sin confirmación.
- No se inventan capacidades empresariales.
- No se inventan precios históricos.
- No se usan desiertos para calcular precios.
- El usuario conserva control sobre toda decisión y presentación oficial.

## 22. Reglas funcionales que no deben perderse en un rediseño

1. La navegación principal debe conservar Oportunidades, Mercado y Postulaciones.
2. Perfil y Alertas deben seguir disponibles como funciones secundarias de cuenta.
3. Admin y Docs no deben convertirse en opciones normales del producto.
4. La ruta raíz debe llevar al flujo principal y no duplicar los tres módulos.
5. El histórico de una oportunidad debe aparecer en su detalle, no como columnas adicionales en Oportunidades.
6. Mercado debe conservar Mis mercados y Todo el mercado.
7. Los filtros de Mercado deben afectar Resumen, Contratos y Empresas.
8. Los desiertos deben mostrarse como riesgo, pero excluirse de precios.
9. Guardar una oportunidad y seguir una oportunidad deben continuar siendo acciones diferentes.
10. Comenzar postulación debe crear o reutilizar un borrador.
11. El workspace debe conservar oferta, mercado, RTM, archivos, responsable y avance.
12. El envío oficial a SEACE debe permanecer deshabilitado hasta validar un endpoint oficial.
13. Las propuestas del copiloto deben requerir confirmación humana.
14. La conexión SEACE debe permanecer separada de la identidad y capacidad empresarial.
15. La información faltante debe declararse como ausente, no inferirse.

## 23. Datos de ejemplo para prototipos funcionales

Los siguientes valores pueden utilizarse únicamente como contenido de demostración:

### Empresa

- Razón social: `VEYON SAC`.
- RUC: un RUC ficticio de once dígitos.
- Línea: `Desarrollo de software`.
- Segmentos: `43` y `81`.
- Keywords: `desarrollo de software`, `sistemas informáticos`, `soporte técnico`.

### Oportunidad

- Código: `CM-110-2026-UNAAT`.
- Entidad: `UNIVERSIDAD NACIONAL AUTÓNOMA ALTOANDINA DE TARMA`.
- Objeto: descripción extensa de un servicio.
- Estado: `Vigente`.
- Cierre: una fecha y hora futura.

### Histórico

- Resultado adjudicado con proveedor y precio.
- Resultado desierto sin precio.
- Resultado sin información concluyente.

### Postulación

- Dos ítems, uno seleccionado y uno no seleccionado.
- Tres RTM, dos respondidos y uno pendiente.
- Un archivo PDF adjunto.
- Un responsable asignado.

Los prototipos deben distinguir claramente estos datos ficticios de los datos reales provenientes de SEACE.

## 24. Resultado esperado al utilizar este contexto

Cualquier propuesta basada en este documento debe representar una aplicación operativa, no una página informativa.

Debe ser posible reconocer y recorrer funcionalmente:

1. acceso y registro;
2. onboarding;
3. descubrimiento de oportunidades;
4. detalle y análisis;
5. histórico comparable;
6. seguimiento de postulaciones;
7. preparación de borrador;
8. inteligencia de mercado;
9. perfil y radar;
10. alertas.

La propuesta puede reorganizar la presentación de la información, pero no debe eliminar, inventar ni cambiar las reglas funcionales descritas aquí.
