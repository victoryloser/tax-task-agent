"""模型服务接入检查；只检查协议与只读业务，不修改发票。"""
import importlib.metadata
import os

from pydantic import BaseModel, ConfigDict
from tax_agent.config import Settings, implementation_hash
from tax_agent.llm.provider import ChatProvider
from tax_agent.storage.db import Database, now


class Probe(BaseModel):
    model_config=ConfigDict(extra="forbid")
    text: str
    amount_cents: int


def diagnose(live=False):
    settings=Settings.runtime()
    db=Database(settings.db_path)
    report={"created_at":now(),"implementation_sha256":implementation_hash(),
            "profile":os.getenv("MODEL_PROFILE","development"),
            "configured_model":os.getenv("LLM_MODEL"),
            "configured_fields":{k:bool(os.getenv(k)) for k in ("LLM_BASE_URL","LLM_API_KEY","LLM_MODEL")},
            "database_backend":"postgresql" if getattr(db,"is_postgres",False) else "sqlite",
            "database_initialized":db.exists,
            "versions":{name:importlib.metadata.version(name) for name in ("langgraph","pydantic","streamlit","httpx")},
            "live_probe":None}
    if live:
        provider=ChatProvider()
        tool={"type":"function","function":{"name":"integration_probe","description":"原样返回指定文本和整数金额","parameters":Probe.model_json_schema()}}
        result=provider.generate([{"role":"system","content":"仅调用integration_probe一次，text填写“接口协议核验”，amount_cents填写12345，不添加其他字段。"}],[tool])
        calls=result.message.get("tool_calls",[])
        if len(calls)!=1 or calls[0]["function"]["name"]!="integration_probe":
            raise ValueError("模型接口未返回预期原生工具调用")
        value=Probe.model_validate_json(calls[0]["function"]["arguments"])
        if value.text!="接口协议核验" or value.amount_cents!=12345:
            raise ValueError("模型接口的中文/整数工具参数核验失败")
        report["live_probe"]={"status":"passed","actual_model":result.model,"usage":result.usage,"parameters":value.model_dump()}
    return report
