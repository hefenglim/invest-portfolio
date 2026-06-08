"""Aggregate ProbeResults into a markdown comparison matrix + ranked recommendation."""

from collections import defaultdict

from scripts.probe.models import ProbeResult, Verdict

_ORDER = {Verdict.PRIMARY: 0, Verdict.FALLBACK: 1, Verdict.SKIPPED: 2, Verdict.UNUSABLE: 3}


def render_report(results: list[ProbeResult]) -> str:
    groups: dict[tuple[str, str], list[ProbeResult]] = defaultdict(list)
    for r in results:
        groups[(r.market, r.data_type.value)].append(r)

    lines: list[str] = ["# Data-Source Probe Results", ""]
    for (market, dtype) in sorted(groups):
        rows = sorted(groups[(market, dtype)], key=lambda r: _ORDER[r.verdict])
        lines.append(f"## {market} — {dtype}")
        usable = [r.source for r in rows if r.verdict in (Verdict.PRIMARY, Verdict.FALLBACK)]
        lines.append(f"Recommended order: {' → '.join(usable) if usable else '(none)'}")
        lines.append("")
        lines.append(
            "| source | verdict | cov | batch | latency ms | 3dp | raw+adj | hist | notes |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r.source} | {r.verdict.value} | {r.coverage_hits} | "
                f"{r.batch_max or ''} | {r.latency_ms or ''} | "
                f"{r.decimals_ok if r.decimals_ok is not None else ''} | "
                f"{r.has_raw_and_adj if r.has_raw_and_adj is not None else ''} | "
                f"{r.history_earliest or ''} | {(r.error or r.notes or '').replace('|', '/')} |"
            )
        lines.append("")
    return "\n".join(lines)
