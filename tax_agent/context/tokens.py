"""使用显式提供的模型tokenizer与chat template计数，不把字节数冒充token。"""
from functools import lru_cache


@lru_cache(maxsize=4)
def tokenizer(directory):
    from transformers import AutoTokenizer
    value=AutoTokenizer.from_pretrained(directory,local_files_only=True,trust_remote_code=False)
    if not value.chat_template:
        raise ValueError("精确模板计数需要tokenizer提供chat_template")
    return value


def count_tokens(messages,tools,settings,model):
    if settings.tokenizer_model!=model:
        raise ValueError("TOKENIZER_MODEL必须与实际配置模型一致")
    return len(tokenizer(settings.tokenizer_dir).apply_chat_template(
        messages,tools=tools,tokenize=True,add_generation_prompt=True))
