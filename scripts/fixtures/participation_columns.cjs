// Synthetic UI fixtures only. Imported in a QA process, never by the app.
(() => {
// Isolated UI QA fixture. Every PostgreSQL query is replaced; no DB connection, write or provider call.
const pg = require('../../node_modules/pg');
const profileId = '00000000-0000-0000-0000-000000000001';
const sha = 'a'.repeat(64), joint = 'b'.repeat(64), code = '00000000-0000-0000-0000-000000000010';
const descriptions = [
 'CONTRATACIÓN DEL SERVICIO DE IMPLEMENTACIÓN, INTEGRACIÓN Y SOPORTE DE UNA PLATAFORMA DE GESTIÓN DOCUMENTAL Y SERVICIOS DIGITALES',
 'SERVICIO DE PROGRAMACIÓN DE CONTROLADORES PLC Y PUESTA EN MARCHA DEL SISTEMA SCADA DE LA PLANTA DE TRATAMIENTO',
 'SERVICIO DE SOPORTE Y MANTENIMIENTO DE LICENCIA DE SOFTWARE PARA APLICACIONES',
 'CONTRATACIÓN DEL SERVICIO DE RENOVACIÓN DE LICENCIAS DE SOFTWARE DE BACKUP PARA LA PLATAFORMA DE CORREO ELECTRÓNICO Y HERRAMIENTAS COLABORATIVAS EN LA NUBE',
 'SERVICIO DE SUSCRIPCIÓN DE LICENCIAS DE SOFTWARE DE DISEÑO GRÁFICO ADOBE CREATIVE CLOUD',
 'SERVICIO DE SUSCRIPCIÓN DE SOFTWARE DE GOBIERNO, RIESGO Y CUMPLIMIENTO',
 'LICENCIA DE SOFTWARE DE ADMINISTRACIÓN DE CORREOS ELECTRÓNICOS PARA CLIENTE',
 'ADQUISICIÓN DE EQUIPAMIENTO INFORMÁTICO Y SOFTWARE PARA IMPRESIÓN 3D PARA EL PROYECTO DE GESTIÓN DE RIESGO DE DESASTRES',
 'SERVICIO DE SOPORTE TÉCNICO, ACTUALIZACIÓN Y MANTENIMIENTO DEL SOFTWARE MATLAB',
 'CONTRATACIÓN DEL SERVICIO DE BIBLIOTECA VIRTUAL EN MODALIDAD SOFTWARE AS A SERVICE PARA LOS INSTITUTOS DE EDUCACIÓN SUPERIOR'
];
const schedule = [{ stage: 'Registro de participantes', starts_at_text: '05/10/2026 10:00', ends_at_text: '15/10/2026 18:00' },
 { stage: 'Formulación de consultas y observaciones', starts_at_text: '05/10/2026', ends_at_text: '08/10/2026' },
 { stage: 'Absolución de consultas y observaciones', starts_at_text: '09/10/2026', ends_at_text: '10/10/2026' },
 { stage: 'Presentación de ofertas', starts_at_text: '16/10/2026 08:00', ends_at_text: '16/10/2026 18:00' }];
const extraction = { id:'7',codigo_alfresco:code,sha256_original:sha,model:'qa-fixture',prompt_version:'qa',schema_version:'qa',quality:'auto',requires_human_review:false,facet_count:3,created_at:'2026-10-05T15:00:00Z',summary_json:{descripcion_corta:'La entidad solicita implementar e integrar una plataforma de gestión documental, migrar información y brindar soporte a los usuarios.',plazo_ejecucion_dias:90,tipo_pago:'por_entregable'},raw_extraction_json:{summary:{description:'Implementación de una plataforma de gestión documental con integración y soporte. La participación conjunta requiere comprobar el mecanismo permitido por las bases.'}} };
const facets = [{id:'1',facet:'economic_experience',label:'Acreditar experiencia compatible en implementación e integración de software',required:true,details_json:{},evidence_json:[{quote:'Experiencia en servicios de implementación de sistemas de información.',page:4}]},
 {id:'2',facet:'professional_experience',label:'Contar con especialista en integración de sistemas',required:true,details_json:{},evidence_json:[{quote:'Especialista responsable con experiencia en integración de sistemas.',page:5}]},
 {id:'3',facet:'certification',label:'Certificación Scrum Master o Product Owner',required:false,details_json:{},evidence_json:[{quote:'Contar con certificación Scrum Master o Product Owner (Deseable).',page:5}]}];
const breakdown = {resumen:'El alcance corresponde a software e integración. El convenio aporta líneas complementarias, pero falta acreditar la experiencia y verificar la modalidad de participación.',acciones_recomendadas:['Revisar el mecanismo de participación y atribuir la experiencia a cada miembro.'],meta:{source_key:'seace_prod2',id_convocatoria:'1001',extraction_id:'7',document_sha256:sha,profile_hash:joint,member_names:{[profileId]:'VEYON SAC','00000000-0000-0000-0000-000000000002':'DINAUT'}},requisitos:facets.map((facet,i)=>({requirement_id:`r${String(i+1).padStart(4,'0')}`,requisito:facet.label,estado:'requiere_revision',gap:'Falta verificar evidencia compatible y el mecanismo de participación conjunta.',accion:'Revisar las bases y documentar respaldo por miembro.',member_gaps:[{profile_id:profileId,estado:'requiere_revision',gap:'La orientación comercial no acredita la experiencia exigida.'},{profile_id:'00000000-0000-0000-0000-000000000002',estado:'requiere_revision',gap:'El alcance declarado requiere respaldo documental compatible.'}]}))};
function row(i) {return {id_convocatoria:String(1001+i),id_procedimiento:String(2001+i),opportunity_id:`00000000-0000-0000-0000-${String(101+i).padStart(12,'0')}`,source_key:'seace_prod2',nomenclatura:`CP-ABR-${84+i}-2026-ENTIDAD-1`,title:descriptions[i],description:descriptions[i],object_type:i===7?'good':'service',buyer_name:i%2?'ORGANISMO NACIONAL DE SERVICIOS PÚBLICOS':'UNIVERSIDAD NACIONAL DE INVESTIGACIÓN Y TECNOLOGÍA',published_at:'2026-10-05T15:00:00Z',reference_amount:'180000',currency:'PEN',last_seen_at:'2026-10-05T15:00:00Z',source_url:'https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml?faces-redirect=true',detail_fetched_at:'2026-10-05T15:00:00Z',schedule_fetched_at:'2026-10-05T15:00:00Z',registration_closes_at:'2026-10-15T23:00:00Z',proposals_start_at:'2026-10-16T13:00:00Z',proposals_closes_at:'2026-10-16T23:00:00Z',procedure_type:'Procedimiento de selección',schedule_json:schedule,item_states:['Convocado'],document_inventory_total:2,document_inventory_complete:true,documents_count:2,jev_result:{affinity_index:91-i*3,affinity_confidence:.9,domain:i===1?'industrial_electrica':'informatica',information:'claro',needs_detail:false},match_score:i===0?29:null,match_verdict:i===0?'gris':null,match_id:i===0?'1':null,match_profile_id:profileId,breakdown_json:i===0?breakdown:null,missing_actions_json:[],matched_at:'2026-10-05T16:00:00Z',match_updated_at:'2026-10-05T16:00:00Z',evaluated_profile_hash:joint};}
pg.Pool.prototype.query = async function(query, values=[]) {
 const sql = typeof query==='string'?query:query.text;
 let rows=[];
 if (sql.includes('SELECT id::text, razon_social FROM company_profiles')) rows=values[1]&&values[1]!==profileId?[]:[{id:profileId,razon_social:'VEYON SAC'}];
 else if (sql.includes('count(*)::int AS total')&&sql.includes('prod2_discoveries')) rows=[{total:values.some(v=>String(v).includes('no-results'))?0:10}];
 else if (sql.includes('prod2_discoveries p JOIN prod2_details')) rows=sql.includes('WHERE p.id_convocatoria = $2')?[row(Number(values[1])-1001)].filter(r=>r.description):values.some(v=>String(v).includes('no-results'))?[]:descriptions.map((_,i)=>row(i));
 else if (sql.includes('FROM prod2_documents pd')) rows=[{codigo_alfresco:code,name:'Bases administrativas y términos de referencia.pdf',document_type:'Bases integradas',stage_name:'Convocatoria',published_at:'2026-10-05T15:00:00Z',published_at_text:'05/10/2026 10:00',extension:'pdf',sha256_original:sha,bytes:'483920',storage_key:`prod2/${values[0]}/${code}/${sha}.pdf`}];
 else if (sql.includes('FROM prod2_document_extractions')) rows=[{...extraction,requires_human_review:values[0]==='1002'}];
 else if (sql.includes('FROM prod2_requirement_facets')) rows=facets;
 return {rows,rowCount:rows.length,fields:[],command:'SELECT'};
};

})();
(() => {
// Task-owned isolated UI fixtures. No real DB, writes, SEACE or model calls.
const pg = require('../../node_modules/pg');
const original = pg.Pool.prototype.query;
const profile = '00000000-0000-0000-0000-000000000001';
const clause = (status, text, conditions = []) => ({ status, clause: text, page: 12, conditions });
function reading(index, source = 'prod2') {
  if (index === 4) return null;
  const states = [
    { subcontratacion: clause('prohibited', 'El contratista no podrá subcontratar las prestaciones objeto del contrato.'), consorcio: clause('conditional', 'Se permite participar en consorcio con un máximo de dos integrantes.', ['Máximo dos integrantes.', 'Precisar las obligaciones de cada integrante.']) },
    { subcontratacion: clause('permitted', 'Se permite subcontratar actividades accesorias previa autorización de la entidad.', ['Autorización previa de la entidad.', 'Solo actividades accesorias; el contratista conserva la responsabilidad.']), consorcio: clause('prohibited', 'No se permite la participación en consorcio.') },
    { subcontratacion: clause('permitted', 'La entidad permite la subcontratación del servicio descrito.'), consorcio: clause('permitted', 'Se admite la participación en consorcio.') },
    { subcontratacion: { status: 'not_identified', clause: null, page: null, conditions: [] }, consorcio: { status: 'not_identified', clause: null, page: null, conditions: [] } }
  ];
  return { id: String(70 + index), quality: 'auto', requires_human_review: index === 0, participation: states[index % 4], page_scope: source === 'prod2' || source === 'prod4' && index === 0 ? 'combined' : 'document' };
}
pg.Pool.prototype.connect = async () => { throw new Error('DB connections forbidden in isolated QA'); };
pg.Pool.prototype.query = async function(query, values = []) {
  const sql = typeof query === 'string' ? query : query.text;
  if (/\b(INSERT|UPDATE|DELETE|CALL)\b/i.test(sql)) throw new Error('Writes forbidden in isolated QA');
  if (values.some(v => typeof v === 'string' && v.includes('__qa_empty__'))) return { rows: sql.includes('count(*)::int AS total') ? [{total:0}] : [], rowCount:0 };
  let result = await original.call(this, query, values);
  if (sql.includes('FROM prod2_discoveries')) result.rows = result.rows.map((r, i) => ({ ...r, participation_extraction: reading(Number(r.id_convocatoria) - 1001) }));
  if (sql.includes('FROM seace_contracts c')) {
    const offset = sql.includes('c.id_contrato = $1') ? Number(values[0])-8001 : 0;
    result.rows = Array.from({length: sql.includes('c.id_contrato = $1') ? 1 : 5}, (_, i) => ({
      id_contrato: 8001+i+offset, opportunity_id:'qa', codigo:`CM-${203+i+offset}-2026`, descripcion:'CONTRATACIÓN DE SERVICIO DE IMPLEMENTACIÓN, INTEGRACIÓN Y SOPORTE DE UNA PLATAFORMA INFORMÁTICA PARA LOS SERVICIOS DIGITALES',
      entidad_nombre:'UNIVERSIDAD NACIONAL DE INVESTIGACIÓN Y TECNOLOGÍA', departamento:'LIMA', provincia:'LIMA', objeto_codigo:2, objeto_nombre:'Servicio', estado_codigo:2,
      cotizar:true, pipeline_state:'extracted', fec_ini_cotizacion:'2026-10-12T13:00:00Z',fec_fin_cotizacion:'2026-10-16T23:00:00Z', summary_json:{descripcion_corta:'Servicio de soporte e integración de sistemas.',plazo_ejecucion_dias:90},
      participation_extraction:reading(i+offset, 'prod6'), total_count:5, fit_points:25, raw_detail_json:{}, cronograma:{etapas:[]}, econ_exigido:null, is_saved:false, updated_at:new Date().toISOString(),
    }));
  }
  if (sql.includes('FROM prod4_processes p')) {
    const detailIndex = sql.match(/p\.id_procedimiento = \$(\d+)/);
    const offset = detailIndex ? Number(values[Number(detailIndex[1])-1])-1250001 : 0;
    result.rows = sql.includes('count(*)::int AS total') ? [{ total:5 }] : Array.from({length: sql.includes('p.id_procedimiento = $1') ? 1 : 5}, (_, i) => ({
      id_procedimiento:String(1250001+i+offset), opportunity_id:'qa', nomenclatura:`CP-ABR-${84+i+offset}-2026-ENTIDAD-1`,title:'CONTRATACIÓN DEL SERVICIO DE INTEGRACIÓN DE PLATAFORMAS INFORMÁTICAS Y SOPORTE ESPECIALIZADO',description:'Servicio de integración y soporte',
      object_type:'service', procedure_type:'Procedimiento de selección', buyer_name:'UNIVERSIDAD NACIONAL DE INVESTIGACIÓN Y TECNOLOGÍA',region:'LIMA',documents_count:0,items_count:0,technology_segments:['81'], keyword_hits:[],fit_level:3,
      registration_closes_at:'2026-10-15T23:00:00Z',proposals_closes_at:'2026-10-16T23:00:00Z',source_window_status:'current', official_schedule_raw:[],detail_fetched_at:new Date().toISOString(),source_url:'https://prod4.seace.gob.pe/openegocio/#/ficha/idProceso/1250001', participation_extraction:reading(i+offset,'prod4'),
    }));
  }
  if (sql.includes('FROM company_profiles') && !sql.includes('SELECT id::text, razon_social') && !sql.includes('FROM prod4_processes') && !sql.includes('FROM seace_contracts') && !sql.includes('FROM prod2_discoveries')) result.rows = [{id:profile,razon_social:'Empresa de prueba',tenant_id:'tenant-qa',ruc:'',economic_areas_json:[],profile_json:{},created_at:'2026-10-01T00:00:00Z'}];
  return {...result, rowCount:result.rows.length};
};
const fetchBeforeQA = globalThis.fetch;
globalThis.fetch = (url, ...args) => {
  const host = new URL(typeof url === 'string' ? url : url.url).hostname;
  if (!['localhost','127.0.0.1'].includes(host)) throw new Error('External requests forbidden in isolated UI QA');
  return fetchBeforeQA(url,...args);
};

})();
