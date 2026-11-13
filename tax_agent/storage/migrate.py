"""将停止写入的SQLite业务快照迁移到空PostgreSQL库，保留rowid顺序与恢复回执。"""
from pathlib import Path
import sqlite3

import psycopg

from tax_agent.storage.db import Database
from tax_agent.storage.postgres import CONTRACTS


def sqlite_to_postgres(source: Path):
    target=Database(source)
    if not getattr(target,"is_postgres",False):
        raise ValueError("请通过TAX_AGENT_DATABASE_URL配置目标PostgreSQL库")
    target.schema()
    counts={}
    source_uri=Path(source).resolve().as_uri()+"?mode=ro"
    with sqlite3.connect(source_uri,uri=True) as original, psycopg.connect(target.url) as destination:
        destination.execute("SELECT pg_advisory_xact_lock(791382045)")
        for table in CONTRACTS:
            if destination.execute("SELECT COUNT(*) FROM "+table).fetchone()[0]:
                raise ValueError("目标库必须为空，已有数据表："+table)
        available={r[0] for r in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table,(columns,_) in CONTRACTS.items():
            if table not in available:
                continue
            names=["rowid"]+columns
            rows=original.execute("SELECT "+",".join(names)+" FROM "+table+" ORDER BY rowid").fetchall()
            for row in rows:
                destination.execute("INSERT INTO "+table+" ("+",".join(names)+") VALUES ("+",".join(["%s"]*len(names))+")",row)
            counts[table]=len(rows)
            if rows:
                destination.execute("SELECT setval(pg_get_serial_sequence(%s,'rowid'),%s,true)",(table,max(r[0] for r in rows)))
    return {"source":str(source),"tables":counts,"status":"migrated","credentials_logged":False}
