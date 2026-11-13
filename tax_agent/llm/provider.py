import os
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv

from tax_agent.config import ROOT
from tax_agent.models import BusinessError
from tax_agent.storage.db import dumps


@dataclass
class ModelReply:
    message: dict
    model: str
    usage: dict


class ChatProvider:
    mode = "real"

    def __init__(self):
        load_dotenv(ROOT/".env")
        missing = [key for key in ("LLM_BASE_URL","LLM_API_KEY","LLM_MODEL") if not os.getenv(key)]
        if missing:
            raise ValueError("请配置class2/.env："+", ".join(missing))
        self.model = os.environ["LLM_MODEL"]
        self.base_url = os.environ["LLM_BASE_URL"].rstrip("/")
        self.api_key = os.environ["LLM_API_KEY"]
        self.timeout = float(os.getenv("LLM_TIMEOUT_SECONDS","60"))

    def generate(self, messages, tools, tool_choice="auto"):
        response = httpx.post(self.base_url+"/chat/completions",
            headers={"Authorization":"Bearer "+self.api_key},
            json={"model":self.model,"messages":messages,"tools":tools,"tool_choice":tool_choice,"temperature":0,"parallel_tool_calls":False},
            timeout=self.timeout)
        response.raise_for_status()
        value = response.json()
        message = value["choices"][0]["message"]
        # 只保留协议所需内容，不索取或保存供应商附带的内部推理字段。
        message = {k:v for k,v in message.items() if k in {"role","content","tool_calls"} and v is not None}
        return ModelReply(message, value["model"], value.get("usage",{}))


class RecordedProvider:
    def __init__(self, provider, db, ctx, settings):
        self.provider, self.db, self.ctx, self.settings = provider, db, ctx, settings
        self.calls = 0

    def generate(self, messages, tools, tool_choice="auto", purpose="planning"):
        if self.calls >= self.settings.max_model_calls:
            raise BusinessError("MODEL_BUDGET", "本轮已达模型调用上限，任务保留为未完成", "failed")
        size = len(dumps({"messages":messages,"tools":tools}).encode())
        tokens = None
        if self.settings.tokenizer_dir:
            from tax_agent.context.tokens import count_tokens
            tokens = count_tokens(messages,tools,self.settings,self.provider.model)
            if tokens>self.settings.context_tokens:
                raise BusinessError("CONTEXT_TOKEN_BUDGET","本轮必要上下文超过已配置tokenizer模板的Token预算","failed")
        elif size > self.settings.context_bytes:
            raise BusinessError("CONTEXT_BUDGET", "本轮必要上下文超过UTF-8字节预算，请缩小请求范围", "failed")
        self.calls += 1
        self.db.event(self.ctx,"model_input",{"messages":messages,"tools":tools,"bytes":size,"purpose":purpose,
            "configured_model":self.provider.model,"mode":self.provider.mode,"call":self.calls,"input_template_tokens":tokens})
        try:
            reply = self.provider.generate(messages,tools,tool_choice)
        except Exception as error:
            payload = {"type":type(error).__name__,"purpose":purpose}
            if isinstance(error,httpx.HTTPStatusError):
                payload["status_code"] = error.response.status_code
            self.db.event(self.ctx,"model_failed",payload)
            raise
        self.db.event(self.ctx,"model_output",{"message":reply.message,"actual_model":reply.model,"usage":reply.usage,"purpose":purpose})
        return reply
