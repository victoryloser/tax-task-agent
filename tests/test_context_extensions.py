import json

import pytest
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from transformers import PreTrainedTokenizerFast

from conftest import message
from test_runtime import call
from tax_agent.config import Settings
from tax_agent.context.builder import build_messages
from tax_agent.context.tokens import count_tokens
from tax_agent.llm.provider import ModelReply, RecordedProvider
from tax_agent.models import BusinessError


class Summarizer:
    model="summary-fixture"
    mode="mock"
    calls=0
    forged=False
    def generate(self,messages,tools,**kwargs):
        self.calls+=1
        sources=json.loads(messages[-1]["content"])
        chosen=sources[0]
        return ModelReply(call("history_summary",{"summary":"用户此前提出一个待查背景问题。",
            "sources":[{"message_id":"invented" if self.forged else chosen["message_id"],"quote":chosen["quote"]}]}),self.model,{})


def test_semantic_summary_has_sources_is_cached_and_invalidated(db):
    session=db.new_session("U001","C001")
    for i in range(20): message(db,f"历史查询{i}",session=session)
    ctx=message(db,"总结此前背景",session=session)
    provider=Summarizer()
    messages,_=build_messages(db,ctx,provider,"semantic")
    core=json.loads(messages[0]["content"].split("可信当前状态：\n")[1])
    assert core["history_summary"]["method"]=="semantic-v1"
    assert core["history_summary"]["sources"] and provider.calls==1
    build_messages(db,ctx,provider,"semantic")
    assert provider.calls==1
    with db.transaction(True) as conn:
        conn.execute("UPDATE memory_scopes SET epoch=epoch+1")
        conn.execute("DELETE FROM context_summaries")
    after=message(db,"删除后的新请求",session=session)
    messages,_=build_messages(db,after,provider,"semantic")
    assert "待查背景" not in str(messages) and provider.calls==1


def test_semantic_summary_rejects_forged_source(db):
    session=db.new_session("U001","C001")
    for i in range(12): message(db,str(i),session=session)
    ctx=message(db,"查询",session=session)
    provider=Summarizer()
    provider.forged=True
    with pytest.raises(BusinessError,match="引用"):
        build_messages(db,ctx,provider,"semantic")


def test_tokenizer_template_count_and_budget_without_network(db,ctx,tmp_path):
    backend=Tokenizer(WordLevel({"[UNK]":0,"hello":1,"user":2,"assistant":3},unk_token="[UNK]"))
    backend.pre_tokenizer=Whitespace()
    tokenizer=PreTrainedTokenizerFast(tokenizer_object=backend,unk_token="[UNK]")
    tokenizer.chat_template="{% for message in messages %}{{ message['role'] }} {{ message['content'] }} {% endfor %}{{ tools | tojson }} assistant"
    tokenizer.save_pretrained(tmp_path)
    messages=[{"role":"user","content":"hello hello"}]
    tools=[]
    settings=Settings(tokenizer_dir=str(tmp_path),tokenizer_model="summary-fixture",context_tokens=1)
    actual=count_tokens(messages,tools,settings,"summary-fixture")
    rendered=tokenizer.apply_chat_template(messages,tools=tools,tokenize=False,add_generation_prompt=True)
    assert actual==len(tokenizer.encode(rendered,add_special_tokens=False))
    assert actual>1
    with pytest.raises(ValueError,match="模型一致"):
        count_tokens(messages,tools,settings,"wrong-model")
    with pytest.raises(BusinessError,match="Token"):
        RecordedProvider(Summarizer(),db,ctx,settings).generate(messages,tools)
