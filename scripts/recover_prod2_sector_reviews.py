"""Explicit follow-up for nine retained DIN-sector document reviews.

Original run/manifests/outcomes/payments remain immutable. Eight retained
corpora reuse exact paid vectors; only the never-indexed ninth source may buy
embeddings. No source HTTP, discovery, Redis, full-PDF generation or Luna.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,os,signal,subprocess,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workers/seace/src'));sys.path.insert(0,str(ROOT/'scripts'))
import psycopg
from psycopg.rows import dict_row
from buenapro_worker.prod2.runtime import production_settings
from buenapro_worker.prod2.batch_runner import private_write,read_private
from buenapro_worker.prod2.repository import hash_request
from buenapro_worker.prod2.pdf_preparation import _result,prepare_selected_document_set
from buenapro_worker.prod2.embedding_runtime import prepare_retrieved_input,config_hash
from buenapro_worker.prod2.input_policy import prepare_input
from buenapro_worker.prod2.handoff_guards import HandoffGuards
from buenapro_worker.prod2.selected_control import RunLimits
from buenapro_worker.prod2.selected_payments import audit_summary
from buenapro_worker.prod2.selected_read import paid_selected_read
from buenapro_worker.prod2.llm1_completion import persist_completed_llm1
from buenapro_worker.prod2.analysis import extraction_request_hash
from buenapro_worker.prod2.model_routing import resolve_llm1_model
from buenapro_worker.prod2.document_selection import JevDocumentSelector,CACHE_PREFIX,document_selection_request_hash
from buenapro_worker.normalization.facets import canonical_hash
import run_prod2_selected as selected

VERSION='prod2_sector_review_completion_v1'
IDS=(1047330,1048156,1048369,1048448,1048462,1048805,1049089,1049583,1050007)
SELECTOR_ID=1048805
ORIGINAL=ROOT/'tmp/ia-industrial-execution-20261020/runs/prod2-din-ia-industrial-20261020'
STOP=threading.Event()


class OwnedReviewGuard(HandoffGuards):
 def __init__(self,*args,owner,**kwargs):
  self.owner=owner
  super().__init__(*args,**kwargs)
 def before_request(self):
  super().before_request()
  if self.owner.closed or self.owner.broken:raise ValueError('Production coordinator connection was lost')
  held=self.owner.execute("""SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND granted
   AND pid=pg_backend_pid() AND classid::bigint=(hashtext('prod2_accepted_executor')::bigint & 4294967295)
   AND objid=0 AND objsubid=2) AS held""").fetchone()['held']
  if held is not True:raise ValueError('Production coordinator no longer owns its lock')
 def embedding_client(self,client):
  wrapped=super().embedding_client(client);guard=self
  class Checked:
   @property
   def model(self):return wrapped.model
   @property
   def dimensions(self):return wrapped.dimensions
   @property
   def price_per_million(self):return wrapped.price_per_million
   def embed(self,*args,**kwargs):
    guard.before_request();return wrapped.embed(*args,**kwargs)
   def close(self):return wrapped.close()
  return Checked()


def sha(p):
 p=Path(p)
 if p.is_symlink() or not p.is_file() or any(x.is_symlink() for x in p.parents):raise ValueError('Literal evidence file required')
 return hashlib.sha256(p.read_bytes()).hexdigest()


def once(path,value):
 path=Path(path)
 if path.exists():
  if path.is_symlink() or read_private(path)!=value:raise ValueError('Frozen follow-up evidence changed')
 else:private_write(path,value)


def checked_records(original):
 original=Path(original).absolute()
 if original!=ORIGINAL or original.is_symlink():raise ValueError('Exact original sector run required')
 report_path=sorted(original.glob('report-*.private.json'))[-1]
 report=read_private(report_path)
 records={r['convocation_id']:r for r in report['records']}
 if len(records)!=157:raise ValueError('Exact original157 partition required')
 reviews={k:r for k,r in records.items() if r['status'] in {'needs_review','selection_needs_review'}}
 if set(reviews)!=set(IDS):raise ValueError('Only the exact nine document reviews are authorized')
 for cid,r in reviews.items():
  if read_private(original/str(cid)/'outcome.private.json')!=r:raise ValueError('Original terminal outcome changed')
  if r.get('accepted') is not True or r.get('confirmed_open') is not True:raise ValueError('Accepted confirmed-open review required')
  if cid!=SELECTOR_ID and r.get('document_preparation_review',{}).get('reason')!='retrieval_context_needs_expansion_or_review':raise ValueError('Unsupported retained review')
  if r.get('status')==('selection_needs_review' if cid==SELECTOR_ID else 'needs_review'):continue
  raise ValueError('Unexpected retained review status')
 costs=audit_summary(original)
 if costs['llm1_calls'] or costs['usage_unknown_attempts']:raise ValueError('Prior paid reading or uncertainty is preserved')
 return records,report_path


def validate_lifecycle(record,now):
 observed=datetime.fromisoformat(record['temporal_observed_at']);closes=datetime.fromisoformat(record['closes_at'])
 if (record.get('confirmed_open') is not True or record.get('requires_fresh_lifecycle') is not False
     or record.get('temporal_state') not in {'registration_open','submission_open'}
     or observed.tzinfo is None or closes.tzinfo is None
     or not -60<=(now-observed).total_seconds()<=21600 or closes<=now):
  raise ValueError('Recent genuine open observation and future deadline required; no source refresh is implied')


def frozen_plan(folder):
 records,report=checked_records(ORIGINAL);old=read_private(ORIGINAL/'manifest.private.json')
 config=deepcopy(old['embedding_reading']['config']);config['retrieval']['context_maximum_tokens']=350000
 config['retrieval']['context_expansion_policy']='prod2_explicit_expanded_context_v1'
 # Same seeds, literals, complete sections/tables and OCR policy: only the
 # explicit finite context allowance changes. Nothing is truncated.
 facts=[]
 for cid in IDS:
  source=ORIGINAL/str(cid);r=records[cid]
  preparation=list(source.glob('preparation-cache/selected-analysis-*/manifest.private.json'))
  if cid!=SELECTOR_ID and len(preparation)!=1:raise ValueError('Exact single retained preparation required')
  paths=[source/'outcome.private.json',source/'selection.private.json',source/'document-previews.private.json']
  paths+=preparation
  paths+=sorted(source.glob('*.download.private.json'))
  for receipt in source.glob('*.download.private.json'):
   info=read_private(receipt)
   originals=[x for x in source.glob(info['document_uuid']+'.*') if x.suffix.lower() in {'.pdf','.rar','.doc','.docx','.zip'}]
   if len(originals)!=1 or sha(originals[0])!=info['sha256']:raise ValueError('Original SHA mismatch')
   paths+=originals
  facts.append({'convocation_id':cid,'original_status':r['status'],'original_outcome_sha256':sha(source/'outcome.private.json'),
    'preparation':str(preparation[0]) if preparation else None,
    'evidence':[{'path':str(p),'sha256':sha(p)} for p in sorted(set(paths))]})
 return {'version':VERSION,'original_folder':str(ORIGINAL),'folder':str(folder),
  'original_manifest_sha256':sha(ORIGINAL/'manifest.private.json'),
  'original_report':{'path':str(report),'sha256':sha(report)},'ids':list(IDS),'cases':facts,
  'embedding_config':config,'embedding_config_sha256':config_hash(config),
  'original_config_sha256':old['embedding_reading']['config_sha256'],
  'selector_review':{'convocation_id':SELECTOR_ID,'high_threshold':70,'low_threshold':30,'partial_ocr_psm':'1',
   'reason':'Manual local PDF review confirms official CP-ABR-6-2026-DEC-EPSBARRANCA-1 bases for electromechanical pumping maintenance; reproject the same paid probabilities, no new selector call'},
  'model_workers':2,'max_llm1_calls':9,'max_new_embedding_calls':10,
  'new_embedding_allowed_ids':[SELECTOR_ID],'no_source_http':True,'no_redis':True,
  'original_inputs_unchanged':True,'new_only':True,'no_full_pdf_or_luna':True}


def verify_plan(folder):
 plan=read_private(folder/'manifest.private.json')
 if frozen_plan(folder)!=plan:raise ValueError('Follow-up manifest or original evidence changed')
 return plan


def prepare_case(plan,cid):
 source=ORIGINAL/str(cid);case=Path(plan['folder'])/str(cid);case.mkdir(parents=True,exist_ok=True,mode=0o700)
 row=next(c for c in plan['cases'] if c['convocation_id']==cid)
 if row['preparation']:
  p=Path(row['preparation']);manifest=read_private(p)
  prepared=_result(p.parent,manifest,manifest['max_analysis_bytes'])
  prepared['embedding_ocr_cache_sources']=[str(p) for p in source.glob('embedding-corpus-*') if p.is_dir()]
  return prepared
 # The selector's original probability receipt is retained unchanged; threshold
 # reprojection is explicit in this follow-up, after actual local PDF review.
 record=read_private(source/'outcome.private.json');old=read_private(ORIGINAL/'manifest.private.json')
 receipt=read_private(source/'selection.private.json');previews=read_private(source/'document-previews.private.json')
 parents=deepcopy(record['documents']);candidates=[];files={}
 for parent in parents:
  candidate={**parent,'candidate_id':'loose:'+parent['document_uuid'],'source_kind':'loose'}
  preview=previews[candidate['candidate_id']]
  candidate.update(preview_text=preview['preview_text'],preview_content_hash=preview['content_hash'])
  candidates.append(candidate)
  original_pdf=source/(parent['document_uuid']+'.pdf');owned_pdf=case/original_pdf.name
  if owned_pdf.exists():
   if sha(owned_pdf)!=parent['original_sha256']:raise ValueError('Owned source copy changed')
  else:
   import shutil
   shutil.copyfile(original_pdf,owned_pdf);owned_pdf.chmod(0o600)
   if sha(owned_pdf)!=parent['original_sha256']:raise ValueError('Owned source copy SHA mismatch')
  files[candidate['candidate_id']]=owned_pdf
 class NoNetwork:
  def post(self,*args,**kwargs):raise ValueError('No new selector call is authorized')
 selector=JevDocumentSelector('offline-reuse-only',policy_version=receipt['policy_version'],client=NoNetwork())
 if document_selection_request_hash(record['description'],candidates,policy_version=receipt['policy_version'])!=receipt['request_hash']:raise ValueError('Exact cached selector input required')
 result=selector.select(record['description'],candidates,high_threshold=70,low_threshold=30,
    cache={CACHE_PREFIX+receipt['request_hash']:receipt})
 if not result.get('cache_hit') or result['pending_ids'] or len(result['selected_ids'])!=1:raise ValueError('Explicit reviewed threshold did not select the exact source')
 once(case/'selection-reprojected.private.json',result)
 context={'official_inventory':parents,'selector_request_hash':receipt['request_hash'],
   'selector_receipt_hash':canonical_hash(receipt['raw_response']),'candidates':candidates,
   'decisions':result['decisions'],'thresholds':result['thresholds'],
   'ficha_description':record['description'],'archive_inventory_policy':old['archive_inventory_policy']}
 for key in ('llm1_scope_policy','llm1_commercial_taxonomy','llm1_input_policy','llm1_visual_review_policy','llm1_retrieved_transport_policy','llm1_source_coverage_policy'):
  if key in old:context[key]=old[key]
 return prepare_selected_document_set(case,candidates,files,context,cache_folder=case/'preparation-cache',max_source_bytes=128*1024**2)


def pg_preflight(conn,plan,cid,record,request_hash=None,selection_context=None):
 validate_lifecycle(record,datetime.now(timezone.utc))
 source=conn.execute('SELECT d.detail_hash,d.proposals_closes_at,p.id_procedimiento,p.opportunity_id::text FROM prod2_details d JOIN prod2_discoveries p USING(id_convocatoria) WHERE id_convocatoria=%s',(cid,)).fetchone()
 if (not source or source['detail_hash']!=record['detail_hash'] or not source['proposals_closes_at']
     or source['proposals_closes_at']!=datetime.fromisoformat(record['closes_at'])):raise ValueError('PG source/deadline differs from the reviewed source')
 current=conn.execute('SELECT id FROM prod2_document_extractions WHERE id_convocatoria=%s AND is_current',(cid,)).fetchall()
 if current:raise ValueError('An existing reading is preserved; no new payment')
 prior=conn.execute("SELECT id,status,request_hash,claim_token::text FROM prod2_evaluations WHERE id_convocatoria=%s AND kind='gemini_extraction'",(cid,)).fetchall()
 claim_path=Path(plan['folder'])/str(cid)/('gemini_extraction-'+str(request_hash)+'.claim.private.json')
 owned=read_private(claim_path) if request_hash and claim_path.exists() else {}
 for entry in prior:
  if not (request_hash and entry['request_hash']==request_hash and entry['id']==owned.get('id')
      and entry['claim_token']==str(owned.get('claim_token')) and entry['status'] in {'running','done'}):
   raise ValueError('Historical paid attempt preserved; no repayment')
 if selection_context is not None:
  from buenapro_worker.prod2.llm1_completion import _current_selection_receipt
  with conn.cursor() as cursor:_current_selection_receipt(cursor,selection_context,record['description'])


def read_case(settings,plan,cid,limits,guard):
 case=Path(plan['folder'])/str(cid);out=case/'outcome.private.json'
 if out.exists():return read_private(out) # No automatic retry of any terminal outcome.
 record=read_private(ORIGINAL/str(cid)/'outcome.private.json');prepared=prepare_case(plan,cid)
 with psycopg.connect(settings.database_url,autocommit=True,row_factory=dict_row) as conn:
  pg_preflight(conn,plan,cid,record,selection_context=prepared['selection_context']);guard.before_request()
  old=read_private(ORIGINAL/'manifest.private.json')
  case_limits=limits
  if cid!=SELECTOR_ID:
   case_limits=RunLimits(max_accepted=1,max_candidates=1,max_jev_calls=0,max_llm1_calls=0,max_embedding_calls=0,model_workers=2)
   case_limits.model_slots=limits.model_slots
  case_limits.handoff_guard=guard
  case_config=deepcopy(plan['embedding_config'])
  if cid==SELECTOR_ID:case_config['corpus']['partial_ocr_psm']=plan['selector_review']['partial_ocr_psm']
  prepared=prepare_retrieved_input(conn,settings,prepared,case,case_limits,case_config,convocation_id=cid,stop_requested=STOP.is_set,memoize_expansion=True)
  prepared,local=prepare_input(prepared,case)
  model,routing=resolve_llm1_model(old,prepared['page_count'],settings.gemini_model)
  active=settings.model_copy(update={'gemini_model':model})
  pdf=prepared['pdf_path'].read_bytes();first=prepared['document_set'][0]
  # document_set contains the verified selected source, including archive origin.
  anchor=first['document_uuid'];anchor_sha=first.get('archive_sha256',first['original_sha256'])
  context={k:prepared[k] for k in ('document_scope','document_set','document_set_hash','document_set_identity','selection_context','llm1_input')}
  request=extraction_request_hash(active,pdf,object_type=record['object_type'],original_sha256=anchor_sha,
    manifest=prepared['source_document_manifest'],document_context=context)
  def before(issue):
   def checked():
    guard.before_request();pg_preflight(conn,plan,cid,record,request,selection_context=prepared['selection_context']);issue()
   return checked
  evaluation,bundle,hit,request,execution=paid_selected_read(conn,active,case,convocation_id=cid,
   profile_id=old['profile_id'],object_type=record['object_type'],anchor=anchor,anchor_sha=anchor_sha,
   pdf=pdf,prepared=prepared,document_context=context,limits=limits,before_request_factory=before,
   local_text_result=local,generation_options={'after_attempt':guard.observe_attempt},fallback_config=None)
  saved=persist_completed_llm1(conn,convocation_id=cid,document_uuid=anchor,
   extraction={**bundle,'evaluation_id':evaluation,'allowed_models':[model]},document_sha256=anchor_sha,
   source_url=first['source_url'])
  result={**record,**saved,'status':'llm1_complete','requires_human_review':bundle['requires_human_review'],
   'matching_eligible':True,'llm1_prior_preserved':False,'llm1_model':model,'llm1_cached':hit,
   'llm1_evaluation_id':evaluation,'llm1_request_hash':request,'llm1_cost_usd':0 if hit else bundle['cost_usd'],
   'facet_count':len(bundle['facets']),'review_completion_policy':VERSION,
   'original_outcome_sha256':next(c['original_outcome_sha256'] for c in plan['cases'] if c['convocation_id']==cid),
   'llm1_input':prepared['llm1_input'],'llm1_model_routing':routing}
  # Old error/review provenance remains in the separate immutable source outcome.
  for key in ('error_type','error_chain','document_preparation_review'):result.pop(key,None)
  once(out,result)
  print(json.dumps({'convocation_id':cid,'status':result['status'],'facet_count':result['facet_count'],'cost_usd':result['llm1_cost_usd']}),flush=True)
  return result


def execute(plan,settings):
 folder=Path(plan['folder']);STOP.clear()
 guard=None
 costs=audit_summary(folder)
 if costs['usage_unknown_attempts']:raise ValueError('Paid usage is uncertain; no retry')
 limits=RunLimits(max_accepted=9,max_candidates=9,max_jev_calls=0,max_llm1_calls=9,max_embedding_calls=10,
  model_workers=2,llm1_calls=costs['llm1_calls'],embedding_calls=costs['embedding_calls'])
 records=[]
 def work(cid):
  try:return read_case(settings,plan,cid,limits,guard)
  except Exception as e:
   selected.stop_handoff_after_local_error(guard,e,phase='explicit_sector_review_completion',convocation_id=cid)
   value={'convocation_id':cid,'status':'completion_stopped','error_type':type(e).__name__,'no_automatic_retry':True}
   once(folder/str(cid)/'outcome.private.json',value)
   print(json.dumps(value),flush=True)
   return value
 with psycopg.connect(settings.database_url,row_factory=dict_row,autocommit=True) as owner:
  if not owner.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()['locked']:raise ValueError('Another coordinator owns PROD2')
  guard=OwnedReviewGuard(folder,owner=owner,binding={'version':VERSION,'manifest_sha256':sha(folder/'manifest.private.json')},max_candidates=9,max_new_documents=20,stop=STOP)
  guard.check_audit(costs);guard.before_request();limits.handoff_guard=guard
  previous_handlers={}
  if threading.current_thread() is threading.main_thread():
   for sig in (signal.SIGINT,signal.SIGTERM):
    previous_handlers[sig]=signal.getsignal(sig);signal.signal(sig,lambda *_:STOP.set())
  with ThreadPoolExecutor(max_workers=2) as pool:
   futures=[pool.submit(work,cid) for cid in IDS]
   for future in as_completed(futures):records.append(future.result())
  for sig,handler in previous_handlers.items():signal.signal(sig,handler)
 report={'version':VERSION,'ids':list(IDS),'records':sorted(records,key=lambda x:x['convocation_id']),
  'llm1_complete':sum(r['status']=='llm1_complete' for r in records),'costs':audit_summary(folder),
  'guard':guard.report(),'original_report_preserved':True,'source_calls':0,'redis_calls':0,'at_utc':datetime.now(timezone.utc).isoformat()}
 once(folder/'report.private.json',report);return report


def parser():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['plan','prepare','execute'])
 p.add_argument('--folder',type=Path,required=True);p.add_argument('--go',action='store_true')
 return p


def main(argv=None):
 args=parser().parse_args(argv);folder=args.folder.absolute()
 if not folder.is_relative_to(ROOT/'tmp') or folder.is_symlink() or any(p.is_symlink() for p in folder.parents):raise ValueError('Literal private follow-up folder under tmp required')
 if args.command=='plan':
  folder.mkdir(parents=True,exist_ok=True,mode=0o700);once(folder/'manifest.private.json',frozen_plan(folder))
  print(json.dumps({'status':'planned','ids':list(IDS),'old_inputs_unchanged':True,'new_context_maximum_tokens':350000}));return
 plan=verify_plan(folder)
 if args.command=='prepare':
  for cid in IDS:
   prepared=prepare_case(plan,cid)
   once(folder/str(cid)/'prepared-proof.private.json',{'analysis_sha256':prepared['analysis_sha256'],'page_count':prepared['page_count'],'document_set_hash':prepared['document_set_hash']})
  print(json.dumps({'status':'prepared','cases':9,'provider_calls':0}));return
 if not args.go:raise ValueError('Execution requires explicit go')
 tracked=[str(Path(__file__).relative_to(ROOT))]
 if any(subprocess.run(['git','ls-files','--error-unmatch',p],cwd=ROOT,capture_output=True).returncode for p in tracked):raise ValueError('Commit runtime before execution')
 if subprocess.run(['git','diff','--quiet','HEAD','--',*tracked],cwd=ROOT).returncode:raise ValueError('Commit runtime before execution')
 bindings={p:sha(ROOT/p) for p in tracked}
 original_paths=read_private(ROOT/'tmp/ia-industrial-execution-20261020/chain-plan.runtime-recovery-002.private.json')['runtime_bindings']
 bindings.update({p:sha(ROOT/p) for p in original_paths})
 for p,digest in bindings.items():
  committed=subprocess.run(['git','show','HEAD:'+p],cwd=ROOT,capture_output=True)
  if committed.returncode or hashlib.sha256(committed.stdout).hexdigest()!=digest:raise ValueError('Imported runtime differs from committed Git source')
 once(folder/'runtime-bindings.private.json',bindings)
 with production_settings(ssh_host='ubuntu@168.129.177.199',ssh_key=ROOT/'credentials/ssh-key-2026-07-14.key') as settings:
  result=execute(plan,settings)
 print(json.dumps({k:v for k,v in result.items() if k!='records'}))


if __name__=='__main__':main()
