from dataclasses import dataclass
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    db_path: Path = ROOT / "data/business.sqlite3"
    checkpoint_path: Path = ROOT / "data/checkpoints.sqlite3"
    max_model_calls: int = 18
    max_tool_calls: int = 16
    context_bytes: int = 48000
    page_size: int = 20
    read_retry_limit: int = 2
    history_mode: str = "extractive"
    tokenizer_dir: str | None = None
    context_tokens: int = 12000
    tokenizer_model: str | None = None

    @classmethod
    def runtime(cls,db_path=None):
        from dotenv import load_dotenv
        load_dotenv(ROOT/".env")
        path=Path(db_path or cls().db_path)
        return cls(db_path=path,checkpoint_path=path.with_name(path.stem+"-checkpoints.sqlite3"),
                   read_retry_limit=int(os.getenv("READ_RETRY_LIMIT","2")),
                   history_mode=os.getenv("HISTORY_MODE","extractive"),
                   tokenizer_dir=os.getenv("TOKENIZER_DIR"),
                   tokenizer_model=os.getenv("TOKENIZER_MODEL"),
                   context_tokens=int(os.getenv("CONTEXT_TOKENS","12000")))


def implementation_hash():
    import hashlib
    return hashlib.sha256(b"".join(p.relative_to(ROOT).as_posix().encode()+p.read_bytes()
        for p in sorted((ROOT / "tax_agent").rglob("*.py")))).hexdigest()
