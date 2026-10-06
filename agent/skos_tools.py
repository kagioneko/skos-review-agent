"""Security Knowledge OS as agent tools.

The agent never judges risk itself: every verdict comes from SKOS's
deterministic rules (the `mcp` and `capgraph` Update Packs). The LLM only
decides what to look at next and explains the result.

No value from the user's config reaches a tool result: SKOS reports facts
(`secrets_in_config: true`), never the secret itself, and only server names
from the config appear. SKOS reads the config from a file, so it is written
to a private (0600) temp file that is deleted as soon as the scan ends.

The work per scan is bounded: config size, number of servers, and the rule
catalogue is loaded once per pack.
"""

from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

PACKS_DIR = Path(__file__).resolve().parents[1] / "vendor" / "packs"
PACK_ZIPS = ("mcp-2026.10.0.zip", "capgraph-2026.10.0.zip")
MAX_CONFIG_CHARS = 50_000
MAX_SERVERS = 20

_home: str | None = None


def ensure_packs() -> str:
    """Install the signed packs into a private SKOS_HOME once per process.
    Installation verifies the Ed25519 signature against the key built into
    SKOS; an unsigned or tampered ZIP is refused."""
    global _home
    if _home is not None:
        return _home
    from app.config import Settings
    from app.packs.store import install
    from app.reviewer.rule_loader import load_rules

    home = tempfile.mkdtemp(prefix="skos-home-")
    os.environ["SKOS_HOME"] = home
    core = load_rules(Settings.from_env().rules_root)
    for name in PACK_ZIPS:
        install(PACKS_DIR / name, core, home=Path(home))
    _home = home
    return home


@lru_cache(maxsize=4)
def _catalogue(pack_id: str) -> Any:
    from app.config import Settings
    from app.packs.loader import load_with_packs, only_pack
    from app.reviewer.rule_loader import load_rules

    ensure_packs()
    merged, _ = load_with_packs(load_rules(Settings.from_env().rules_root))
    return only_pack(merged, pack_id)


def _assess(extensions: dict[str, Any], pack_id: str, name: str) -> dict[str, Any]:
    from app.config import Settings
    from app.models.assessment import AssessmentInput
    from app.reviewer.report import build_report

    report = build_report(
        AssessmentInput.model_validate({"name": name, "extensions": {pack_id: extensions}}),
        _catalogue(pack_id),
        settings=Settings.from_env(),
    )
    if report.result is None:
        return {"status": report.status.value}
    prefix = f"{pack_id.upper()}-"
    findings = [
        {"rule": f.risk_id, "status": f.status.value, "title": f.title}
        for f in report.result.findings
        if f.risk_id.startswith(prefix) and f.status.value != "N/A"
    ]
    return {
        "overall": report.result.overall_status.value,
        "findings": findings,
        "questions": [
            {"field": q.field, "text": q.text} for q in report.result.questions
        ][:10],
    }


def scan(config_text: str, client: str = "none") -> dict[str, Any]:
    """Scan an MCP client config (JSON text) and assess every server (mcp
    pack) and the whole agent (capgraph pack)."""
    from app.adapters.capgraph import agent_labels
    from app.adapters.mcp_config import ConfigError, scan_config

    if len(config_text) > MAX_CONFIG_CHARS:
        return {"error": "config too large"}
    with tempfile.TemporaryDirectory(prefix="skos-scan-") as tmp:
        path = Path(tmp) / "mcp.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(config_text)
        try:
            scans = scan_config(path)
        except ConfigError as exc:
            return {"error": f"invalid MCP config: {exc}"}  # messages carry no values
    if len(scans) > MAX_SERVERS:
        return {"error": f"too many servers: this demo reviews up to {MAX_SERVERS}"}
    agent = agent_labels(scans, client if client in ("claude-code", "none") else None)
    servers = []
    for s in scans:
        servers.append({
            "server": s.server,
            "facts": s.facts,
            "unknown": s.unknown(),
            "assessment": _assess(s.facts, "mcp", f"mcp:{s.name}"),
        })
    return {
        "servers": servers,
        "agent": {
            "labels": agent.labels,
            "contributors": agent.contributors,
            "facts": agent.facts(),
            "assessment": _assess(agent.facts(), "capgraph", "agent"),
        },
    }


def reassess(pack: str, facts: dict[str, Any]) -> dict[str, Any]:
    """Re-run one assessment with answered facts (keys without the pack
    prefix, e.g. {"flow_gated": true}). Unknown keys are rejected by SKOS."""
    if pack not in ("mcp", "capgraph"):
        return {"error": "pack must be 'mcp' or 'capgraph'"}
    try:
        return _assess(facts, pack, f"{pack}:reassess")
    except (ValueError, TypeError, KeyError) as exc:  # pydantic errors are ValueErrors
        return {"error": type(exc).__name__}  # the type only: no values echoed
