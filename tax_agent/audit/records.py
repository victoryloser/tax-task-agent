import json


def turn_record(db, ctx, turn_id):
    db.scope(ctx)
    with db.transaction() as conn:
        turn = conn.execute("SELECT t.* FROM turns t JOIN sessions s USING(session_id) WHERE t.turn_id=? AND s.user_id=? AND s.enterprise_id=?",(turn_id,ctx.user_id,ctx.enterprise_id)).fetchone()
        if turn is None:
            from tax_agent.models import BusinessError
            raise BusinessError("TURN_SCOPE","运行记录不属于当前用户与企业","denied")
        events = [dict(row) for row in conn.execute("SELECT * FROM audit_events WHERE turn_id=? AND user_id=? AND enterprise_id=? ORDER BY rowid",(turn_id,ctx.user_id,ctx.enterprise_id))]
        for event in events:
            event["payload"] = json.loads(event.pop("payload_json"))
        return {"turn":dict(turn),"events":events}
