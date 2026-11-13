import shutil
from pathlib import Path

import pytest

from tax_agent.models import AuthContext
from tax_agent.storage.db import Database, now, uid
from tax_agent.storage.import_data import initialize_sample
from tax_agent.config import ROOT
from tax_agent.llm.provider import ModelReply


@pytest.fixture(scope="session")
def seed(tmp_path_factory):
    if not all((ROOT / "material" / name).is_file() for name in ("02_示例数据.xlsx", "03_业务规则.docx")):
        pytest.skip("Historical sample regression: local material files are not distributed")
    path = tmp_path_factory.mktemp("seed")/"seed.sqlite3"
    initialize_sample(Database(path))
    return path


@pytest.fixture
def db(seed,tmp_path):
    path = tmp_path/"business.sqlite3"
    shutil.copyfile(seed,path)
    return Database(path)


def message(db,content,user="U001",enterprise="C001",session=None,role="user"):
    session = session or db.new_session(user,enterprise)
    session = db.session(session["session_id"],user,enterprise)
    mid, tid = uid(),uid()
    with db.transaction(True) as conn:
        conn.execute("INSERT INTO turns VALUES(?,?,?,'running',NULL,'test',?)",(tid,session["session_id"],mid,now()))
        conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",(mid,session["session_id"],user,enterprise,tid,role,content,session["memory_epoch"],now()))
    return AuthContext(user,enterprise,session["session_id"],tid,mid,session["memory_epoch"])


class AllowAuthorization:
    """仅用于确定性权限/事务测试；不当作语义理解验证。"""
    def generate(self,messages,tools,tool_choice="auto",purpose="planning"):
        return ModelReply({"role":"assistant","tool_calls":[{"id":uid(),"type":"function","function":{"name":"authorization_decision","arguments":'{"allowed":true,"reason":"unit test explicit approval"}'}}]},"test-authorization",{})


@pytest.fixture
def ctx(db):
    return message(db,"请将F002的用途保存为客户培训资料打印")


@pytest.fixture
def authorization(db):
    from tax_agent.domain.authorization import Authorization
    return Authorization(db,AllowAuthorization())
