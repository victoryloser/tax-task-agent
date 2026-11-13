"""同机进程共享的范围锁；进程退出由操作系统释放，无租约超时抢占。"""
import errno
import os
import time
from contextlib import contextmanager

from tax_agent.storage.db import digest


@contextmanager
def scope_lock(db, user, enterprise, blocking=True):
    if getattr(db,"is_postgres",False):
        with db.lock(user,enterprise,blocking) as acquired:
            yield acquired
        return
    directory = db.path.parent / (db.path.name + ".locks")
    directory.mkdir(exist_ok=True)
    path = directory / (digest([user, enterprise]) + ".lock")
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        acquired = False
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if not blocking:
                    break
                time.sleep(.1)
        try:
            yield acquired
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
