"""Build original LKH for CVRP and the travel-time-patched LKH for VRPTW."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen

if __package__:
    from .paths import DEPS
else:
    from paths import DEPS


VERSION = "3.0.10"
PACKAGE = f"LKH-{VERSION}"
ARCHIVE_SHA256 = "c7432409e359f4b712983be8670b351b20875de1be93d7b1a074c45e1744f910"
AUTHOR_URL = f"http://akira.ruc.dk/~keld/research/LKH-3/{PACKAGE}.tgz"
ARCHIVED_URL = f"https://web.archive.org/web/20251223012825id_/{AUTHOR_URL}"
PATCH = Path(__file__).resolve().parent / "patches/lkh-3.0.10-vrptw-time.patch"
BINARIES = ("LKH", "LKH-vrptw-time")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def measure(times, label):
    start = time.monotonic()
    try:
        yield
    finally:
        times[label] = time.monotonic() - start


def download(archive):
    for url in (AUTHOR_URL, ARCHIVED_URL):
        try:
            with urlopen(url, timeout=20) as source, archive.open("wb") as output:
                shutil.copyfileobj(source, output)
        except (URLError, TimeoutError, ConnectionError, IncompleteRead) as error:
            print(f"Download failed for {url}: {error}", flush=True)
            if url == ARCHIVED_URL:
                raise
        else:
            return url


def verify_archive(archive):
    actual = sha256(archive)
    if actual != ARCHIVE_SHA256:
        raise ValueError(f"{archive}: expected {ARCHIVE_SHA256}, got {actual}")


def build_binaries(work, archive, patch, make, target, times):
    # Keep the pristine and patched sources outside the checkout. Compile both
    # with the author's makefile and the same compiler and default flags.
    log = work / "build.log"
    env = os.environ.copy()
    for key in ("MAKEFLAGS", "GNUMAKEFLAGS", "MFLAGS", "MAKEFILES", "MAKEOVERRIDES"):
        env.pop(key, None)
    for label, name in zip(("original", "vrptw"), BINARIES):
        tree = work / label
        with measure(times, f"extract_{label}"), tarfile.open(archive) as tgz:
            tgz.extractall(tree, filter="data")
        try:
            with log.open("ab") as output:
                if label == "vrptw":
                    with measure(times, "apply_patch"):
                        subprocess.run(["patch", "--batch", "--forward", "--fuzz=0", "-p1"],
                                       cwd=tree / PACKAGE, input=patch, stdout=output,
                                       stderr=subprocess.STDOUT, check=True)
                with measure(times, f"compile_{label}"):
                    subprocess.run(make, cwd=tree / PACKAGE, stdout=output,
                                   stderr=subprocess.STDOUT, env=env, check=True)
        except subprocess.CalledProcessError as error:
            failure_log = target / f"failed-{work.name}.log"
            shutil.copy2(log, failure_log)
            raise RuntimeError(f"{error}; see {failure_log}") from error
        shutil.copy2(tree / PACKAGE / "LKH", work / name)
    return log


def prepare(args):
    target = args.deps_dir.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    # Publish the two binaries and their record together. An interrupted build
    # leaves the previous bin symlink and its immutable generation usable.
    with (target / ".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="evb-lkh-") as tmp:
            work = Path(tmp)
            times = {}
            with measure(times, "read_patch"):
                patch = PATCH.read_bytes()
                patch_hash = hashlib.sha256(patch).hexdigest()
            compiler = shutil.which("cc")
            if compiler is None:
                raise FileNotFoundError("cc is required to build LKH")
            compiler_version = subprocess.check_output([compiler, "--version"], text=True).splitlines()[0]
            inputs = dict(version=VERSION, archive_sha256=ARCHIVE_SHA256,
                          patch_sha256=patch_hash, script_sha256=sha256(Path(__file__)),
                          compiler=compiler, compiler_version=compiler_version)
            bin_dir = target / "bin"
            if bin_dir.exists() and not bin_dir.is_symlink():
                raise FileExistsError(f"Refusing to replace existing directory {bin_dir}")
            # Snapshot a supplied archive into the private build directory
            # before hashing it so extraction always uses the verified bytes.
            archive = work / f"{PACKAGE}.tgz"
            if args.archive:
                with measure(times, "copy_archive"):
                    shutil.copyfile(args.archive, archive)
                with measure(times, "verify_archive"):
                    verify_archive(archive)
            record = bin_dir / "preparation.json"
            if record.is_file():
                previous = json.loads(record.read_text())
                if previous["inputs"] == inputs:
                    for name in BINARIES:
                        executable = bin_dir / name
                        if (not os.access(executable, os.X_OK)
                                or sha256(executable) != previous["binaries_sha256"][name]):
                            raise ValueError(f"Installed {name} failed verification")
                    print(f"Verified existing build: {record}")
                    return
            if args.archive:
                source = str(args.archive.resolve())
            else:
                with measure(times, "download_archive"):
                    source = download(archive)
                with measure(times, "verify_archive"):
                    verify_archive(archive)
            make = ["make", "-C", "SRC", f"-j{args.build_jobs}", f"CC={compiler}", "LKH"]
            log = build_binaries(work, archive, patch, make, target, times)
            with tempfile.TemporaryDirectory(dir=target, prefix=".install-") as staging:
                stage = Path(staging)
                generation = stage / "bin"
                generation.mkdir()
                with measure(times, "install_and_hash"):
                    hashes = {}
                    for name in BINARIES:
                        shutil.copy2(work / name, generation / name)
                        hashes[name] = sha256(generation / name)
                    shutil.copy2(log, generation / "build.log")
                receipt = dict(inputs=inputs, upstream=AUTHOR_URL, archive_source=source,
                               make_command=make, binaries_sha256=hashes, seconds=times)
                (generation / "preparation.json").write_text(json.dumps(receipt, indent=2) + "\n")
                installed = target / ("build-" + stage.name.removeprefix(".install-"))
                generation.rename(installed)
                link = stage / "current"
                link.symlink_to(installed.name)
                os.replace(link, bin_dir)
            print(json.dumps(receipt, indent=2))
            print(f"Installed {bin_dir} (record: {record})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="use an existing original .tgz after verifying its hash")
    parser.add_argument("--deps-dir", type=Path, default=DEPS / f"lkh-{VERSION}",
                        help="solver installation directory (contains bin/)")
    parser.add_argument("--build-jobs", type=int, default=4)
    args = parser.parse_args()
    if args.build_jobs < 1:
        parser.error("--build-jobs must be positive")
    prepare(args)


if __name__ == "__main__":
    main()
