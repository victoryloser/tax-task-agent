"""真实PostgreSQL TCP集成测试；使用独立测试库，不连接日常业务库。"""
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from tax_agent.config import ROOT, Settings
from tax_agent.models import BusinessError
from tax_agent.runtime.graph import Harness
from tax_agent.storage.db import Database
from tax_agent.storage.import_data import initialize_sample as initialize

sys.path.insert(0,str(ROOT/"tests"))
from test_runtime import Scripted,call
from test_batches import BatchProvider
from test_queue_recovery import PROCESS_SCRIPT


@pytest.fixture
def pg(tmp_path,monkeypatch):
    admin=os.environ["TEST_POSTGRES_URL"]
    name="tax_test_"+uuid4().hex
    with psycopg.connect(admin,autocommit=True) as conn:
        conn.execute("CREATE DATABASE "+name)
    url=admin.rsplit("/",1)[0]+"/"+name
    monkeypatch.setenv("TAX_AGENT_DATABASE_URL",url)
    db=Database(tmp_path/"host-a/business.sqlite3")
    initialize(db)
    yield db
    with psycopg.connect(admin,autocommit=True) as conn:
        conn.execute("DROP DATABASE "+name+" WITH (FORCE)")


def test_postgres_full_graph_write_readback_and_repeat(pg,tmp_path):
    target={"invoice_id":"F002","purpose":"培训"}
    h=Harness(Settings(db_path=pg.path,checkpoint_path=tmp_path/"a/checkpoints.sqlite3"),BatchProvider([target],execute=[target]))
    s=pg.new_session("U001","C001")
    turn=h.respond(s["session_id"],"U001","C001","F002用途保存为培训。")
    assert turn.status=="completed" and "已保存" in turn.answer
    again=Harness(Settings(db_path=tmp_path/"other-host/business.sqlite3",checkpoint_path=tmp_path/"other-host/checkpoints.sqlite3"),Scripted([]))
    assert again.respond(s["session_id"],"U001","C001","F002用途保存为培训。",turn.turn_id).answer==turn.answer
    with pg.transaction() as conn:
        assert conn.execute("SELECT row_version FROM invoices WHERE enterprise_id='C001' AND invoice_id='F002'").fetchone()[0]==1
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]==1


def test_postgres_fencing_rejects_old_worker(pg,tmp_path):
    with pg.lock("U001","C001"):
        old=pg._fence.get()
    other=Database(tmp_path/"host-b/business.sqlite3")
    with other.lock("U001","C001"):
        assert other._fence.get()[2]>old[2]
        token=pg._fence.set(old)
        try:
            with pytest.raises(BusinessError,match="代次"):
                with pg.transaction(True) as conn:
                    conn.execute("UPDATE invoices SET purpose='stale' WHERE enterprise_id='C001'")
        finally:
            pg._fence.reset(token)
    with pg.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invoices WHERE purpose='stale'").fetchone()[0]==0


def worker(tmp_path,db,mode,name):
    script=tmp_path/(name+".py")
    script.write_text(PROCESS_SCRIPT,encoding="utf-8")
    local=tmp_path/name/"business.sqlite3"
    return subprocess.Popen([sys.executable,str(script),mode,str(local),str(tmp_path/"calls.txt")],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)


def test_postgres_remote_workers_do_not_share_local_files(pg,tmp_path):
    h=Harness(Settings(db_path=pg.path,checkpoint_path=tmp_path/"submit/cp.sqlite3"),Scripted([]))
    sessions=[pg.new_session("U001",e) for e in ("C001","C002")]
    for i in range(6):
        s=sessions[i%2]
        h.submit(s["session_id"],"U001",s["enterprise_id"],f"query {i}")
    processes=[worker(tmp_path,pg,"race",name) for name in ("host-a","host-b")]
    for process in processes:
        _,err=process.communicate(timeout=90)
        assert process.returncode==0,err.decode()
    assert len((tmp_path/"calls.txt").read_text().splitlines())==6
    with pg.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM turns WHERE status='completed'").fetchone()[0]==6
        assert conn.execute("SELECT MAX(attempts) FROM turn_jobs").fetchone()[0]==1
    assert (tmp_path/"host-a/process-cp.sqlite3").exists()
    assert (tmp_path/"host-b/process-cp.sqlite3").exists()


def test_postgres_dead_host_committed_operation_recovers_on_other_host(pg,tmp_path):
    h=Harness(Settings(db_path=pg.path,checkpoint_path=tmp_path/"submit/cp.sqlite3"),Scripted([]))
    s=pg.new_session("U001","C001")
    tid=h.submit(s["session_id"],"U001","C001","Save F002 purpose process recovery")
    crashed=worker(tmp_path,pg,"crash","host-a")
    _,err=crashed.communicate(timeout=60)
    assert crashed.returncode==73,err.decode()
    resumed=worker(tmp_path,pg,"resume","host-b")
    _,err=resumed.communicate(timeout=60)
    assert resumed.returncode==0,err.decode()
    assert h.result(tid,"U001","C001").status=="completed"
    with pg.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]==1
        assert conn.execute("SELECT state FROM operations").fetchone()[0]=="verified"
        assert conn.execute("SELECT row_version FROM invoices WHERE enterprise_id='C001' AND invoice_id='F002'").fetchone()[0]==1


def test_sqlite_migration_preserves_data_and_recovery_tables(tmp_path,monkeypatch):
    from tax_agent.storage.migrate import sqlite_to_postgres
    import sqlite3
    monkeypatch.delenv("TAX_AGENT_DATABASE_URL",raising=False)
    source=tmp_path/"source.sqlite3"
    db=Database(source)
    initialize(db)
    h=Harness(Settings(db_path=source,checkpoint_path=tmp_path/"source-cp.sqlite3"),Scripted([]))
    s=db.new_session("U001","C001")
    tid=h.submit(s["session_id"],"U001","C001","pending query")
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE invoices SET purpose='already saved',row_version=3 WHERE enterprise_id='C001' AND invoice_id='F002'")
    admin=os.environ["TEST_POSTGRES_URL"]
    name="tax_migrate_"+uuid4().hex
    with psycopg.connect(admin,autocommit=True) as conn:
        conn.execute("CREATE DATABASE "+name)
    monkeypatch.setenv("TAX_AGENT_DATABASE_URL",admin.rsplit("/",1)[0]+"/"+name)
    try:
        result=sqlite_to_postgres(source)
        assert result["tables"]["invoices"]==40
        migrated=Database(tmp_path/"remote/business.sqlite3")
        with migrated.transaction() as conn:
            row=conn.execute("SELECT purpose,row_version FROM invoices WHERE enterprise_id='C001' AND invoice_id='F002'").fetchone()
            assert tuple(row)==("already saved",3)
            assert conn.execute("SELECT status FROM turns WHERE turn_id=?",(tid,)).fetchone()[0]=="queued"
        resumed=Harness(Settings(db_path=migrated.path,checkpoint_path=tmp_path/"remote/cp.sqlite3"),Scripted([{"role":"assistant","content":"done"}])).run_once()
        assert resumed.turn_id==tid and resumed.status=="completed"
        with pytest.raises(ValueError,match="必须为空"):
            sqlite_to_postgres(source)
    finally:
        with psycopg.connect(admin,autocommit=True) as conn:
            conn.execute("DROP DATABASE "+name+" WITH (FORCE)")
