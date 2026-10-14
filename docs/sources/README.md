# Registro maestro de fuentes

Este archivo es el inventario maestro de plataformas y canales que BuenaPro investigara, documentara e integrara. Incluye fuentes de oportunidades, historico, resultados, taxonomias y canales privados. El registro refleja todas las fuentes conocidas al 20 de septiembre de 2026; toda fuente nueva debe agregarse aqui antes de implementarse.

Las fases ordenan el trabajo, pero no reducen el alcance. Que una fila este `PENDIENTE` significa que ya forma parte del programa, no que haya sido descartada.

## Regla de trabajo

Cada fuente se procesa individualmente:

1. investigar propietario, alcance y permisos
2. probar endpoints o superficie publica de forma read-only
3. capturar ejemplos sanitizados
4. documentar autenticacion, paginacion, IDs, documentos y lifecycle
5. definir el mapeo canonico
6. implementar y probar el adaptador
7. ejecutar y reconciliar el backfill
8. activar polling y observabilidad

Usar siempre [`_TEMPLATE.md`](./_TEMPLATE.md). No registrar secretos, cookies, tokens ni datos personales innecesarios. El plan de arquitectura se encuentra en [`../universal-ingestion-plan.md`](../universal-ingestion-plan.md).

## Estados

```text
PENDIENTE         registrada, aun no investigada formalmente
EN_INVESTIGACION  endpoints y permisos en revision
VERIFICADA        guia y pruebas read-only terminadas
IMPLEMENTADA      adaptador y tests terminados
BACKFILLED        historico permitido reconciliado
PRODUCCION        polling y observabilidad activos
BLOQUEADA         requiere permiso, alianza o acceso del usuario
ARCHIVO           solo historico; no se trata como fuente viva
```

## Fase A — Nucleo publico peruano

| ID | Plataforma o modulo | Funcion | Acceso | Guia prevista | Estado |
| ---: | --- | --- | --- | --- | --- |
| 001 | SEACE/OECE Contratos Menores (`prod6`) | oportunidades, documentos y lifecycle | publico + cuenta para postular | [`001-seace-contratos-menores.md`](./001-seace-contratos-menores.md) | VERIFICADA para bienes; implementacion legacy existente, polling multiobjeto pendiente |
| 002 | OECE Contrataciones Abiertas OCDS/API | procesos, entidades, proveedores, participaciones, adjudicaciones, contratos e historico | API publica | [`002-oece-ocds.md`](./002-oece-ocds.md) | VERIFICADA; inteligencia de compradores/proveedores mapeada, no determina postulacion vigente por si sola |
| 003 | SEACE Buscador Publico (`prod2`) | anuncios futuros, procedimientos, cronogramas, documentos, expresiones, requerimientos y ordenes | publico con sesion JSF/reCAPTCHA | [`003-seace-buscador-publico.md`](./003-seace-buscador-publico.md) | VERIFICADA; bienes/servicios, anuncio futuro y detalle operativo mapeados; adaptador pendiente |
| 004 | SEACE Oportunidades (`prod4/openegocio`) | oportunidades vigentes | publico | [`004-seace-oportunidades.md`](./004-seace-oportunidades.md) | VERIFICADA; API JSON pública, detalle/cronograma/documentos/CUBSO y cruce PROD2/OCDS probados |
| 005 | Plan Anual de Contrataciones | demanda futura y versiones | publico | [`005-seace-pac.md`](./005-seace-pac.md) | VERIFICADA; PAC3/PROD2 vigente, JSF con sesión, procesos/ítems/versiones probados; PROD1 fuera de alcance |
| 006 | Buscador Publico de Contratos | contratos y ejecucion | publico | `006-seace-contratos.md` | PENDIENTE |
| 007 | Datos Abiertos OECE | backfill y analitica | descarga publica | `007-oece-datos-abiertos.md` | PENDIENTE |
| 008 | Peru Compras — acceso CEAM | operaciones de acuerdos marco | registrado | `008-peru-compras-ceam.md` | PENDIENTE |
| 009 | Peru Compras — Consulta de Ordenes | ordenes publicas CEAM e inteligencia de mercado | publico/OCDS | [`009-peru-compras-ordenes.md`](./009-peru-compras-ordenes.md) | VERIFICADA; descarga OCDS publica y documentos mapeados |
| 010 | Peru Compras — Buscador de Catalogos | fichas-producto y catalogos | publico con sesion antiforgery | [`010-peru-compras-catalogos.md`](./010-peru-compras-catalogos.md) | VERIFICADA; catalogo maestro, no fuente de oportunidades |
| 011 | Cotizador OECE | indagaciones y solicitudes de cotizacion | cuenta autorizada | `011-oece-cotizador.md` | PENDIENTE |
| 012 | Proveedores adjudicados OECE | historial competitivo | publico | `012-oece-proveedores-adjudicados.md` | PENDIENTE |
| 013 | Ficha Unica del Proveedor | experiencia, sanciones y perfil | publico | `013-oece-fup.md` | PENDIENTE |
| 014 | CUBSO | taxonomia publica | publico | `014-oece-cubso.md` | PENDIENTE |
| 015 | Listado de Bienes y Servicios Comunes | soporte para subasta inversa | publico | `015-peru-compras-lbsc.md` | PENDIENTE |
| 016 | Proyectos y fichas tecnicas | señales de futura estandarizacion | publico | `016-peru-compras-fichas-tecnicas.md` | PENDIENTE |
| 017 | Cuaderno de Obra/Incidencias | seguimiento de obras | publico | `017-oece-cuaderno-obra.md` | PENDIENTE |
| 018 | Estadisticas del Mercado Estatal/CONOSCE | validacion y analitica | publico | `018-oece-estadisticas.md` | PENDIENTE |

## Fase B — Portales institucionales y contratos menores

| ID | Plataforma o entidad | Funcion | Acceso | Guia prevista | Estado |
| ---: | --- | --- | --- | --- | --- |
| 019 | JNE PLACOBS | oportunidades, TDR integrados y resultados | publico/registro con WAF | [`019-jne-placobs.md`](./019-jne-placobs.md) | VERIFICADA con bloqueo Imperva desde esta red; API identificada |
| 020 | Poder Judicial — Convocatorias 8 UIT | contratos menores | publico/registro | `020-poder-judicial-8uit.md` | PENDIENTE |
| 021 | SUNARP — 8 UIT | oportunidades menores, TDR/EETT y postulacion | publico/registro | [`021-sunarp-8uit.md`](./021-sunarp-8uit.md) | VERIFICADA; HTML/PDF publico, sin CAPTCHA en lectura |
| 022 | Municipalidad de Lima — Lima Compra | compras municipales | publico/registro | `022-lima-compra.md` | PENDIENTE |
| 023 | Provias Nacional | contratos menores | publico | `023-provias-nacional.md` | PENDIENTE |
| 024 | SBN — 8 UIT | contratos menores | publico/registro | `024-sbn-8uit.md` | PENDIENTE |
| 025 | SENAMHI — Contrataciones 8 UIT | contratos menores | publico/registro | `025-senamhi-8uit.md` | PENDIENTE |
| 026 | MINSA — 8 UIT | contratos menores | publico con anti-bot/registro | `026-minsa-8uit.md` | PENDIENTE |
| 027 | Hospital de Ventanilla — 8 UIT | contratos menores | publico/registro | `027-hospital-ventanilla-8uit.md` | PENDIENTE |
| 028 | SANIPES — Requerimientos 8 UIT | contratos menores | publico/registro | `028-sanipes-8uit.md` | PENDIENTE |
| 029 | OTASS — 8 UIT | contratos menores | registrado | `029-otass-8uit.md` | PENDIENTE |
| 030 | CENARES — Contratos Menores | bienes y servicios sanitarios | publico | `030-cenares-menores.md` | PENDIENTE |
| 031 | ENOSA — Requerimientos y Cotizaciones | compras empresariales | publico/registro | `031-enosa-cotizaciones.md` | PENDIENTE |
| 032 | PNSR — Procesos menores | saneamiento rural | publico | `032-pnsr-menores.md` | PENDIENTE |
| 033 | PNSU — Contrataciones | saneamiento urbano | publico/correo | `033-pnsu-contrataciones.md` | PENDIENTE |
| 034 | Provias Descentralizado | contratos menores y resultados | publico/registro | `034-provias-descentralizado.md` | PENDIENTE |
| 035 | DIRESA Madre de Dios — Cotizaciones | solicitudes de cotizacion | publico/correo | `035-diresa-mdd-cotizaciones.md` | PENDIENTE |
| 036 | Gobierno Regional de Apurimac — Cotizaciones | compras regionales | publico/registro | `036-gore-apurimac-cotizaciones.md` | PENDIENTE |
| 037 | Gobierno Regional del Cusco — GORE Cotiza | compras regionales | registrado | `037-gore-cusco-cotiza.md` | PENDIENTE |
| 038 | GEREDU Cusco — Abastecimiento | cotizaciones educativas | publico/correo | `038-geredu-cusco.md` | PENDIENTE |
| 039 | UNIQ — Cotizaciones | compras universitarias | publico/registro | `039-uniq-cotizaciones.md` | PENDIENTE |
| 040 | Universidad Nacional de Arte Diego Quispe Tito | abastecimiento universitario | publico/registro | `040-unadqtc-abastecimiento.md` | PENDIENTE |
| 041 | San Gaban — Cotizaciones vigentes | compras de empresa estatal | publico/registro | `041-san-gaban-cotizaciones.md` | PENDIENTE |
| 042 | EPS Grau — Requerimientos | bienes y servicios | publico | `042-eps-grau-requerimientos.md` | PENDIENTE |
| 043 | Municipalidad Provincial de Huaral | contratos menores | publico con acceso variable | `043-muni-huaral-menores.md` | PENDIENTE |
| 044 | Universidad Nacional de Cañete | bienes y servicios | publico/correo | `044-undc-bienes-servicios.md` | PENDIENTE |
| 045 | Universidad Nacional Ciro Alegria | servicios y cotizaciones | publico/correo | `045-unca-convocatorias.md` | PENDIENTE |
| 046 | Universidad Nacional del Centro — Logistica | convocatorias y archivo | publico | `046-uncp-logistica.md` | PENDIENTE |
| 047 | UE 118 Educacion/PMESUT | bienes y servicios | publico | `047-ue118-proveedores.md` | PENDIENTE |
| 048 | UGEL Santiago de Chuco | invitaciones a cotizar | publico/correo | `048-ugel-santiago-chuco.md` | PENDIENTE |
| 049 | Beneficencia de Puerto Maldonado | publicaciones de cotizacion | publico Gob.pe | `049-beneficencia-puerto-maldonado.md` | PENDIENTE |
| 050 | MININTER — Menores a 8 UIT | archivo institucional | publico Gob.pe | `050-mininter-menores.md` | ARCHIVO/PENDIENTE |
| 051 | Defensoria del Pueblo — Transparencia 8 UIT | historial institucional | publico | `051-defensoria-8uit.md` | ARCHIVO/PENDIENTE |

## Fase C — Regimenes especiales y empresas estatales

| ID | Plataforma o entidad | Funcion | Acceso | Guia prevista | Estado |
| ---: | --- | --- | --- | --- | --- |
| 052 | Petroperu — Adquisiciones y Contrataciones | convocatorias e historico | publico | `052-petroperu-convocatorias.md` | PENDIENTE |
| 053 | Petroperu — Suplos | participacion y ofertas | registrado/invitacion | `053-petroperu-suplos.md` | PENDIENTE |
| 054 | Petroperu — PAAC | compras previstas | publico | `054-petroperu-paac.md` | PENDIENTE |
| 055 | Petroperu Gas Natural Sur | convocatorias propias | publico/correo | `055-petroperu-gas-sur.md` | PENDIENTE |
| 056 | ProInversion — Concursos Publicos | concursos y adjudicaciones | publico | `056-proinversion-concursos.md` | PENDIENTE |
| 057 | ProInversion — Expresiones de Interes | consultorias y proyectos | publico | `057-proinversion-expresiones.md` | PENDIENTE |
| 058 | ProInversion — Concursos por Invitacion | procesos restringidos e historico | invitacion/publicacion | `058-proinversion-invitacion.md` | PENDIENTE |
| 059 | ProInversion — Concursos SSE | supervision especializada | publico | `059-proinversion-sse.md` | PENDIENTE |
| 060 | ProInversion — Obras por Impuestos | financista, ejecutor y supervisor | publico | `060-proinversion-oxi.md` | PENDIENTE |
| 061 | ProInversion — APP y Proyectos en Activos | oportunidades de inversion | publico | `061-proinversion-inversion.md` | PENDIENTE |
| 062 | PERUPETRO — Procesos de Seleccion | lotes de hidrocarburos | publico | `062-perupetro-seleccion.md` | PENDIENTE |
| 063 | Compras a MYPEru | convocatorias de nucleos ejecutores | publico/registro | `063-compras-myperu.md` | PENDIENTE |
| 064 | Wasi Mikuna — Proveedores | compras del programa alimentario | publico/registro | `064-wasi-mikuna.md` | PENDIENTE |
| 065 | Banco de la Nacion — Bases y Subastas | licitaciones y concursos | publico | `065-banco-nacion.md` | PENDIENTE |
| 066 | EsSalud — Contrataciones Directas | procesos y archivos | publico | `066-essalud-contrataciones.md` | PENDIENTE |
| 067 | FONAFE — Directorio y Compras Corporativas | padron de compradores y señales | publico | `067-fonafe.md` | PENDIENTE |
| 068 | SEDAPAL — Financiamiento Externo | procesos BID/BIRF/JICA/KfW | publico | `068-sedapal-financiamiento.md` | PENDIENTE |
| 069 | OSINERGMIN — SICOES/Empresas Supervisoras | concursos de supervision | publico/registro | `069-osinergmin-sicoes.md` | PENDIENTE |

## Fase D — Organismos internacionales y cooperacion

| ID | Plataforma | Funcion | Acceso | Guia prevista | Estado |
| ---: | --- | --- | --- | --- | --- |
| 070 | Banco Mundial — Procurement/API/STEP | avisos, planes y adjudicaciones | API/publico | `070-world-bank-procurement.md` | PENDIENTE |
| 071 | BID — Procurement/Open Data | avisos, planes y adjudicaciones | descarga/publico | `071-idb-procurement.md` | PENDIENTE |
| 072 | UN Global Marketplace | compras del sistema ONU | publico/registro | `072-ungm.md` | PENDIENTE |
| 073 | PNUD Procurement Notices | avisos y adjudicaciones | publico | `073-undp-procurement-notices.md` | PENDIENTE |
| 074 | Quantum Supplier Portal | postulacion ONU asociada | registrado/invitacion | `074-quantum-supplier.md` | PENDIENTE |
| 075 | UN Partner Portal | oportunidades para ONG y academia | registrado | `075-un-partner-portal.md` | PENDIENTE |
| 076 | OEI — Licitaciones Peru | bienes, servicios y consultorias | publico | `076-oei-peru.md` | PENDIENTE |
| 077 | CAF — Convocatorias | consultorias, obras y suministros | publico | `077-caf-convocatorias.md` | PENDIENTE |
| 078 | Union Europea — Funding & Tenders | compras y subvenciones | publico/registro | `078-eu-funding-tenders.md` | PENDIENTE |
| 079 | EEAS — Tenders | compras de delegaciones UE | publico | `079-eeas-tenders.md` | PENDIENTE |
| 080 | AFD-dgMarket | procesos financiados por AFD | publico/registro | `080-afd-dgmarket.md` | PENDIENTE |
| 081 | GIZ — Tenders | compras y consultorias | publico/registro | `081-giz-tenders.md` | PENDIENTE |

## Fase E — Privados publicamente descubribles

| ID | Plataforma | Funcion | Acceso | Guia prevista | Estado |
| ---: | --- | --- | --- | --- | --- |
| 082 | eBIZ B2M | RFI, RFQ, RFP y red empresarial | registrado/invitacion | `082-ebiz-b2m.md` | PENDIENTE |
| 083 | Subastas.pe | subastas empresariales | registrado | `083-subastas-pe.md` | PENDIENTE |
| 084 | SAP Business Network Discovery | publicaciones publicas y privadas | registrado | `084-sap-business-network-discovery.md` | PENDIENTE |
| 085 | ConnectAmericas | anuncios de compra regionales | registrado/verificado | `085-connectamericas.md` | PENDIENTE |
| 086 | Suplos — Licitaciones | compradores publicos y privados | suscripcion/registro | `086-suplos-licitaciones.md` | PENDIENTE |
| 087 | Licify | construccion y cadena de proveedores | registrado | `087-licify.md` | PENDIENTE |
| 088 | Procu | licitaciones privadas peruanas | registrado | `088-procu.md` | PENDIENTE |
| 089 | Mercado Eletronico | cotizaciones y marketplace B2B | registrado/API comercial | `089-mercado-eletronico.md` | PENDIENTE |
| 090 | iConstruye Marketplace | construccion y cotizaciones | plan/registro | `090-iconstruye.md` | PENDIENTE |
| 091 | Contacto Latam | licitaciones privadas regionales | solicitud/registro | `091-contacto-latam.md` | PENDIENTE |
| 092 | Sistad | RFQ industrial y marketplace | registro/plan | `092-sistad.md` | PENDIENTE |
| 093 | Achilles Community/Regic | homologacion y visibilidad minera | membresia | `093-achilles.md` | PENDIENTE |
| 094 | SICEP | proveedores y licitaciones mineras | membresia | `094-sicep.md` | PENDIENTE |

## Fase F — Privados autenticados, invitaciones y captura autorizada

| ID | Plataforma o canal | Funcion | Acceso | Guia prevista | Estado |
| ---: | --- | --- | --- | --- | --- |
| 095 | Buzon privado BuenaPro | invitaciones y adjuntos por empresa | tenant privado | `095-buzon-privado.md` | PENDIENTE |
| 096 | Gmail autorizado | ingestion de invitaciones | OAuth tenant | `096-gmail-oportunidades.md` | PENDIENTE |
| 097 | Outlook/Microsoft 365 autorizado | ingestion de invitaciones | OAuth tenant | `097-outlook-oportunidades.md` | PENDIENTE |
| 098 | Coupa Supplier Portal | eventos publicos o invitados | cuenta comprador | `098-coupa-supplier.md` | BLOQUEADA hasta cuenta/autorizacion |
| 099 | Oracle Supplier Portal | negociaciones por comprador | cuenta comprador | `099-oracle-supplier.md` | BLOQUEADA hasta cuenta/autorizacion |
| 100 | JAGGAER Supplier Network | eventos y notificaciones | cuenta comprador | `100-jaggaer-supplier.md` | BLOQUEADA hasta cuenta/autorizacion |
| 101 | Ivalua Supplier Portal | RFx y contratos por comprador | cuenta comprador | `101-ivalua-supplier.md` | BLOQUEADA hasta cuenta/autorizacion |
| 102 | GEP SMART Supplier Portal | RFx y eventos empresariales | cuenta comprador | `102-gep-smart.md` | BLOQUEADA hasta cuenta/autorizacion |
| 103 | SAP Ariba eventos privados | sourcing por invitacion | cuenta comprador | `103-sap-ariba-private.md` | BLOQUEADA hasta cuenta/autorizacion |
| 104 | Portales particulares de compradores | oportunidades de mineras, bancos e industria | variable | `104-buyer-portals-registry.md` | PENDIENTE |
| 105 | Carga manual | correos, links y documentos | tenant privado | `105-manual-upload.md` | PENDIENTE |
| 106 | Extension de navegador autorizada | guardar oportunidad desde sesion del usuario | tenant privado | `106-browser-extension.md` | PENDIENTE |

## Fase G — Inventario propio de BuenaPro

| ID | Componente | Funcion | Acceso | Guia prevista | Estado |
| ---: | --- | --- | --- | --- | --- |
| 107 | Portal de compradores BuenaPro | publicar RFI, RFQ, RFP y subastas | comprador registrado | `107-buyer-portal.md` | PENDIENTE |
| 108 | API/webhook/SFTP/CSV de compradores | ingestion directa de socios | acuerdo/API | `108-buyer-integrations.md` | PENDIENTE |
| 109 | Red de homologacion BuenaPro | descubrimiento y validacion de proveedores | comprador/proveedor | `109-supplier-network.md` | PENDIENTE |

## Cobertura del registro

- Las filas 001-094 representan plataformas o modulos externos identificados.
- Las filas 095-106 representan canales necesarios para oportunidades privadas no publicas.
- Las filas 107-109 representan inventario generado directamente dentro de BuenaPro.
- FMP y MIDIS no se registran aun como endpoints operativos: existe evidencia de sistemas, pero falta una URL publica vigente y verificable. Cuando aparezca, se agregaran como nuevas filas.
- United Nations Development Business se considera fuente cerrada desde el 31 de marzo de 2025; solo se agregara si se decide hacer archivo historico.

La guia individual es la fuente de verdad de cada verificacion. Este README es la fuente de verdad del inventario y del orden.
