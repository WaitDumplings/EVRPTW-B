"""Exercise the Linux process tree used by formal solvers, probes, and collectors."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
THIS = Path(__file__).resolve()


def process_tree(role, prefix):
    if role != "bench":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    if role == "orphan":
        # A double fork with an independent session also falls under the guardian.
        if os.fork():
            return
        os.setsid()
        role = "detached"
    prefix.with_suffix("." + role).write_text(str(os.getpid()))
    if role in ("native", "detached"):
        while True:
            signal.pause()
    child = "worker" if role == "bench" else "native"
    if role == "worker":
        subprocess.run([sys.executable, str(THIS), "orphan", str(prefix)], check=True)
    subprocess.run([sys.executable, str(THIS), child, str(prefix)], start_new_session=True)
    if role == "bench":
        # Killing the worker before its writer would publish a false failure.
        prefix.with_suffix(".result").write_text("worker died")


def drive(root):
    sys.path.insert(0, str(ROOT))
    from scripts.formal_processes import PanelCancelled, panel_processes
    other = subprocess.Popen(["sleep", "45"])
    try:
        with (root / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with panel_processes() as panel, ThreadPoolExecutor(2) as pool:
                    jobs = [pool.submit(panel.run, [sys.executable, str(THIS), "bench", str(root / job)],
                                        pass_fds=(lock.fileno(),), capture_output=(job == "collect"))
                            for job in ("search", "collect")]
                    for job in jobs:
                        job.result()
            except PanelCancelled as e:
                # The output lock is still held when all scheduled descendants are gone.
                def alive(marker):
                    return Path("/proc", marker.read_text()).exists()
                states = {p.name:alive(p) for p in root.iterdir() if p.suffix in
                          {".bench", ".worker", ".native", ".detached"}}
                (root / "state").write_text(json.dumps(dict(alive=states, unrelated_alive=other.poll() is None)))
                try:
                    panel.run([sys.executable, "-c", "raise RuntimeError('must not dispatch')"])
                except PanelCancelled:
                    return 128 + e.signum
                raise AssertionError("new invocation after cancellation")
    finally:
        other.terminate()
        other.wait()


class FormalCancellation(unittest.TestCase):
    def test_scheduler_signals_clean_detached_descendants_before_unlock(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with subprocess.Popen([sys.executable, str(THIS), "driver", tmp]) as driver:
                    try:
                        until = time.monotonic() + 10
                        while len(list(root.glob("*.native"))) + len(list(root.glob("*.detached"))) != 4:
                            self.assertIsNone(driver.poll())
                            self.assertLess(time.monotonic(), until)
                            time.sleep(.015)
                        with (root / "lock").open("a") as lock:
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        driver.send_signal(sig)
                        self.assertEqual(driver.wait(timeout=8), 128 + sig)
                    finally:
                        if driver.poll() is None:
                            driver.terminate()
                state = json.loads((root / "state").read_text())
                self.assertTrue(state["unrelated_alive"])
                self.assertEqual(len(state["alive"]), 8)
                self.assertFalse(any(state["alive"].values()), state)
                self.assertFalse(list(root.glob("*.result")))
                with (root / "lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == "__main__":
    if len(sys.argv) == 3:
        role, prefix = sys.argv[1:]
        if role == "driver":
            sys.exit(drive(Path(prefix)))
        process_tree(role, Path(prefix))
    else:
        unittest.main()
