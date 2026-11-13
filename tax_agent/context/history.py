"""带来源的提取式历史压缩；当前业务及写入授权始终另行核实。"""
import json
from tax_agent.storage.db import dumps
from tax_agent.models import BusinessError, HistorySummary


def active_sources(conn, ctx, refs):
    """按失效区间裁剪引用，保留同条消息中仍有效的独立任务原话。"""
    result=[]
    for ref in refs:
        row=conn.execute("SELECT content FROM messages WHERE message_id=? AND user_id=? AND enterprise_id=?",(ref["message_id"],ctx.user_id,ctx.enterprise_id)).fetchone()
        start=row[0].index(ref["quote"])
        end=start+len(ref["quote"])
        spans=conn.execute("SELECT start_pos,end_pos FROM invalidated_sources WHERE user_id=? AND enterprise_id=? AND message_id=? AND start_pos<? AND end_pos>? ORDER BY start_pos",(ctx.user_id,ctx.enterprise_id,ref["message_id"],end,start)).fetchall()
        cursor=start
        for left,right in spans:
            if cursor<left:
                result.append({"message_id":ref["message_id"],"quote":row[0][cursor:left]})
            cursor=max(cursor,right)
        if cursor<end:
            result.append({"message_id":ref["message_id"],"quote":row[0][cursor:end]})
    return result


def history_rows(db, ctx):
    with db.transaction() as conn:
        db.scope(ctx,conn=conn)
        epoch = db.epoch(ctx.user_id,ctx.enterprise_id,conn)
        return [dict(row) for row in conn.execute(
            "SELECT m.message_id,m.role,m.content,m.turn_id FROM messages m JOIN turns t ON t.turn_id=m.turn_id "
            "WHERE m.session_id=? AND m.user_id=? AND m.enterprise_id=? AND (m.memory_epoch=? OR m.message_id=?) "
            "AND t.rowid<=(SELECT rowid FROM turns WHERE turn_id=?) ORDER BY t.rowid,m.rowid",
            (ctx.session_id,ctx.user_id,ctx.enterprise_id,epoch,ctx.message_id,ctx.turn_id))]


def compress_history(db, ctx, rows, epoch, provider=None, mode="extractive"):
    older = rows[:-10]
    if not older:
        return None
    current = next(r["content"] for r in rows if r["message_id"]==ctx.message_id)
    grams = {current[i:i+2] for i in range(len(current)-1)}
    candidates = [r for r in older if r["role"]=="user"]
    ranked = sorted(enumerate(candidates),key=lambda x:(sum(g in x[1]["content"] for g in grams),x[0]),reverse=True)[:8]
    excerpts = [{"message_id":r["message_id"],"quote":r["content"][:320],
                 "truncated":len(r["content"])>320} for _,r in sorted(ranked)]
    summary = {"method":"source-excerpts-v1","covered_messages":len(older),
               "selected_messages":len(excerpts),"excerpts":excerpts,
               "notice":"历史用户原话摘录，仅供理解；不是当前业务真值、长期偏好或新写入授权。需要完整原文调用recall_history。"}
    if mode=="semantic" and excerpts:
        with db.transaction() as conn:
            cached=conn.execute("SELECT summary_json FROM context_summaries WHERE session_id=? AND memory_epoch=? AND boundary_id=?",(ctx.session_id,epoch,older[-1]["message_id"])).fetchone()
        if cached and json.loads(cached[0])["method"]=="semantic-v1":
            return json.loads(cached[0])
        if provider is None:
            raise ValueError("语义摘要需要配置真实模型Provider")
        tool={"type":"function","function":{"name":"history_summary","description":"带原消息证据的历史摘要","parameters":HistorySummary.model_json_schema()}}
        reply=provider.generate([{"role":"system","content":"将这些历史用户原话概括为简短背景摘要，保留未决问题和用户明确目标；每个要点有sources中的真实message_id和逐字quote支撑。不推断长期偏好，不把历史金额或用途当当前真值，不把摘要当新写入授权。仅输出history_summary工具。"},
                                 {"role":"user","content":dumps(excerpts)}],[tool],purpose="history_summary")
        calls=reply.message.get("tool_calls",[])
        if len(calls)!=1 or calls[0]["function"]["name"]!="history_summary":
            raise BusinessError("HISTORY_SUMMARY_FORMAT","模型未返回有效历史摘要","failed")
        value=HistorySummary.model_validate_json(calls[0]["function"]["arguments"])
        sources={r["message_id"]:r["quote"] for r in excerpts}
        if any(ref.message_id not in sources or ref.quote not in sources[ref.message_id] for ref in value.sources):
            raise BusinessError("HISTORY_SUMMARY_SOURCE","历史摘要引用未匹配原话","failed")
        summary.update(method="semantic-v1",summary=value.summary,sources=[r.model_dump() for r in value.sources])
    elif mode not in {"extractive","semantic"}:
        raise ValueError("HISTORY_MODE必须为extractive或semantic")
    with db.transaction(True) as conn:
        # epoch变化后物理移除旧压缩副本，审计源消息仍保留。
        conn.execute("DELETE FROM context_summaries WHERE session_id=? AND memory_epoch<>?",(ctx.session_id,epoch))
        conn.execute("INSERT OR REPLACE INTO context_summaries VALUES(?,?,?,?)",
                     (ctx.session_id,epoch,older[-1]["message_id"],dumps(summary)))
    db.event(ctx,"history_compacted",{"covered_messages":len(older),"selected_messages":len(excerpts),
             "source_bytes":len(dumps(older).encode()),"summary_bytes":len(dumps(summary).encode()),"memory_epoch":epoch})
    return summary


def recall(db, ctx, args):
    rows = history_rows(db,ctx)
    matches = [r for r in rows if r["role"]=="user" and
               (r["message_id"]==args.message_id if args.message_id else args.query in r["content"])]
    page = matches[args.cursor:args.cursor+args.limit]
    end = args.cursor+len(page)
    return {"items":page,"total_count":len(matches),"returned_count":len(page),
            "next_cursor":end if end<len(matches) else None}
