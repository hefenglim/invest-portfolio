"""Run a command on the deployment VM and append it to the operation log — atomically.

`docs/human_noted/vm-operation-log.md` declares an append-only audit trail of **every**
operation performed against the VM. That rule is self-imposed discipline, and discipline
has now failed twice: the log went silent for 15 released versions, and the v0.1.25 deploy
issued five remote commands but recorded one narrative summary. A rule that relies on
remembering is not a control.

So the logging is not a step you perform after the command — it is part of running it. Use
this wrapper instead of calling `gcloud compute ssh` directly and the trail cannot drift
from what actually happened.

    python scripts/vm_exec.py --action "restart demo service" --write \
        --cmd 'sudo systemctl restart <unit> && systemctl is-active <unit>'

Host identity (instance, zone, SSH host-key fingerprint) is a **real host detail** and never
lives in this repo: it is read from PD_VM_* environment variables, or from the git-ignored
`docs/human_noted/vm.json`. The script refuses to run rather than guess.

`--log-only` records an operation performed some other way (a console action, a command run
by the human), so the trail stays complete without pretending this script executed it.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess  # noqa: S404 - invoking gcloud is this script's entire purpose
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "docs" / "human_noted" / "vm-operation-log.md"
CONFIG_PATH = REPO_ROOT / "docs" / "human_noted" / "vm.json"

_TS_MARKER = "__VM_EXEC_UTC__"
_ENTRY_RE = re.compile(r"^### (\d+)\.", re.MULTILINE)

# Output is pasted into a file the human reads. Keep the head (what happened) and the tail
# (how it ended); the middle of a long dump is the least informative part.
_HEAD_CHARS = 1800
_TAIL_CHARS = 600

# Windows caps a command line near 8 KB; fail loudly rather than truncate a remote script.
_MAX_PAYLOAD = 6000

# A remote command may incidentally print a secret (an env dump, a config cat). The log is
# git-ignored, but "git-ignored" is not "safe to write secrets into" — redact at the seam.
_SECRET_RE = re.compile(
    r"(?i)^([A-Z0-9_]*(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*\s*[=:]\s*)(.+)$",
    re.MULTILINE,
)


class ConfigError(RuntimeError):
    """The VM's identity is not configured — refuse rather than guess at a host."""


@dataclass(frozen=True)
class VMConfig:
    instance: str
    zone: str
    hostkey: str

    @property
    def ssh_flags(self) -> list[str]:
        # plink, not OpenSSH: `-batch` never prompts, `-hostkey` verifies non-interactively.
        return ["--ssh-flag=-batch", f"--ssh-flag=-hostkey {self.hostkey}"]


def load_config() -> VMConfig:
    env = {k: os.environ.get(f"PD_VM_{k.upper()}") for k in ("instance", "zone", "hostkey")}
    if all(env.values()):
        return VMConfig(instance=env["instance"] or "", zone=env["zone"] or "",
                        hostkey=env["hostkey"] or "")
    if CONFIG_PATH.exists():
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        missing = [k for k in ("instance", "zone", "hostkey") if not raw.get(k)]
        if missing:
            raise ConfigError(f"{CONFIG_PATH} is missing: {', '.join(missing)}")
        return VMConfig(instance=str(raw["instance"]), zone=str(raw["zone"]),
                        hostkey=str(raw["hostkey"]))
    raise ConfigError(
        f"VM identity not configured. Set PD_VM_INSTANCE / PD_VM_ZONE / PD_VM_HOSTKEY, or "
        f"create {CONFIG_PATH} (git-ignored) with those three keys. The values are real host "
        f"details and must never be committed — see docs/human_noted/vm-host-info.md."
    )


def next_entry_number(log_text: str) -> int:
    """Entries are numbered and append-only; continue the sequence, never renumber."""
    numbers = [int(m) for m in _ENTRY_RE.findall(log_text)]
    return max(numbers) + 1 if numbers else 1


def redact(text: str) -> str:
    return _SECRET_RE.sub(r"\1<redacted>", text)


def truncate(text: str) -> str:
    if len(text) <= _HEAD_CHARS + _TAIL_CHARS:
        return text
    omitted = len(text) - _HEAD_CHARS - _TAIL_CHARS
    return f"{text[:_HEAD_CHARS]}\n… [{omitted} chars omitted] …\n{text[-_TAIL_CHARS:]}"


def render_entry(*, number: int, when: str, action: str, write: bool, command: str,
                 output: str, rc: int, seconds: float, executed: bool) -> str:
    kind = "WRITE" if write else "READ"
    body = truncate(redact(output.strip())) or "(no output)"
    # Only claim an exit code and a duration when this wrapper actually measured them —
    # a placeholder `rc=0 · 0.0s` on a hand-logged entry reads as evidence and is not.
    footer = (f"rc={rc} · {seconds:.1f}s · via scripts/vm_exec.py" if executed
              else "logged only — run outside this wrapper; rc/duration not measured")
    return (
        f"\n### {number}. {when} — {action}  *({kind})*\n\n"
        f"```\n$ {command}\n{body}\n```\n"
        f"{footer}\n"
    )


def append_entry(entry: str) -> None:
    if not LOG_PATH.exists():
        raise ConfigError(f"operation log not found: {LOG_PATH}")
    with LOG_PATH.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(entry)


def encode_remote(command: str) -> str:
    """Wrap the remote script so that only base64 characters ride on the command line.

    On Windows `gcloud` IS `gcloud.cmd`, so the argument is re-parsed by cmd.exe on its way
    through — and cmd acts on `|`, `&&`, `||` and `>` the moment an embedded double quote
    closes its quoting context. A command containing `|| echo "(unset -> default)"` was split
    there and cmd tried to run `cut` locally. Encoding leaves only [A-Za-z0-9+/=] to be
    parsed by anything other than the remote shell.

    The server-side UTC timestamp rides along inside the payload, so it costs no extra round
    trip and cannot drift from the local clock.
    """
    script = f'printf "{_TS_MARKER}%s\\n" "$(date -u +%Y-%m-%d\\ %H:%M)"\n{command}\n'
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    if len(payload) > _MAX_PAYLOAD:
        raise ConfigError(f"remote command too long ({len(payload)} b64 chars > "
                          f"{_MAX_PAYLOAD}); put it in a script on the host instead.")
    return f"echo {payload} | base64 -d | bash"


def run_remote(cfg: VMConfig, command: str) -> tuple[str, str, int, float]:
    """Return (server-UTC timestamp, combined output, exit code, wall seconds).

    The timestamp is taken on the SERVER (the log's stated convention) by prefixing the
    command, so it costs no extra round trip and cannot drift from the local clock.
    """
    # On Windows the SDK ships `gcloud.cmd`, which CreateProcess will not find from the bare
    # name — resolve it up front rather than falling back to shell=True.
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        raise ConfigError("`gcloud` is not on PATH — install the Google Cloud SDK or run "
                          "with --log-only.")
    argv = [gcloud, "compute", "ssh", cfg.instance, f"--zone={cfg.zone}", "--quiet",
            f"--command={encode_remote(command)}", *cfg.ssh_flags]
    started = time.monotonic()
    # Decode EXPLICITLY as UTF-8 with errors="replace". `text=True` alone decodes with the
    # locale codec, which on Windows is cp1252: a single byte the remote emitted that cp1252
    # cannot map (any zh-TW output, a `lsof` path, a JSON body) raised UnicodeDecodeError
    # inside subprocess's reader thread, `proc.stdout` came back None, and this function
    # crashed on `None + str` — BEFORE the log entry was written. So the one failure mode
    # this wrapper exists to prevent (an operation that ran but was never recorded) was
    # reachable through its own output handling. 2026-08-05, mid-deploy.
    proc = subprocess.run(argv, capture_output=True, check=False,  # noqa: S603
                          encoding="utf-8", errors="replace")
    elapsed = time.monotonic() - started

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    output = stdout + (f"\n[stderr]\n{stderr}" if stderr.strip() else "")
    when = "(server time unavailable)"
    for line in output.splitlines():
        if line.startswith(_TS_MARKER):
            when = f"{line[len(_TS_MARKER):].strip()} UTC"
            break
    output = "\n".join(ln for ln in output.splitlines() if not ln.startswith(_TS_MARKER))
    return when, output, proc.returncode, elapsed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--action", required=True,
                   help="short human-readable description of WHY, for the log entry")
    p.add_argument("--cmd", required=True, help="the remote shell command")
    p.add_argument("--write", action="store_true",
                   help="mark the entry WRITE (changes VM state); default is READ")
    p.add_argument("--log-only", action="store_true",
                   help="record an operation performed elsewhere instead of running it")
    p.add_argument("--result", default="",
                   help="with --log-only: the outcome to record")
    p.add_argument("--when", default="",
                   help="with --log-only: when it happened (server UTC); omit if unknown")
    args = p.parse_args(argv)

    try:
        if args.log_only:
            when, output, rc, elapsed = (args.when or "(time not recorded)"), args.result, 0, 0.0
        else:
            when, output, rc, elapsed = run_remote(load_config(), args.cmd)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    log_text = LOG_PATH.read_text(encoding="utf-8") if LOG_PATH.exists() else ""
    append_entry(render_entry(
        number=next_entry_number(log_text), when=when, action=args.action,
        write=args.write, command=args.cmd, output=output, rc=rc,
        seconds=elapsed, executed=not args.log_only))

    if not args.log_only:
        print(output)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
