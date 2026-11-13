"""服务入口：日志落盘，数据库未初始化时不启动业务worker。"""
import argparse
import os
from pathlib import Path
import subprocess
import socket
import sys

from tax_agent.config import ROOT, Settings
from tax_agent.runtime.locking import scope_lock
from tax_agent.storage.db import Database


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--workers",type=int,default=2)
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args()
    settings=Settings.runtime()
    db=Database(settings.db_path)
    if not db.exists:
        raise RuntimeError("请先运行tax_agent.cli init初始化业务库")
    db.schema()
    with scope_lock(db,"service",socket.gethostname(),blocking=False) as acquired:
        if not acquired:
            return
        directory=ROOT/"runs/service"
        directory.mkdir(parents=True,exist_ok=True)
        command=[sys.executable,"-m","tax_agent.cli","--db",str(settings.db_path),"worker","--workers",str(args.workers)]
        if args.once:
            command.append("--once")
        with (directory/"worker.log").open("a",encoding="utf-8") as log:
            process=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
            try:
                raise SystemExit(process.wait())
            except KeyboardInterrupt:
                process.terminate()
                process.wait(timeout=15)


if __name__=="__main__":
    main()
