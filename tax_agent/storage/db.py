import hashlib
import json
import sqlite3
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from tax_agent.models import BusinessError

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS enterprises(enterprise_id TEXT PRIMARY KEY,name TEXT NOT NULL,alias TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS enterprise_permissions(user_id TEXT REFERENCES users,enterprise_id TEXT REFERENCES enterprises,can_read INTEGER NOT NULL,can_write INTEGER NOT NULL,PRIMARY KEY(user_id,enterprise_id));
CREATE TABLE IF NOT EXISTS invoices(enterprise_id TEXT REFERENCES enterprises,invoice_id TEXT,invoice_date TEXT NOT NULL,seller TEXT NOT NULL,item TEXT NOT NULL,amount_cents INTEGER NOT NULL,purpose TEXT,note TEXT,row_version INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(enterprise_id,invoice_id));
CREATE TABLE IF NOT EXISTS bank_transactions(enterprise_id TEXT REFERENCES enterprises,transaction_id TEXT,transaction_date TEXT NOT NULL,direction TEXT NOT NULL CHECK(direction IN ('income','expense')),amount_cents INTEGER NOT NULL CHECK(amount_cents>0),counterparty TEXT NOT NULL,note TEXT,PRIMARY KEY(enterprise_id,transaction_id));
CREATE TABLE IF NOT EXISTS memory_scopes(user_id TEXT,enterprise_id TEXT,epoch INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(user_id,enterprise_id));
CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY,user_id TEXT,enterprise_id TEXT,thread_id TEXT UNIQUE,memory_epoch INTEGER NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turns(turn_id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions,message_id TEXT UNIQUE,status TEXT NOT NULL,answer TEXT,model TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(message_id TEXT PRIMARY KEY,session_id TEXT REFERENCES sessions,user_id TEXT,enterprise_id TEXT,turn_id TEXT,role TEXT NOT NULL,content TEXT NOT NULL,memory_epoch INTEGER NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY,user_id TEXT,enterprise_id TEXT,month TEXT,goal TEXT,kind TEXT,targets_json TEXT NOT NULL,status TEXT NOT NULL,pending_json TEXT NOT NULL,done_json TEXT NOT NULL,revision INTEGER NOT NULL,source_message_id TEXT,source_refs TEXT NOT NULL,created_at TEXT NOT NULL,deleted_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_unfinished_task ON tasks(user_id,enterprise_id) WHERE deleted_at IS NULL AND status IN ('ACTIVE','WAITING_INPUT','BLOCKED');
CREATE TABLE IF NOT EXISTS preferences(memory_id TEXT PRIMARY KEY,user_id TEXT,enterprise_id TEXT,key TEXT NOT NULL,value TEXT NOT NULL,source_message_id TEXT,source_refs TEXT NOT NULL,revision INTEGER NOT NULL,deleted_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS preference_key ON preferences(user_id,enterprise_id,key) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS invalidated_sources(user_id TEXT,enterprise_id TEXT,message_id TEXT,start_pos INTEGER,end_pos INTEGER,target_id TEXT,PRIMARY KEY(user_id,enterprise_id,message_id,start_pos,end_pos,target_id));
CREATE TABLE IF NOT EXISTS rule_versions(rule_id TEXT,version TEXT,name TEXT NOT NULL,content TEXT NOT NULL,content_hash TEXT NOT NULL,condition_json TEXT NOT NULL,effective_from TEXT NOT NULL,effective_to TEXT,status TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,source TEXT NOT NULL,reviewed_hash TEXT,PRIMARY KEY(rule_id,version));
CREATE TABLE IF NOT EXISTS rule_scopes(rule_id TEXT,version TEXT,enterprise_id TEXT REFERENCES enterprises,PRIMARY KEY(rule_id,version,enterprise_id),FOREIGN KEY(rule_id,version) REFERENCES rule_versions);
CREATE TABLE IF NOT EXISTS write_requests(request_id TEXT PRIMARY KEY,user_id TEXT,enterprise_id TEXT,turn_id TEXT,source_refs TEXT NOT NULL,task_id TEXT,task_revision INTEGER,patch_json TEXT NOT NULL,payload_hash TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations(operation_id TEXT PRIMARY KEY,request_id TEXT REFERENCES write_requests,user_id TEXT,enterprise_id TEXT,invoice_id TEXT,before_json TEXT NOT NULL,after_json TEXT NOT NULL,row_version INTEGER NOT NULL,state TEXT NOT NULL,result_json TEXT,UNIQUE(request_id,enterprise_id,invoice_id));
CREATE TABLE IF NOT EXISTS audit_events(event_id TEXT PRIMARY KEY,turn_id TEXT,user_id TEXT,enterprise_id TEXT,event TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turn_jobs(turn_id TEXT PRIMARY KEY REFERENCES turns,state_json TEXT,attempts INTEGER NOT NULL DEFAULT 0,owner TEXT,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tool_invocations(turn_id TEXT,call_id TEXT,name TEXT NOT NULL,arguments_json TEXT NOT NULL,data_json TEXT,result_json TEXT,PRIMARY KEY(turn_id,call_id));
CREATE TABLE IF NOT EXISTS context_summaries(session_id TEXT,memory_epoch INTEGER,boundary_id TEXT,summary_json TEXT NOT NULL,PRIMARY KEY(session_id,memory_epoch));
CREATE TABLE IF NOT EXISTS request_manifests(turn_id TEXT PRIMARY KEY,user_id TEXT,enterprise_id TEXT,manifest_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batch_items(turn_id TEXT,item_id TEXT,invoice_id TEXT,target_json TEXT NOT NULL,status TEXT NOT NULL,result_json TEXT,PRIMARY KEY(turn_id,item_id));
CREATE TABLE IF NOT EXISTS read_attempts(turn_id TEXT,request_key TEXT,attempts INTEGER NOT NULL,last_status TEXT,PRIMARY KEY(turn_id,request_key));
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid4().hex


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


class Database:
    def __new__(cls,path):
        if cls is Database and os.getenv("TAX_AGENT_DATABASE_URL"):
            from tax_agent.storage.postgres import PostgresDatabase
            return object.__new__(PostgresDatabase)
        return object.__new__(cls)

    def __init__(self, path: Path):
        self.path = Path(path)
        self._active_call = ContextVar("tool_call",default=None)

    @property
    def exists(self):
        return self.path.exists()

    @property
    def active_call(self):
        return self._active_call.get()

    @active_call.setter
    def active_call(self,value):
        self._active_call.set(value)

    def receipt(self, data, conn):
        """记忆修改与工具回执在同一事务提交；中断恢复不再次修改revision。"""
        if self.active_call:
            conn.execute("UPDATE tool_invocations SET data_json=? WHERE turn_id=? AND call_id=?",
                         (dumps(data), *self.active_call))
        return data

    @contextmanager
    def transaction(self, immediate=False):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def schema(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            conn.executescript(SCHEMA)

    def event(self, ctx, event, payload, conn=None):
        record = (uid(), ctx.turn_id if ctx else None, ctx.user_id if ctx else None,
                  ctx.enterprise_id if ctx else None, event, dumps(payload), now())
        if conn is not None:
            conn.execute("INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)", record)
        else:
            with self.transaction() as connection:
                connection.execute("INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)", record)
        return record[0]

    def authorize(self, user_id, enterprise_id, write=False, conn=None):
        if conn is None:
            with self.transaction() as connection:
                return self.authorize(user_id, enterprise_id, write, connection)
        row = conn.execute("SELECT can_read,can_write FROM enterprise_permissions WHERE user_id=? AND enterprise_id=?", (user_id, enterprise_id)).fetchone()
        if row is None or not row["can_read"] or (write and not row["can_write"]):
            raise BusinessError("PERMISSION_DENIED", f"{user_id}无权{'修改' if write else '查看'}{enterprise_id}资料", "denied")
        return dict(row)

    def scope(self, ctx, enterprise_id=None, write=False, conn=None):
        target = enterprise_id or ctx.enterprise_id
        permission = self.authorize(ctx.user_id, target, write, conn)
        if target != ctx.enterprise_id:
            raise BusinessError("SWITCH_ENTERPRISE", f"请在入口切换到{target}，再处理该企业；当前会话属于{ctx.enterprise_id}")
        return permission

    def epoch(self, user_id, enterprise_id, conn):
        return conn.execute("SELECT epoch FROM memory_scopes WHERE user_id=? AND enterprise_id=?", (user_id, enterprise_id)).fetchone()[0]

    def new_session(self, user_id, enterprise_id):
        self.authorize(user_id, enterprise_id)
        with self.transaction(True) as conn:
            conn.execute("INSERT OR IGNORE INTO memory_scopes VALUES(?,?,0)", (user_id, enterprise_id))
            row = dict(session_id=uid(), user_id=user_id, enterprise_id=enterprise_id, thread_id=uid(),
                       memory_epoch=self.epoch(user_id, enterprise_id, conn), created_at=now())
            conn.execute("INSERT INTO sessions VALUES(:session_id,:user_id,:enterprise_id,:thread_id,:memory_epoch,:created_at)", row)
        return row

    def session(self, session_id, user_id, enterprise_id):
        self.authorize(user_id, enterprise_id)
        with self.transaction(True) as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id=? AND user_id=? AND enterprise_id=?", (session_id, user_id, enterprise_id)).fetchone()
            if row is None:
                raise BusinessError("SESSION_SCOPE", "会话不存在或不属于当前用户和企业", "denied")
            session = dict(row)
            epoch = self.epoch(user_id, enterprise_id, conn)
            if session["memory_epoch"] != epoch:
                session.update(memory_epoch=epoch, thread_id=uid())
                conn.execute("UPDATE sessions SET memory_epoch=?,thread_id=? WHERE session_id=?", (epoch, session["thread_id"], session_id))
            return session
