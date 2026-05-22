"""Live smoke test for the classifier + Thai rewriter against Claude.

Run: python tests/smoke_news_alert.py

Loads .env for ANTHROPIC_API_KEY automatically. Exercises the cases that
broke production output (per user screenshots 2026-05-22):
  - Personal-finance article ('how to protect savings') → must REJECT
  - Old article (744d ago) → must REJECT
  - Equity-specific Nvidia analysis → must REJECT (no macro signal)
  - Year/BE bug: source says 2026 → output must NEVER contain 2563/2569/พ.ศ.
  - Real CPI release → must KEEP with hawkish/dovish tone + impact line
  - BoJ shouldn't be expanded to ธนาคารกลางญี่ปุ่น
"""
from __future__ import annotations

import os
import pathlib
import sys

# Force UTF-8 console output on Windows (default cp874 chokes on emoji + Thai)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

env = pathlib.Path(__file__).resolve().parents[1] / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.news_alert import classify_and_rewrite  # noqa: E402


CASES = [
    # (title, summary, age_hours, expected_action, notes)
    (
        "How to protect your savings from inflation",
        "Five tips for retirees to keep their nest egg from being eroded.",
        24,
        "reject",
        "personal finance — must reject",
    ),
    (
        "Annuity payout rates remain elevated as inflation cools",
        "What this means for your retirement income.",
        12,
        "reject",
        "personal finance — annuity",
    ),
    (
        "AI energy trade powering investment into Nvidia",
        "How the AI infrastructure buildout is driving capital into chip leaders.",
        4,
        "reject",
        "equity-specific Nvidia article — no macro signal",
    ),
    (
        "Outdated: How to invest in gold for beginners",
        "A guide for first-time gold investors.",
        24 * 30,    # 30 days old
        "reject",
        "evergreen + stale — must reject",
    ),
    (
        "US Core CPI prints 3.5% y/y vs 3.3% expected",
        "Core CPI hot for the third month, raising odds of Fed staying on hold longer.",
        0.1,
        "keep",
        "hot CPI — must keep + hawkish tone + impact line",
    ),
    (
        "Powell signals patient stance, opens door to rate cuts later this year",
        "Fed chair Powell told the Economic Club that the FOMC will be data-dependent.",
        0.5,
        "keep",
        "Fed speech — must keep + dovish tone",
    ),
    (
        "Japan Core CPI slows to 1.4% y/y vs 1.7% expected in April 2026",
        "Bank of Japan governor Ueda warned that inflation cooling reduces urgency for rate hike. Article from May 22 2026 by Eamonn Sheridan at investinglive.com.",
        0.05,
        "keep",
        "CRITICAL: year must be 2026, NOT 2563 / 2569 / พ.ศ.",
    ),
    (
        "Putin signals openness to ceasefire as gold rallies on safe-haven bid",
        "Russian president comments at SPIEF; gold up 1% on de-escalation hopes.",
        0.2,
        "keep",
        "geopolitics — must keep + risk_on tone",
    ),
]


def _has_be_year(text: str) -> bool:
    """Buddhist Era markers we want to ensure DON'T leak through."""
    if not text:
        return False
    if "พ.ศ." in text or "BE" in text.split():
        return True
    for be in ("2563", "2564", "2565", "2566", "2567", "2568", "2569", "2570"):
        if be in text:
            return True
    return False


def _has_long_thai_bank_name(text: str) -> bool:
    """Watch for long Thai institution names that should have been kept
    as short English (BoJ / Fed / ECB)."""
    if not text:
        return False
    return ("ธนาคารกลางญี่ปุ่น" in text
            or "ธนาคารกลางสหรัฐ" in text
            or "ธนาคารกลางยุโรป" in text)


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("FAIL: ANTHROPIC_API_KEY not set")
        return 1

    passed = failed = 0
    for title, summary, age_h, expected_action, note in CASES:
        out = classify_and_rewrite(title, summary, source_id="smoke",
                                    age_hours=age_h)
        ok = (out.action == expected_action)

        # Year + institution checks only matter on keeps
        be_leak = ""
        long_name = ""
        if out.action == "keep":
            joined = " ".join(filter(None, [
                out.headline_th, out.impact_th,
                *(out.body_th or []),
            ]))
            if _has_be_year(joined):
                ok = False
                be_leak = "  ❌ Buddhist Era year leaked!"
            if _has_long_thai_bank_name(joined):
                ok = False
                long_name = "  ❌ Long bank name (should be BoJ/Fed/ECB)!"

        tag = "PASS" if ok else "FAIL"
        if ok: passed += 1
        else:  failed += 1
        print(f"{tag}  [{expected_action} -> {out.action}]  {note}")
        print(f"        EN: {title}")
        if out.action == "keep":
            print(f"        TH: {out.headline_th}")
            for b in out.body_th or []:
                print(f"           • {b}")
            if out.impact_th:
                print(f"        impact: {out.impact_th}")
            print(f"        tone={out.tone}  category={out.category}  type={out.news_type}")
        elif out.action == "reject":
            print(f"        reason: {out.reason}")
        if be_leak: print(be_leak)
        if long_name: print(long_name)
        print()

    print(f"--- {passed}/{passed+failed} pass ---")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
