"""Generate the v0.1.31 reference payloads with v0.1.31's OWN code (PERF plan W0.2 (a), P9).

    python scripts/perf/make_reference_payloads.py            # write tests/reference/payloads
    python scripts/perf/make_reference_payloads.py --check    # compare, write nothing
    python scripts/perf/make_reference_payloads.py --here --out <dir outside the repo>

What it does:

1. ``git worktree add --detach <temp dir outside the repo> 473fe5a`` (``--commit`` to change);
2. copies this checkout's ``tests/reference/{__init__,corpora,payload_gen}.py`` into that
   worktree — they use only APIs that exist at 473fe5a, and the corpora must be the same
   definitions the permanent test reads;
3. runs the generation in a CHILD ``python -c`` with ``cwd`` = the worktree and
   ``PYTHONPATH`` = the worktree (never ``-I``: the editable install would otherwise hand the
   child the main checkout's package), which first asserts that ``portfolio_dash.__file__``
   lies under the worktree;
4. writes every corpus's files plus ``manifest.json`` under ``--out``
   (default ``tests/reference/payloads``) — or, with ``--check``, compares them;
5. removes the temporary worktree, whatever happened.

``--here`` skips the worktree and generates with THIS checkout's code (the determinism
self-test: on a tip whose calculation code equals 473fe5a the output must be byte-identical).
The child runs with ``PD_DISABLE_SCHEDULER=1`` and a throwaway ``DB_PATH``, as
``tests/conftest.py::_safe_db`` sets them. Synthetic data only — no real database is read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[2])
if sys.path[:1] != [_ROOT]:
    sys.path.insert(0, _ROOT)

from scripts.perf._refcommon import REPO_ROOT, inside_repo  # noqa: E402

DEFAULT_COMMIT = "473fe5a"
DEFAULT_OUT = REPO_ROOT / "tests" / "reference" / "payloads"
#: The files of this checkout the child needs inside the reference worktree.
CARRIED = ("tests/reference/__init__.py", "tests/reference/corpora.py",
           "tests/reference/payload_gen.py")
MANIFEST = "manifest.json"

_CHILD = r"""
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
stage = Path(sys.argv[2])
names = sys.argv[3:]

import portfolio_dash

pkg = Path(portfolio_dash.__file__).resolve()
if not pkg.is_relative_to(root):
    raise SystemExit(f"portfolio_dash was imported from {pkg}, not from under {root}")

from tests.reference.corpora import CORPUS_NAMES
from tests.reference.payload_gen import corpus_info, render_corpus

templates = stage / "_templates"
infos = {}
for name in names or CORPUS_NAMES:
    files = render_corpus(name, templates, stage / "_work" / name)
    for rel, data in files.items():
        path = stage / "out" / name / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    infos[name] = corpus_info(name, templates)
    print(f"  {name}: {len(files)} files", flush=True)
(stage / "info.json").write_text(
    json.dumps({"portfolio_dash": str(pkg), "corpora": infos}, ensure_ascii=False),
    encoding="utf-8")
"""


def _git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(REPO_ROOT), *args], check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under *root*: ``{posix relative path: bytes}``."""
    if not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _manifest(source: str, infos: dict[str, dict[str, object]], out_stage: Path) -> bytes:
    corpora: dict[str, object] = {}
    for name, info in infos.items():
        files = _tree(out_stage / name)
        corpora[name] = {**info, "files": {rel: _sha256(b) for rel, b in sorted(files.items())}}
    doc = {
        "about": "v0.1.31 reference payloads (PERF plan W0.2); see README.md beside this file",
        "source": source,
        "generator": "scripts/perf/make_reference_payloads.py",
        "corpora": corpora,
    }
    return (json.dumps(doc, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def _run_child(code_root: Path, stage: Path, names: list[str], tmp: Path) -> dict[str, object]:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(code_root),
        "PD_DISABLE_SCHEDULER": "1",
        "DB_PATH": str(tmp / "safe" / "test.db"),
        "PYTHONIOENCODING": "utf-8",
    })
    subprocess.run([sys.executable, "-c", _CHILD, str(code_root), str(stage), *names],
                   cwd=code_root, env=env, check=True)
    info: dict[str, object] = json.loads((stage / "info.json").read_text(encoding="utf-8"))
    return info


def _compare(expected: dict[str, bytes], actual: dict[str, bytes]) -> list[str]:
    problems = [f"missing: {p}" for p in sorted(expected.keys() - actual.keys())]
    problems += [f"extra:   {p}" for p in sorted(actual.keys() - expected.keys())]
    problems += [f"differs: {p}" for p in sorted(expected.keys() & actual.keys())
                 if expected[p] != actual[p]]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--commit", default=DEFAULT_COMMIT,
                        help="the commit whose code generates the files (default 473fe5a)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="the payload directory (default tests/reference/payloads)")
    parser.add_argument("--corpus", action="append", default=[],
                        help="generate only this corpus (repeatable; default: all of CORPUS_NAMES)")
    parser.add_argument("--here", action="store_true",
                        help="generate with THIS checkout's code instead of a worktree")
    parser.add_argument("--check", action="store_true",
                        help="compare against --out and write nothing (exit 1 on a difference)")
    parser.add_argument("--keep-temp", action="store_true",
                        help="keep the temporary directory (the worktree is removed anyway)")
    args = parser.parse_args(argv)

    out: Path = args.out.resolve()
    tmp = Path(tempfile.mkdtemp(prefix="pd-ref-payloads-"))
    if inside_repo(tmp):
        raise SystemExit(f"the temp directory {tmp} lies inside the repository")
    worktree: Path | None = None
    try:
        if args.here:
            code_root = REPO_ROOT
            source = f"{_git('rev-parse', 'HEAD')} (--here: this checkout's code)"
        else:
            sha = _git("rev-parse", "--verify", f"{args.commit}^{{commit}}")
            worktree = tmp / f"ref-{sha[:12]}"
            _git("worktree", "add", "--detach", str(worktree), sha)
            for rel in CARRIED:
                dest = worktree / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO_ROOT / rel, dest)
            code_root = worktree
            source = sha
        print(f"generating with the code of {source} at {code_root}", flush=True)
        stage = tmp / "stage"
        info = _run_child(code_root, stage, list(args.corpus), tmp)
        infos = info["corpora"]
        assert isinstance(infos, dict)
        print(f"the child imported portfolio_dash from {info['portfolio_dash']}", flush=True)
        names = list(infos)

        generated = {f"{n}/{rel}": b for n in names for rel, b in _tree(stage / "out" / n).items()}
        manifest = _manifest(source, infos, stage / "out")

        if args.check:
            committed = {f"{n}/{rel}": b for n in names for rel, b in _tree(out / n).items()}
            problems = _compare(committed, generated)
            old = json.loads((out / MANIFEST).read_text(encoding="utf-8"))
            new = json.loads(manifest)
            for n in names:
                if old["corpora"].get(n) != new["corpora"][n]:
                    problems.append(f"manifest entry differs: {n}")
            for line in problems:
                print(line)
            print("OK: byte-identical" if not problems else f"{len(problems)} difference(s)")
            return 1 if problems else 0

        out.mkdir(parents=True, exist_ok=True)
        for n in names:
            shutil.rmtree(out / n, ignore_errors=True)
            shutil.copytree(stage / "out" / n, out / n)
        if args.corpus and (out / MANIFEST).exists():
            # A partial run keeps the other corpora's entries.
            merged = json.loads((out / MANIFEST).read_text(encoding="utf-8"))
            merged["source"] = json.loads(manifest)["source"]
            merged["corpora"].update(json.loads(manifest)["corpora"])
            manifest = (json.dumps(merged, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
        (out / MANIFEST).write_bytes(manifest)
        print(f"wrote {len(generated)} files + {MANIFEST} under {out}")
        return 0
    finally:
        if worktree is not None:
            try:
                _git("worktree", "remove", "--force", str(worktree))
            except subprocess.CalledProcessError:
                shutil.rmtree(worktree, ignore_errors=True)
                _git("worktree", "prune")
        if args.keep_temp:
            print(f"kept {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
