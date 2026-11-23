"""Stop the gateway process group when its standby supervisor disappears."""

import os
import select
import signal
import subprocess
import sys


CHILD_STOP_SECONDS = 1.5


def stop_group(child):
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=CHILD_STOP_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=CHILD_STOP_SECONDS)


def run(monitor, executable):
    stopping = False

    def shutdown(_signal, _frame):
        nonlocal stopping
        stopping = True

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, shutdown)
    # A supervisor may exit before this child guard has a chance to launch.
    if select.select([monitor], [], [], 0)[0]:
        return 0
    child = subprocess.Popen([executable, "gateway", "run"], start_new_session=True)
    try:
        while not stopping:
            status = child.poll()
            if status is not None:
                return status if status >= 0 else 128 - status
            if select.select([monitor], [], [], 0.2)[0]:
                break
        return 0
    finally:
        stop_group(child)


if __name__ == "__main__":
    raise SystemExit(run(int(sys.argv[1]), sys.argv[2]))
