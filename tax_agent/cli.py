import argparse
import json
from pathlib import Path

from tax_agent.config import Settings
from tax_agent.domain.rules import Rules
from tax_agent.models import AuthContext, RuleDraft
from tax_agent.runtime.graph import Harness
from tax_agent.storage.db import Database, dumps
from tax_agent.storage.import_data import initialize, import_workbook
from tax_agent.audit.records import turn_record


def main():
    parser = argparse.ArgumentParser(description="财税任务服务")
    parser.add_argument("--db",type=Path,default=Settings().db_path)
    sub = parser.add_subparsers(dest="command",required=True)
    init = sub.add_parser("init",help="初始化空库；可指定 Excel，拒绝覆盖已有数据")
    init.add_argument("--source", type=Path)
    importer = sub.add_parser("import-data", help="将 Excel 导入空业务库；拒绝覆盖")
    importer.add_argument("file", type=Path)
    sub.add_parser("migrate",help="追加运行队列/回执表，保留已有业务与记忆")
    migrate_pg=sub.add_parser("migrate-postgres",help="将停止写入的SQLite快照迁移到空PostgreSQL库")
    migrate_pg.add_argument("--source",type=Path,required=True)
    doctor=sub.add_parser("doctor",help="环境配置、依赖与工具协议检查")
    doctor.add_argument("--live",action="store_true",help="使用配置模型执行一次不修改业务的协议探针")
    doctor.add_argument("--output",type=Path)
    legacy=sub.add_parser("recover-legacy",help="核对无节点回执的早期中断记录，不重放未知写入")
    legacy.add_argument("--apply",action="store_true",help="保存核对结果并结束旧running状态")
    worker = sub.add_parser("worker",help="后台消费持久队列；同机多进程、崩溃接续")
    worker.add_argument("--workers",type=int,default=1)
    worker.add_argument("--once",action="store_true",help="处理当前可领取工作后退出")
    enqueue = sub.add_parser("enqueue",help="提交后立即返回turn_id")
    enqueue.add_argument("question")
    enqueue.add_argument("--user",default="U001")
    enqueue.add_argument("--enterprise",default="C001")
    enqueue.add_argument("--session")
    enqueue.add_argument("--turn-id")
    jobs = sub.add_parser("jobs",help="查看当前用户企业队列状态")
    jobs.add_argument("--user",default="U001")
    jobs.add_argument("--enterprise",default="C001")
    chat = sub.add_parser("chat")
    chat.add_argument("--user",default="U001")
    chat.add_argument("--enterprise",default="C001")
    chat.add_argument("--session")
    rules = sub.add_parser("rules",help="本机管理员规则管理；不在模型工具中开放")
    rules_sub = rules.add_subparsers(dest="action",required=True)
    rules_sub.add_parser("list")
    for action in ("import","update"):
        item = rules_sub.add_parser(action)
        item.add_argument("file",type=Path)
    item = rules_sub.add_parser("activate")
    item.add_argument("rule_id")
    item.add_argument("version")
    item.add_argument("--reviewed-hash",required=True)
    item.add_argument("--supersede")
    item = rules_sub.add_parser("disable")
    item.add_argument("rule_id")
    item.add_argument("version")
    item = rules_sub.add_parser("search")
    item.add_argument("--user",required=True)
    item.add_argument("--enterprise",required=True)
    item.add_argument("--month",required=True)
    item.add_argument("--query",default="")
    args = parser.parse_args()
    Settings.runtime()
    db = Database(args.db)
    if args.command == "init":
        print(dumps(initialize(db, args.source)))
    elif args.command == "import-data":
        print(dumps(import_workbook(db, args.file)))
    elif args.command == "doctor":
        from tax_agent.diagnostics import diagnose
        result=diagnose(args.live)
        rendered=json.dumps(result,ensure_ascii=False,indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(rendered,encoding="utf-8")
        print(rendered)
    elif args.command == "migrate":
        db.schema()
        print("运行表已更新，现有业务数据保留。")
    elif args.command == "migrate-postgres":
        from tax_agent.storage.migrate import sqlite_to_postgres
        print(json.dumps(sqlite_to_postgres(args.source),ensure_ascii=False,indent=2))
    elif args.command == "recover-legacy":
        from tax_agent.runtime.legacy import reconcile
        print(json.dumps(reconcile(db,args.apply),ensure_ascii=False,indent=2))
    elif args.command in {"worker","enqueue","jobs"}:
        settings = Settings.runtime(args.db)
        harness = Harness(settings)
        if args.command == "worker":
            from tax_agent.runtime.worker import serve
            serve(settings,args.workers,args.once)
        elif args.command == "enqueue":
            session = db.session(args.session,args.user,args.enterprise) if args.session else db.new_session(args.user,args.enterprise)
            turn_id = harness.submit(session["session_id"],args.user,args.enterprise,args.question,args.turn_id)
            print(dumps({"session_id":session["session_id"],"turn_id":turn_id}))
        else:
            db.authorize(args.user,args.enterprise)
            with db.transaction() as conn:
                rows = [dict(r) for r in conn.execute("SELECT t.turn_id,t.session_id,t.status,t.answer,j.attempts,j.owner,j.updated_at FROM turns t JOIN turn_jobs j USING(turn_id) JOIN sessions s USING(session_id) WHERE s.user_id=? AND s.enterprise_id=? ORDER BY t.rowid DESC",(args.user,args.enterprise))]
            print(json.dumps(rows,ensure_ascii=False,indent=2))
    elif args.command == "rules":
        manager = Rules(db)
        if args.action == "list":
            result = manager.list()
        elif args.action in {"import","update"}:
            result = manager.draft(RuleDraft.model_validate_json(args.file.read_text(encoding="utf-8")))
        elif args.action == "activate":
            result = manager.activate(args.rule_id,args.version,args.reviewed_hash,args.supersede)
        elif args.action == "disable":
            result = manager.disable(args.rule_id,args.version)
        else:
            ctx = AuthContext(args.user,args.enterprise,"admin-search","","",0)
            result = manager.search(ctx,args.month,args.query)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    else:
        settings = Settings.runtime(args.db)
        harness = Harness(settings)
        session = db.session(args.session,args.user,args.enterprise) if args.session else db.new_session(args.user,args.enterprise)
        print(f"会话：{session['session_id']}；/new新会话，/records最后一轮依据，/exit退出")
        last = None
        while (question := input("\n你> ").strip()) != "/exit":
            if question == "/new":
                session = db.new_session(args.user,args.enterprise)
                print("新会话："+session["session_id"])
                continue
            if question == "/records" and last:
                ctx = AuthContext(args.user,args.enterprise,session["session_id"],last.turn_id,"",session["memory_epoch"])
                print(json.dumps(turn_record(db,ctx,last.turn_id),ensure_ascii=False,indent=2))
                continue
            last = harness.respond(session["session_id"],args.user,args.enterprise,question)
            print(last.answer)
            print("运行记录："+last.turn_id)


if __name__ == "__main__":
    main()
