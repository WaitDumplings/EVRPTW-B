"""Prepare pinned CPU EV heuristics. Run with the release reader's Python 3.11."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if __package__:
    from .paths import EV_DEPS, ROOT
else:
    from paths import EV_DEPS, ROOT
GROUP_SHA = "09ffe9dba09f9c21c70be3590e721b0a605ffec4"
BENCH_SHA = "bd94474c551b39ac90e98a25a2cc43cb919f81b8"
HMA_SHA = "0ebbcaa4c2b8385c2c9bff06d60689cb2c64229d"
RB_SHA = "2d2ee06c79b5d458b1aeeafd95a2b42b5441079a"
PYBIND_SHA = "d2ac3f5c70c76c367a8af6592fc4ac2eb325b253"
BUILD_PACKAGES = (
    "scikit-build-core==0.7.1", "cmake==3.31.10", "ninja==1.13.2",
    "pyproject-metadata==0.8.1", "pathspec==1.1.1", "packaging==26.3",
)
RUNTIME_PACKAGES = (
    "tqdm==4.70.1", "pydantic==2.14.4", "click==8.5.1", "pydantic-core==2.50.5",
    "annotated-types==0.8.0", "typing-inspection==0.4.5", "typing-extensions==4.16.0",
)


def fetch(folder, remote, rev, subdirs, *, override=None):
    if not (folder / ".git").exists():
        subprocess.run(["git", "init", "-q", str(folder)], check=True)
        subprocess.run(["git", "-C", str(folder), "remote", "add", "origin", override or remote], check=True)
    elif override:
        subprocess.run(["git", "-C", str(folder), "remote", "set-url", "origin", override], check=True)
    git = ["git", "-C", str(folder)]
    subprocess.run(git + ["fetch", "--depth=1", "--filter=blob:none", "origin", rev], check=True)
    if subdirs:
        subprocess.run(git + ["sparse-checkout", "set", *subdirs], check=True)
    subprocess.run(git + ["checkout", "--detach", rev], check=True)
    actual = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
    if actual != rev:
        raise ValueError(f"Revision mismatch in {folder}")


def file_hash(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--deps-dir", type=Path, default=EV_DEPS)
    p.add_argument("--build-jobs", type=int, default=6)
    args = p.parse_args()
    root = args.deps_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    bridge = root / "benchmark" / "Other_benchmark"
    build_env = root / "build-env"
    vpy = build_env / "bin" / "python"
    site = root / "python"
    wheelhouse = root / "wheelhouse"
    pybind = root / "pybind11"
    hma_src = bridge / "vendor" / "HMA" / "src"
    provenance = dict(python=sys.version, interpreter=sys.executable,
                      compiler=subprocess.check_output(["g++", "--version"], text=True).splitlines()[0],
                      group_commit=GROUP_SHA, bridge_commit=BENCH_SHA, routingblocks_upstream=RB_SHA,
                      hma_upstream=HMA_SHA, pybind11_commit=PYBIND_SHA, build_jobs=args.build_jobs, phases_s={},
                      dependency_cache="network fetch; pip may reuse its local cache",
                      notes="Monotonic wall times of this invocation; no solve or replay is included.")
    report = root / "preparation.json"
    def phase(name, fn):
        start = time.monotonic()
        try:
            fn()
        finally:
            provenance["phases_s"][name] = time.monotonic() - start
            report.write_text(json.dumps(provenance, indent=2) + "\n")
    def call(*args, env=None):
        subprocess.run(list(map(str, args)), cwd=root, env=env, check=True)

    phase("obtain_group_vns", lambda: fetch(root / "group",
          "git@github.com:WaitDumplings/EVRPTW_DB_ICLR.git", GROUP_SHA,
          ["EVRPTW_Core", "EVRPTW_Benchmark/MetaHeuristics", "EVRPTW_Benchmark/Exact",
           "EVRPTW_Dataset_Generator/src", "src", "requirements"], override=os.environ.get("EVB_GROUP_REPO_URL")))
    phase("obtain_road_ports_and_vendored_sources", lambda: fetch(root / "benchmark",
          "https://github.com/WaitDumplings/Benchmark.git", BENCH_SHA, ["Other_benchmark"],
          override=os.environ.get("EVB_ROAD_REPO_URL")))
    phase("obtain_pybind11_sources", lambda: fetch(pybind,
          "https://github.com/pybind/pybind11.git", PYBIND_SHA, []))
    phase("create_build_env", lambda: call(sys.executable, "-m", "venv", build_env))
    pip = [vpy, "-m", "pip", "--disable-pip-version-check"]
    offline = ["--no-index", "--find-links", wheelhouse]
    def get_wheels(packages):
        call(*pip, "download", "--only-binary=:all:", "--dest", wheelhouse, *packages)
    phase("download_build_wheels", lambda: get_wheels(BUILD_PACKAGES))
    phase("download_runtime_wheels", lambda: get_wheels(RUNTIME_PACKAGES))
    phase("install_build_dependencies_offline", lambda: call(*pip, "install", *offline, *BUILD_PACKAGES))
    phase("install_runtime_dependencies_offline", lambda: call(*pip, "install", *offline,
          "--target", site, "--upgrade", *RUNTIME_PACKAGES))
    phase("compile_hma", lambda: call("bash", bridge / "hma_adapter/build.sh"))
    phase("compile_hma_resource_probe", lambda: call("g++", "-std=c++11", "-O2", "-I" + str(hma_src),
          ROOT / "adapters/hma_charge_probe.cpp", hma_src / "data.cpp", hma_src / "util.cpp",
          hma_src / "reference_runtime.cpp", "-o", root / "hma_probe"))
    env = dict(os.environ, PATH=str(build_env / "bin") + ":" + os.environ["PATH"],
               CMAKE_BUILD_PARALLEL_LEVEL=str(args.build_jobs))
    phase("compile_routingblocks_wheel", lambda: call(*pip, "wheel", "--no-index", "--no-build-isolation",
          "--no-deps", bridge / "vendor/RoutingBlocks", "-w", root / "wheels",
          "-Ccmake.define.routingblocks_BUILD_TESTS=OFF", "-Ccmake.define.CPM_pybind11_SOURCE=" + str(pybind), "-Cbuild-dir=" + str(root / "rb-build"), env=env))
    wheel, = (root / "wheels").glob("routingblocks-0.2.1-*.whl")
    phase("install_routingblocks_wheel_offline", lambda: call(*pip, "install", "--no-index", "--upgrade",
          "--target", site, "--no-deps", wheel))
    provenance["downloaded_wheels_sha256"] = {p.name: file_hash(p) for p in sorted(wheelhouse.glob("*.whl"))}
    provenance["artifacts_sha256"] = {str(path.relative_to(root)): file_hash(path)
        for path in (wheel, bridge / "hma_adapter/bin/hma_reference", root / "hma_probe")}
    report.write_text(json.dumps(provenance, indent=2) + "\n")


if __name__ == "__main__":
    main()
