"""Install the pinned classical solver environments, separately timing each phase."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic

if __package__:
    from .build_hgs import REV
    from .paths import DEPS
else:
    from build_hgs import REV
    from paths import DEPS


def prepare_hgs(root, measurements):
    checkout = root / "HGS-CVRP"
    if not checkout.is_dir():
        start = monotonic()
        subprocess.run(["git", "clone", "https://github.com/vidalt/HGS-CVRP.git", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", REV], check=True)
        measurements["hgs_clone_s"] = monotonic() - start
    else:
        measurements["hgs_clone_s"] = None
    start = monotonic()
    subprocess.run([sys.executable, str(Path(__file__).with_name("build_hgs.py")),
                    "--deps", str(root)], check=True)
    measurements["hgs_compile_s"] = monotonic() - start


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-root", type=Path, default=DEPS)
    options = parser.parse_args()
    root = options.env_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    measurements = {}

    def timed(key, command):
        start = monotonic()
        subprocess.run(command, check=True)
        measurements[key] = monotonic() - start

    for name, pkg in (("ortools", "ortools==9.15.6755"),
                      ("pyvrp-ils", "pyvrp==0.14.0"), ("pyvrp-hgs", "pyvrp==0.9.0")):
        env = root / name
        timed(name + "_venv_s", [sys.executable, "-m", "venv", str(env)])
        pip = [str(env / "bin/python"), "-m", "pip", "--disable-pip-version-check"]
        wheels = root / "wheels" / name
        requirements = [pkg, "numpy==2.4.6", "pyarrow==26.0.1"]
        timed(name + "_download_s", [*pip, "download", "--no-cache-dir", "--only-binary=:all:",
                                     "--dest", str(wheels), *requirements])
        timed(name + "_install_s", [*pip, "install", "--no-index", "--find-links", str(wheels),
                                    *requirements])

    prepare_hgs(root, measurements)
    (root / "preparation-times.json").write_text(json.dumps(measurements, indent=2) + "\n")


if __name__ == "__main__":
    main()
