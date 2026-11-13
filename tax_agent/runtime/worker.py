"""SQLite持久队列的同机多进程worker与进程监督。"""
import multiprocessing
import time

from tax_agent.config import Settings
from tax_agent.runtime.graph import Harness


def work(settings, once=False, poll_seconds=.5):
    harness = Harness(settings)
    while True:
        result = harness.run_once()
        if result:
            print(f"{result.turn_id} {result.status}",flush=True)
        elif once:
            return
        else:
            time.sleep(poll_seconds)


def serve(settings: Settings, workers=1, once=False):
    if workers < 1:
        raise ValueError("workers必须至少为1")
    context = multiprocessing.get_context("spawn")

    def start():
        process = context.Process(target=work,args=(settings,once))
        process.start()
        return process

    children = [start() for _ in range(workers)]
    try:
        while children:
            for process in list(children):
                if process.exitcode is not None:
                    children.remove(process)
                    process.join()
                    if not once or process.exitcode != 0:
                        print(f"worker {process.pid} exited {process.exitcode}; starting replacement",flush=True)
                        children.append(start())
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        for process in children:
            process.terminate()
        for process in children:
            process.join(timeout=10)
