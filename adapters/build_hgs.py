"""Build upstream HGS-CVRP with incumbent notifications and a wall-clock limit."""

from pathlib import Path
import argparse
import re
import subprocess
import tempfile

if __package__:
    from .paths import DEPS, ROOT
else:
    from paths import DEPS, ROOT

HERE = ROOT / "adapters"
REV = "1a927955cd2861a29d978f0d359d6e647db9319c"


def main(deps):
    checkout = deps / "HGS-CVRP"
    if not checkout.is_dir():
        subprocess.run(["git", "clone", "https://github.com/vidalt/HGS-CVRP.git", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", REV], check=True)
    if subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip() != REV:
        raise ValueError(f"HGS headers must come from {REV}")
    source = checkout / "Program"
    build = deps / "hgs-build"
    build.mkdir(exist_ok=True)
    sources = []
    for name in ("Genetic", "Individual", "LocalSearch", "Params", "Population",
                 "Split", "AlgorithmParameters", "C_Interface"):
        original = subprocess.check_output(
            ["git", "-C", str(checkout), "show", f"{REV}:Program/{name}.cpp"], text=True
        )
        if name in ("Genetic", "Population"):
            original, count = re.subn(
                r"\(double\)\(clock\(\)\s*-\s*params.startTime\)\s*/\s*\(double\)CLOCKS_PER_SEC\s*<\s*params.ap.timeLimit",
                "hgs_before_deadline()", original,
            )
            if count != 1:
                raise RuntimeError(f"Upstream wall-clock hook moved in {name}")
            original = "bool hgs_before_deadline();\n" + original
        if name == "Population":
            marker = "bestSolutionOverall = indiv;"
            if original.count(marker) != 1:
                raise RuntimeError("Upstream incumbent hook moved")
            original = ('#include "Individual.h"\nvoid hgs_new_best(const Individual &);\n' +
                        original.replace(marker, marker + "\n\t\t\thgs_new_best(indiv);"))
        if name == "C_Interface":
            original = original.replace("Solution *result;", "Solution *result = nullptr;")
        target = build / f"{name}.cpp"
        target.write_text(original)
        sources.append(target)
    with tempfile.TemporaryDirectory(dir=build) as temp:
        library = Path(temp) / "libhgs_observed.so"
        subprocess.run(["c++", "-std=c++17", "-O3", "-shared", "-fPIC", "-I", str(source),
                        *map(str, sources), str(HERE / "hgs_observer.cpp"), "-o",
                        str(library)], check=True)
        library.replace(build / library.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deps", type=Path, default=DEPS)
    main(parser.parse_args().deps)
