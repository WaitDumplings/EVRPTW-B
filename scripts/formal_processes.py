"""Cancel all subprocesses of a Linux formal panel, including detached solver sessions."""
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading


class PanelCancelled(Exception):
    def __init__(self, signum):
        self.signum = signum
        super().__init__(signal.Signals(signum).name)


class PanelProcesses:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.signum = None

    def cancel(self, signum, frame):
        self.signum = self.signum or signum
        # EOF is persistent, including for a guardian racing with cancellation.
        self.writer.close()

    def check_cancelled(self):
        if self.signum is not None:
            raise PanelCancelled(self.signum)

    def run(self, command, *, pass_fds=(), **kwargs):
        self.check_cancelled()
        control = self.reader.fileno()
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(control), *command],
                                start_new_session=True, pass_fds=(control, *pass_fds), **kwargs)
        self.check_cancelled()
        return result


@contextmanager
def panel_processes():
    reader, writer = socket.socketpair()
    with reader, writer:
        panel = PanelProcesses(reader, writer)
        previous = {}
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, panel.cancel)
            yield panel
            panel.check_cancelled()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def reap_descendants():
    """All remaining children belong to this one invocation, including reparented grandchildren."""
    children = Path(f"/proc/self/task/{os.getpid()}/children")
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid:
            continue
        for child in children.read_text().split():
            # No other thread reaps these children, so their PIDs cannot be reused here.
            os.kill(int(child), signal.SIGKILL)
        os.waitpid(-1, 0)


def supervise(control, command):
    # PR_SET_CHILD_SUBREAPER. setsid() changes sessions but does not escape reparenting.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    os.set_inheritable(control, False)
    cancelled = False
    process = None

    def stop_guarded_process(signum, frame):
        # A service manager can signal the guardian and the scheduler simultaneously.
        nonlocal cancelled
        cancelled = True
        if process is not None:
            process.kill()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop_guarded_process)
    if cancelled or select.select([control], [], [], 0)[0]:
        return 1
    # Only explicitly passed lease descriptors are inheritable across this exec.
    process = subprocess.Popen(command, close_fds=False)
    if cancelled:
        process.kill()

    def cancel_on_eof():
        os.read(control, 1)
        # Stop the result writer first, before a worker's signal can become a technical failure.
        process.kill()

    threading.Thread(target=cancel_on_eof, daemon=True).start()
    try:
        code = process.wait()
    finally:
        reap_descendants()
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(supervise(int(sys.argv[1]), sys.argv[2:]))
