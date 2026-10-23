"""Exact-match accuracy of the intent parser on tests/fixtures/emails.jsonl.

uv run python scripts/eval_parser.py            # rules only (never calls an API)
uv run python scripts/eval_parser.py --llm      # also rules+LLM, if LLM_ENABLED and a key is set (billed API calls)
uv run python scripts/eval_parser.py -v         # also print every miss
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from uarb_agent.intent.parser import parse
from uarb_agent.models import ParseResult

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "emails.jsonl"


def load(path: Path = DEFAULT_PATH) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def summarise(result: ParseResult, expected: dict[str, Any]) -> dict[str, Any]:
    """Project a ParseResult onto the fields the fixture pins down."""
    requests = []
    for r, exp in zip(result.requests, expected["requests"] + [{}] * len(result.requests), strict=False):
        d = {"matter": r.matter, "doc_type": r.doc_type and str(r.doc_type), "limit": r.limit, "offset": r.offset}
        if "assumed" in exp:
            d["assumed"] = bool(r.assumed_notes)
        requests.append(d)
    return {
        "requests": requests,
        "clarify": result.clarify.reason if result.clarify else None,
        "command": {"kind": str(result.command.kind), "args": result.command.args} if result.command else None,
    }


def evaluate(cases: list[dict[str, Any]], llm: Callable | None = None) -> tuple[int, list[tuple[str, dict, dict]]]:
    """Returns (correct, misses) where each miss is (id, expected, got)."""
    misses = []
    for c in cases:
        result = parse(c["subject"], c["body_text"], c.get("body_html"), c.get("thread_state"), llm=llm)
        # A few cases have a different right answer once the LLM may read what the rules leave open.
        expected = c.get("expected_llm", c["expected"]) if llm else c["expected"]
        got = summarise(result, expected)
        if got != expected:
            misses.append((c["id"], expected, got))
    return len(cases) - len(misses), misses


def report(label: str, cases: list[dict[str, Any]], llm: Callable | None, verbose: bool) -> float:
    correct, misses = evaluate(cases, llm)
    canon = [c for c in cases if "canonical" in c["tags"]]
    canon_ok, _ = evaluate(canon, llm)
    acc = correct / len(cases)
    print(f"{label}: {correct}/{len(cases)} = {acc:.1%}  (canonical {canon_ok}/{len(canon)})")
    for case_id, exp, got in misses if verbose else []:
        print(f"  MISS {case_id}\n    expected {json.dumps(exp)}\n    got      {json.dumps(got)}")
    return acc


def main(argv: list[str]) -> int:
    from uarb_agent.config import get_settings
    from uarb_agent.intent import llm_fallback

    verbose = "-v" in argv
    cases = load()
    acc = report("rules", cases, None, verbose)
    if "--llm" in argv and get_settings().llm_active:  # opt-in: each case is a billed API call
        report("rules+llm", cases, llm_fallback.guess, verbose)
    return 0 if acc >= 0.95 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
