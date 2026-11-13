"""PostgreSQL共享业务库：兼容现有参数化SQL，数据库范围锁＋代次校验。"""
import os
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar

import psycopg

from tax_agent.models import BusinessError
from tax_agent.storage.db import Database, SCHEMA, digest


class Row:
    def __init__(self,names,values):
        self.names,self.values=names,values
    def keys(self):
        return self.names
    def __getitem__(self,key):
        return self.values[key] if isinstance(key,int) else self.values[self.names.index(key)]
    def __iter__(self):
        return iter(self.values)


class Cursor:
    def __init__(self,cursor):
        self.rowcount=cursor.rowcount
        self.rows=[Row([d.name for d in cursor.description],row) for row in cursor.fetchall()] if cursor.description else []
        self.index=0
    def fetchone(self):
        if self.index==len(self.rows):
            return None
        row=self.rows[self.index]
        self.index+=1
        return row
    def fetchall(self):
        result=self.rows[self.index:]
        self.index=len(self.rows)
        return result
    def __iter__(self):
        return iter(self.fetchall())


def table_contracts():
    connection=sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    contracts={}
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        rows=connection.execute("PRAGMA table_info("+name+")").fetchall()
        contracts[name]=([r[1] for r in rows],[r[1] for r in sorted(rows,key=lambda x:x[5]) if r[5]])
    connection.close()
    return contracts


CONTRACTS=table_contracts()


def translate(sql):
    mode=None
    if sql.startswith("INSERT OR IGNORE"):
        mode="ignore"
        sql=sql.replace("INSERT OR IGNORE","INSERT",1)
    elif sql.startswith("INSERT OR REPLACE"):
        mode="replace"
        sql=sql.replace("INSERT OR REPLACE","INSERT",1)
    match=re.match(r"INSERT INTO (\w+) VALUES",sql)
    if match:
        table=match[1]
        columns,primary=CONTRACTS[table]
        sql=sql.replace("INSERT INTO "+table+" VALUES","INSERT INTO "+table+" ("+",".join(columns)+") VALUES",1)
    if mode=="ignore":
        sql+=" ON CONFLICT DO NOTHING"
    elif mode=="replace":
        sql+=" ON CONFLICT ("+",".join(primary)+") DO UPDATE SET "+",".join(c+"=EXCLUDED."+c for c in columns if c not in primary)
    sql=sql.replace("?","%s")
    return re.sub(r"(?<!:):(\w+)",r"%(\1)s",sql)


class Connection:
    def __init__(self,raw):
        self.raw=raw
    def execute(self,sql,parameters=()):
        try:
            return Cursor(self.raw.execute(translate(sql),parameters))
        except psycopg.IntegrityError as error:
            raise sqlite3.IntegrityError(str(error)) from error
        except psycopg.Error as error:
            raise sqlite3.OperationalError(str(error)) from error
    def executemany(self,sql,parameters):
        for values in parameters:
            self.execute(sql,values)


class PostgresDatabase(Database):
    is_postgres=True
    def __init__(self,path):
        super().__init__(path)
        self.url=os.environ["TAX_AGENT_DATABASE_URL"]
        self._fence=ContextVar("postgres_scope_fence",default=None)

    @property
    def exists(self):
        with psycopg.connect(self.url) as conn:
            present=conn.execute("SELECT to_regclass('public.enterprises')").fetchone()[0]
            return bool(present and conn.execute("SELECT COUNT(*) FROM enterprises").fetchone()[0])

    @contextmanager
    def transaction(self,immediate=False):
        try:
            with psycopg.connect(self.url) as connection:
                fence=self._fence.get()
                if fence:
                    user,enterprise,generation=fence
                    current=connection.execute("SELECT generation FROM scope_fences WHERE user_id=%s AND enterprise_id=%s FOR SHARE",(user,enterprise)).fetchone()
                    if not current or current[0]!=generation:
                        raise BusinessError("STALE_WORKER","当前worker的锁代次已失效，禁止继续读写旧任务","conflict")
                yield Connection(connection)
        except psycopg.Error as error:
            raise sqlite3.OperationalError(str(error)) from error

    def schema(self):
        schema=re.sub(r"CREATE TABLE IF NOT EXISTS (\w+)\(",r"CREATE TABLE IF NOT EXISTS \1(rowid BIGSERIAL UNIQUE,",SCHEMA)
        with psycopg.connect(self.url) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(791382045)")
            connection.execute(schema)
            connection.execute("CREATE TABLE IF NOT EXISTS scope_fences(user_id TEXT,enterprise_id TEXT,generation BIGINT NOT NULL,PRIMARY KEY(user_id,enterprise_id))")

    @contextmanager
    def lock(self,user,enterprise,blocking=True):
        key=int(digest([user,enterprise])[:16],16)
        if key>=2**63:
            key-=2**64
        with psycopg.connect(self.url,autocommit=True) as connection:
            if blocking:
                connection.execute("SELECT pg_advisory_lock(%s)",(key,))
                acquired=True
            else:
                acquired=connection.execute("SELECT pg_try_advisory_lock(%s)",(key,)).fetchone()[0]
            token=None
            if acquired:
                generation=connection.execute("INSERT INTO scope_fences VALUES(%s,%s,1) ON CONFLICT(user_id,enterprise_id) DO UPDATE SET generation=scope_fences.generation+1 RETURNING generation",(user,enterprise)).fetchone()[0]
                token=self._fence.set((user,enterprise,generation))
            try:
                yield acquired
            finally:
                if token is not None:
                    self._fence.reset(token)
                if acquired and not connection.closed:
                    connection.execute("SELECT pg_advisory_unlock(%s)",(key,))
