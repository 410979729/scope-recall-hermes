"""Focused finite scheduling checks with no model or network calls."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from scope_recall.contracts import InstanceBinding
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.runtime.instance import RuntimeInstanceConfig
from scope_recall.runtime.scheduling import SupervisorControl, next_wake, supervise
from v11_support import source_event


NOW = datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc)


def fixture(tmp_path, **settings):
    binding = InstanceBinding('TEST-agent', 'TEST-installation', tmp_path/'data',
                              frozenset({'TEST-a', 'TEST-b'}), True)
    core = MemoryCore(CoreConfig(binding)); core.initialize()
    raw = dict(binding=dict(agent_id=binding.agent_id, installation_id=binding.installation_id,
                            data_directory=str(binding.data_directory), scope_ids=sorted(binding.scope_ids), test_mode=True),
               session_id='TEST-session', allowed_scope_ids=['TEST-a'], actor_origin='human_direct',
               project_id='TEST-project', branch_id='TEST-main', supervisor_seconds=180,
               supervisor_max_drains=8, worker_min_interval_seconds=1,
               auxiliary=dict(external_embedding=False, external_consolidation=False))
    raw.update(settings)
    path = tmp_path/'worker.json'; path.write_text(json.dumps(raw), encoding='utf-8')
    return core, RuntimeInstanceConfig.from_mapping(raw), path


def queue(core, cfg, *, ref='TEST-source', kind='rebuild_projection', state='pending',
          due=NOW, error=None, scope='TEST-a', project='TEST-project'):
    with core.storage.write(cfg.context()) as tx:
        tx._check(write=True).execute('''INSERT INTO work_items
            (work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,state,available_at,last_error_code)
            VALUES (?,?,1,?,?,'TEST-main',?,?,?)''',
            (kind,ref,scope,project,state,due.isoformat().replace('+00:00','Z'),error))


def test_next_due_preserves_audience_cooldown_budget_and_purge(tmp_path):
    core,cfg,_ = fixture(tmp_path)
    queue(core,cfg,state='failed',error='http_503',due=NOW)
    queue(core,cfg,ref='TEST-foreign-scope',scope='TEST-b',due=NOW-timedelta(days=2))
    queue(core,cfg,ref='TEST-foreign-project',project='TEST-other',due=NOW-timedelta(days=2))
    queue(core,cfg,ref='TEST-exhausted',state='failed',error='auto_retry:2|http_503',due=NOW-timedelta(days=2))
    queue(core,cfg,ref='TEST-no-embedding',kind='embed',due=NOW-timedelta(days=2))
    plan=next_wake(cfg,now=NOW)
    assert plan.due_at == '2026-09-12T01:00:00Z' and plan.reason == 'failure_cooldown'
    assert plan.blocked == 1
    # daily_work_limit defaults to 0 (uncapped), so an exhausted cap must be
    # stated to be exercised.
    capped = replace(cfg, daily_work_limit=256)
    budget=cfg.binding.data_directory/'runtime-worker-day.json'
    budget.write_text(json.dumps(dict(installation_id=cfg.binding.installation_id,day='2026-09-12',used=capped.daily_work_limit)))
    before=budget.read_bytes()
    assert next_wake(capped,now=NOW).due_at == '2026-09-13T00:00:00Z'
    # The same spent counter under the uncapped default must NOT defer. With a
    # limit of 0 every `used >= limit` comparison is trivially true, which would
    # put an uncapped instance to sleep until tomorrow — the exact dormancy that
    # setting exists to avoid.
    assert next_wake(cfg,now=NOW).due_at != '2026-09-13T00:00:00Z'
    queue(core,cfg,ref='TEST-purge',kind='purge')
    assert next_wake(capped,now=NOW).due_at == '2026-09-12T00:00:00Z'
    assert budget.read_bytes() == before  # Planning neither spends nor resets.
    plan = next_wake(replace(capped, daily_work_limit=capped.daily_work_limit+1),now=NOW,
                     unavailable_until={'purge':NOW+timedelta(seconds=300)})
    assert plan.due_at == '2026-09-12T00:05:00Z' and plan.reason == 'capability_cooldown'
    with core.storage.write(cfg.context()) as tx:
        tx._check(write=True).execute("UPDATE work_items SET state='failed',last_error_code='invalid_derivation'")
    plan=next_wake(cfg,now=NOW)
    assert plan.due_at is None and plan.reason=='failed_terminal' and plan.failed>0


def test_quiet_supervisor_wakes_due_work_without_chat_and_does_not_spin(tmp_path):
    core,cfg,path=fixture(tmp_path)
    queue(core,cfg,ref='TEST-now')
    queue(core,cfg,ref='TEST-later',due=NOW+timedelta(seconds=65))
    elapsed=[0.0]; calls=[]; sleeps=[]
    def sleep(seconds):
        sleeps.append(seconds); elapsed[0]+=seconds
    def utc():
        return NOW+timedelta(seconds=elapsed[0])
    def drain(remaining):
        assert 0 < remaining <= cfg.drain_seconds
        calls.append(elapsed[0])
        with core.storage.write(cfg.context()) as tx:
            cur=tx._check(write=True).execute("""UPDATE work_items SET state='done' WHERE work_id=(
                SELECT work_id FROM work_items WHERE state='pending' AND available_at<=? ORDER BY work_id LIMIT 1)""",
                (utc().isoformat().replace('+00:00','Z'),))
            return 0, {'completed':cur.rowcount}
    assert supervise(path,drain,clock=lambda:elapsed[0],sleep=sleep,utc_now=utc) == 0
    assert calls == [0,65,66]
    assert max(sleeps) <= 60 and len(sleeps) == 3
    assert SupervisorControl(cfg).read()['state'] == 'idle'


def test_supervisor_single_owner_and_idle_exit_generation_handshake(tmp_path):
    _,cfg,path=fixture(tmp_path)
    entered=threading.Event(); release=threading.Event(); calls=[]
    def drain(_remaining):
        calls.append(1); entered.set(); assert release.wait(2)
        return 0, {'completed':0}
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(supervise,path,drain)
        assert entered.wait(2)
        try:
            assert supervise(path,lambda _: (_ for _ in ()).throw(AssertionError('duplicate drain'))) == 0
        finally:
            release.set()
        assert first.result(2) == 0
    assert len(calls)==1
    control=SupervisorControl(cfg)
    revision=control.read()['wake_revision']
    control.request()
    assert not control.close_if_unchanged(revision,state='idle')
    assert control.close_if_unchanged(control.read()['wake_revision'],state='idle')


def test_finite_limit_does_not_renew_and_records_pending_resume(tmp_path):
    core,cfg,path=fixture(tmp_path,supervisor_max_drains=2)
    queue(core,cfg)
    elapsed=[0.0]; calls=[]
    def drain(_remaining):
        calls.append(elapsed[0]); SupervisorControl(cfg).request()
        return 0, {'completed':1}
    assert supervise(path,drain,clock=lambda:elapsed[0],sleep=lambda t:elapsed.__setitem__(0,elapsed[0]+t),
                     utc_now=lambda:NOW+timedelta(seconds=elapsed[0])) == 0
    state=SupervisorControl(cfg).read()
    assert calls==[0,1] and state['state']=='suspended' and state['drains']==2
    assert state['reason']=='supervisor_limit' and state['accepting'] is False
    assert state['deadline_at']=='2026-09-12T00:03:00Z'


def test_recovery_progress_continues_once_and_budget_waits_legacy_repair(tmp_path):
    core,cfg,path=fixture(tmp_path)
    queue(core,cfg,kind='consolidate',state='failed',error='INPUT_INVALID')
    # An explicit cap: the default is 0, which means uncapped and never defers.
    configured=replace(cfg,daily_work_limit=256,
                       auxiliary=replace(cfg.auxiliary,external_consolidation=True,consolidation=object()))
    assert next_wake(configured,now=NOW).reason == 'legacy_source_repair'
    budget=cfg.binding.data_directory/'runtime-worker-day.json'
    budget.write_text(json.dumps(dict(installation_id=cfg.binding.installation_id,day='2026-09-12',used=configured.daily_work_limit)))
    assert next_wake(configured,now=NOW).due_at=='2026-09-13T00:00:00Z'
    assert next_wake(replace(configured,daily_work_limit=0),now=NOW).reason == 'legacy_source_repair'
    with core.storage.write(cfg.context()) as tx:
        tx._check(write=True).execute("UPDATE work_items SET last_error_code='chunk_checked:1106|INPUT_INVALID'")
    assert next_wake(configured,now=NOW).due_at is None
    elapsed=[0.0]; calls=[]
    def drain(_remaining):
        calls.append(elapsed[0])
        return 0, {'completed':0,'recovered':1 if len(calls)==1 else 0}
    assert supervise(path,drain,clock=lambda:elapsed[0],sleep=lambda t:elapsed.__setitem__(0,elapsed[0]+t),
                     utc_now=lambda:NOW+timedelta(seconds=elapsed[0]))==0
    assert calls==[0,1]
    assert SupervisorControl(cfg).read()['state']=='blocked'


def test_real_detached_supervisor_processes_future_local_work_after_host_exit(tmp_path):
    # A wall-clock +3s due date raced Python startup and could be consumed by
    # the first drain. Handshake at the actual scheduler sleep, then advance its
    # injected clocks; the worker, SQLite and host-exit boundary remain real.
    core,cfg,path=fixture(tmp_path,supervisor_seconds=180,supervisor_max_drains=5,
                          drain_seconds=30,daily_work_limit=16)
    receipt=core.record_event(cfg.context(),source_event(),scope_id='TEST-a')
    ref=receipt.event_refs[0].ref
    base=datetime.now(timezone.utc)
    due=(base+timedelta(days=1)).isoformat().replace('+00:00','Z')
    with core.storage.write(cfg.context()) as tx:
        tx.work.enqueue('rebuild_projection',ref,1,available_at=due)
    marker=tmp_path/'host-exited.json'
    waiting=tmp_path/'supervisor-waiting'
    release=tmp_path/'work-now-due'
    child=tmp_path/'TEST-controlled-supervisor.py'
    child.write_text("""import sys,types,time
from datetime import datetime,timedelta
from pathlib import Path
root,config,waiting,release,base=sys.argv[1:]
package=types.ModuleType('scope_recall'); package.__path__=[root]
sys.modules['scope_recall']=package
from scope_recall.runtime import scheduling,worker_watchdog
original=scheduling.supervise
elapsed=[0.0]; base=datetime.fromisoformat(base)
def sleep(seconds):
    Path(waiting).touch()
    deadline=time.monotonic()+40
    while not Path(release).exists():
        if time.monotonic()>deadline: raise TimeoutError('TEST clock handshake')
        time.sleep(.02)
    elapsed[0]+=seconds
def controlled(path,drain,**kwargs):
    return original(path,drain,**kwargs,clock=lambda:elapsed[0],sleep=sleep,
                    utc_now=lambda:base+timedelta(seconds=elapsed[0]))
scheduling.supervise=controlled
raise SystemExit(worker_watchdog.main(['--config',config,'--python',sys.executable]))
""",encoding='utf-8')
    script="""import json,os,subprocess,sys
from pathlib import Path
child=subprocess.Popen([sys.executable,'-B',*sys.argv[1:-1]],
                       stdout=(log:=open(Path(sys.argv[1]).with_suffix('.log'),'w')),stderr=log,
                       start_new_session=(os.name!='nt'),
                       creationflags=int(getattr(subprocess,'CREATE_NO_WINDOW',0)))
Path(sys.argv[-1]).write_text(json.dumps({'pid':child.pid}))
os._exit(0)
"""
    parent=subprocess.Popen([sys.executable,'-B','-c',script,str(child),
                             str(Path(__file__).resolve().parents[2]),str(path),
                             str(waiting),str(release),base.isoformat(),str(marker)],
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                            creationflags=int(getattr(subprocess,'CREATE_NO_WINDOW',0)))
    out,err=parent.communicate(timeout=15)
    assert parent.returncode==0,(out,err)
    assert marker.exists()
    control=SupervisorControl(cfg); deadline=time.monotonic()+40
    try:
        while not waiting.exists() and time.monotonic()<deadline:
            time.sleep(.02)
        assert waiting.exists(),(control.read(),child.with_suffix('.log').read_text(encoding='utf-8'))
        assert control.read()['state']=='waiting'
        # Only after observing the second-pass scheduler do we release real DB
        # work. No 3-second startup assumption and no pytest/CI retries.
        with core.storage.write(cfg.context()) as tx:
            tx._check(write=True).execute(
                "UPDATE work_items SET available_at=? WHERE work_type='rebuild_projection'",
                (base.isoformat().replace('+00:00','Z'),))
    finally:
        release.touch()  # also releases the owned child on an assertion failure
    while time.monotonic()<deadline:
        state=control.read()
        if state.get('state') in {'idle','blocked','failed','suspended'}:
            break
        time.sleep(.02)
    assert state['state']=='blocked' and state['drains']>=2,state
    with core.storage.read(cfg.context()) as tx:
        row=tx._check().execute("SELECT state FROM work_items WHERE work_type='rebuild_projection'").fetchone()
        assert row['state']=='done'
    budget=json.loads((cfg.binding.data_directory/'runtime-worker-day.json').read_text())
    assert budget['used']==1  # An explicit cap, never the uncapped default.
