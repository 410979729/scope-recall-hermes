"""Hash-bound TEST native A2 source and query gates. No model calls."""
import hashlib,json,sqlite3,time,tomllib
from pathlib import Path
FREEZE_SHA='0f51e27537709fd44df727d47325d80a9162b54027aba105fe9329504502fa90'
def readref(ref):
 p=Path(ref['path']);b=p.read_bytes()
 if hashlib.sha256(b).hexdigest()!=ref['sha256']:raise ValueError('A2_bound_artifact_changed')
 return json.loads(b)
def rows(home,db,sql,args=()):
 p=home/db
 if not p.exists():return []
 with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as c:
  c.row_factory=sqlite3.Row;return [dict(x) for x in c.execute(sql,args)]
def plan_for(binding):
 ref=binding['native_source_import']['method_freeze']
 if ref['sha256']!=FREEZE_SHA:raise ValueError('A2_unreviewed_method')
 method=readref(ref);plans=[p for p in method['plans'] if p['unit_id']==binding['native_source_import']['unit_id']]
 if len(plans)!=1:raise ValueError('A2_unit_identity')
 p=plans[0];root=Path(p['root']).resolve()
 if (binding['arm_id']!='A' or binding['host_id']!='codex_windows_desktop' or root.parent!=Path('F:/T/TEST-A2').resolve()
   or Path(binding['roots']['binding_root']).resolve()!=root or Path(binding['roots']['home_path']).resolve()!=Path(p['home']).resolve()
   or Path(binding['launch_contract']['working_directory']).resolve()!=Path(p['cwd']).resolve()):raise ValueError('A2_root_identity')
 if root!=Path(p['root']).absolute() or Path(p['home']).resolve()!=Path(p['home']).absolute() or Path(p['cwd']).resolve()!=Path(p['cwd']).absolute():raise ValueError('A2_linked_root_rejected')
 if binding['fixed_host']['executable_sha256']!=method['binary']['sha256']:raise ValueError('A2_executable_identity')
 return p
def permits_external_root(binding):
 if binding.get('native_source_import',{}).get('method')!='native-A2-reviewed-v2':return False
 p=plan_for(binding)
 result=verify_source({'unit_id':p['unit_id'],'source_records':readref(p['original_records'])},binding,'external-root-preflight')
 if result['status']!='IMPORTED_NATIVE_A2':raise ValueError('A2_unmaterialized_source_not_admitted')
 return True
def verify_source(row,binding,manifest_sha):
 p=plan_for(binding);refs=binding['native_source_import'];original=readref(p['original_records']);data=readref(p['source_input']);readref(p['request'])
 if p['unit_id']!=row['unit_id'] or original!=row['source_records']:raise ValueError('A2_original_source_changed')
 from p18_native_a_import import project_records
 expected=project_records(original)
 for item in expected['items']:
  if item['type']=='function_call_output':item['name']='synthetic_imported_tool_observation'
 if expected!=data:raise ValueError('A2_role_or_transport_changed')
 cp=Path(p['config']['path']);b=cp.read_bytes()
 if hashlib.sha256(b).hexdigest()!=p['config']['sha256'] or tomllib.loads(b.decode())['memories']['min_rollout_idle_hours']!=1:raise ValueError('A2_runtime_config_changed')
 r=readref(refs['receipt'])
 ledger=Path('F:/SCOPERECALL更新项目/worktrees/scope-recall-v1.1/.execution/TEST-MODEL-BUDGET-V1/call-budget.sqlite3')
 with sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True) as db:
  db.row_factory=sqlite3.Row
  for entry in r['budget']:
   actual=db.execute('select * from codex_submissions where operation_id=?',(entry['operation_id'],)).fetchone()
   if actual is None or any(actual[k]!=entry[k] for k in ('request_sha256','actual_input','actual_output','reserved_input','reserved_output','status','finished_ns')):raise ValueError('A2_source_ledger_changed')
 if r['unit_id']!=row['unit_id'] or r['source_turn_start_calls']!=1:raise ValueError('A2_source_receipt_identity')
 result={'status':'SOURCE_PREPARATION_FAILED','manifest_sha256':manifest_sha,'source_receipt':refs['receipt'],'source_capture_refs':[],'import_session_id':r.get('source_thread_id'),'host_L1_capture_proven':False,'query_ready':False,'error':r.get('error')}
 if r['status']!='SOURCE_MATERIALIZED_IDLE_PENDING':return result
 if r['ACK_validation']['status']!='EXACT_SINGLE_ACK' or r['source_items_matched']!=len(original):raise ValueError('A2_non_ACK_source')
 home=Path(p['home']).resolve();sid=r['source_thread_id'];rp=Path(r['source_rollout_at_preparation']['path']).resolve()
 if not rp.is_relative_to(home/'sessions') or hashlib.sha256(rp.read_bytes()).hexdigest()!=r['source_rollout_at_preparation']['sha256']:raise ValueError('A2_rollout_changed')
 lines=[json.loads(x) for x in rp.read_text(encoding='utf-8').splitlines() if x]
 meta=[x['payload']['id'] for x in lines if x.get('type')=='session_meta']
 if meta!=[sid]:raise ValueError('A2_rollout_session_mismatch')
 result.update(status='IMPORTED_NATIVE_A2',source_rollout=r['source_rollout_at_preparation'],source_ACK_receipt=refs['receipt'],main_model_calls=1,source_preparation_is_not_formal_query=True,native_generation_verified=False)
 return result
def observe_state(binding):
 p=plan_for(binding);r=readref(binding['native_source_import']['receipt']);sid=r.get('source_thread_id');home=Path(p['home']);s=rows(home,'state_5.sqlite','select id,source,preview,updated_at_ms,memory_mode,archived from threads where id=?',(sid,))
 if r['status']!='SOURCE_MATERIALIZED_IDLE_PENDING' or len(s)!=1:raise ValueError('A2_source_not_successfully_prepared')
 s=s[0]
 if s['memory_mode']!='enabled' or s['archived'] or s['source'] not in ('vscode','cli','atlas','chatgpt') or not s['preview']:raise ValueError('A2_source_not_eligible')
 jobs=rows(home,'memories_1.sqlite','select * from jobs');not_before=s['updated_at_ms']+3600000
 for j in jobs:
  if j['kind']=='memory_consolidate_global':
   not_before=max(not_before,1000*(j.get('retry_at') or 0),1000*(j.get('lease_until') or 0) if j['status']=='running' else 0)
 outputs=rows(home,'memories_1.sqlite','select thread_id,source_updated_at,generated_at,selected_for_phase2,length(raw_memory) raw_chars,length(rollout_summary) summary_chars from stage1_outputs where thread_id=?',(sid,))
 files=[]
 for name in ('MEMORY.md','raw_memories.md'):
  path=home/'memories'/name
  if path.is_file():files.append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size})
 return {'source':s,'jobs':jobs,'outputs':outputs,'files':files,'not_before_epoch_ms':not_before,'idle_due':time.time()*1000>=not_before}
def require_query_ready(binding):
 # Ready is source-specific and requires successful real global consolidation,
 # not merely a file written by the harness or an empty raw-memory placeholder.
 state=observe_state(binding);outputs=state['outputs'];globaljobs=[j for j in state['jobs'] if j['kind']=='memory_consolidate_global']
 good=bool(outputs and outputs[0]['raw_chars']>0 and outputs[0]['summary_chars']>0 and outputs[0]['source_updated_at']>=state['source']['updated_at_ms']//1000 and outputs[0]['selected_for_phase2'])
 good=good and bool(globaljobs and globaljobs[0]['status']=='done' and globaljobs[0].get('last_error') is None and len(state['files'])==2 and all(f['bytes']>0 for f in state['files']))
 if not good:raise ValueError('A2_native_source_and_consolidation_not_ready')
 return state
