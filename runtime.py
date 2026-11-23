"""Common reader, solver registry and process settings for the CPU benchmark."""
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

from adapters.paths import DEPS, ROOT

def reader_directory():
    if os.environ.get("EVRPTWD_READER"):
        return Path(os.environ["EVRPTWD_READER"]).expanduser().resolve()
    packaged = DEPS / "reader"
    adjacent = ROOT.parent / "EVRPTW-D-HF-city-v2"
    if (packaged / "instance_file.py").is_file() or not (adjacent / "instance_file.py").is_file():
        return packaged
    return adjacent


READER = reader_directory()
# Insertion order is the published acceptance order and determines its directory names.
SOLVERS = {
    "ortools": ("classical", ("cvrp", "vrptw")),
    "hgs": ("classical", ("cvrp",)),
    "pyvrp-ils": ("classical", ("cvrp", "vrptw")),
    "pyvrp-hgs": ("classical", ("vrptw",)),
    "vns-ts": ("electric", ("evrptw",)),
    "routingblocks": ("electric", ("evrptw",)),
    "hma": ("electric", ("evrptw",)),
    "gurobi": ("milp", ("cvrp", "vrptw", "evrptw")),
    "lkh": ("lkh", ("cvrp", "vrptw")),
}
MODULE = {method: f"adapters.non_learning.{module}" for method, (module, _) in SOLVERS.items()}
COMBINATIONS = [(method, view) for method, (_, views) in SOLVERS.items() for view in views]
DEFAULT_SEED = 20261103


def _gurobi_python():
    if importlib.util.find_spec("gurobipy") is not None:
        return sys.executable
    # A reader virtualenv may have been built from the licensed Conda env.
    base = Path(sys.base_prefix)
    if base.name == "maojie" and (base / "bin/python").is_file():
        return str(base / "bin/python")
    # Also support a controller launched from another env in the same Conda installation.
    conda = os.environ.get("CONDA_EXE") or shutil.which("conda")
    if conda:
        named = Path(conda).resolve().parents[1] / "envs/maojie/bin/python"
        if named.is_file():
            return str(named)
    return sys.executable


def interpreter(method):
    envkey = "EVB_PYTHON_" + method.replace("-", "_").upper()
    if envkey in os.environ:
        return os.environ[envkey]
    env = {"ortools": "ortools", "hgs": "ortools", "pyvrp-ils": "pyvrp-ils", "pyvrp-hgs": "pyvrp-hgs"}.get(method)
    if env:
        return str(DEPS / env / "bin/python")
    if method == "gurobi":
        return _gurobi_python()
    return sys.executable


def one_thread_env():
    env = os.environ.copy()
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                 "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS",
                 "RAYON_NUM_THREADS", "ARROW_IO_THREADS", "OMP_THREAD_LIMIT"):
        env[name] = "1"
    env["OMP_DYNAMIC"] = "FALSE"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def load_view(path, view):
    if not (READER / "instance_file.py").is_file():
        raise FileNotFoundError(f"EVRPTW-D reader missing in {READER}. "
                                "Follow the README setup or set EVRPTWD_READER to its checkout.")
    if str(READER) not in sys.path:
        sys.path.insert(0, str(READER))
    import pyarrow
    pyarrow.set_cpu_count(1)
    pyarrow.set_io_thread_count(1)
    from instance_file import read_instance
    return read_instance(path, view)


def file_hash(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def write_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    temp.replace(path)


def cpu_topology():
    cpus = {}
    for cpu in sorted(os.sched_getaffinity(0)):
        topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        key = tuple(int((topology / k).read_text()) for k in ("physical_package_id", "core_id"))
        cpus[cpu] = key
    return cpus
