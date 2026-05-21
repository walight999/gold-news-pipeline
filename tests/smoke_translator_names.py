"""Live smoke test — requires ANTHROPIC_API_KEY in env (.env auto-loaded).

Verifies that the explicit name + term glossary in the Claude prompt actually
produces the canonical Thai names (Trump → ทรัมป์, Xi → สี จิ้นผิง, Putin →
ปูติน) on real-world finance headlines.

Run:  python tests/smoke_translator_names.py
"""
from __future__ import annotations

import os
import pathlib
import sys

# Load .env so ANTHROPIC_API_KEY is available without manual export.
env = pathlib.Path(__file__).resolve().parents[1] / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.translator import to_thai  # noqa: E402

CASES = [
    ("Trump says new tariffs on China imports will start next month",
     ["ทรัมป์", "จีน"]),
    ("Xi Jinping pledges stimulus to revive China economy",
     ["สี จิ้นผิง", "จีน"]),
    ("Putin signals openness to ceasefire as gold rallies on safe-haven bid",
     ["ปูติน", "หยุดยิง"]),
    ("Powell signals patient stance, opens door to rate cuts later this year",
     ["พาวเวลล์", "ดอกเบี้ย"]),
    ("Lagarde says ECB will stay data dependent on inflation path",
     ["ลาการ์ด", "เงินเฟ้อ"]),
    ("BoJ governor Ueda warns yen weakness drives import-price inflation",
     ["อูเอดะ", "เงินเฟ้อ"]),
    ("Netanyahu rejects US ceasefire proposal, Middle East tensions escalate",
     ["เนทันยาฮู", "หยุดยิง"]),
    ("Treasury yields jump as sticky inflation pushes back rate-cut bets",
     ["อัตราผลตอบแทน", "เงินเฟ้อ"]),
    ("Modi visit to Saudi Arabia targets oil and trade deals",
     ["โมดี", "ซาอุดิอาระเบีย"]),
    ("Zelensky urges allies for more aid as Russia escalates strikes",
     ["เซเลนสกี", "รัสเซีย"]),
]

def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("FAIL: ANTHROPIC_API_KEY not set — cannot verify Claude prompt")
        return 1

    passed = failed = 0
    for src, expected in CASES:
        out = to_thai(src)
        ok = out is not None and all(needle in out for needle in expected)
        tag = "PASS" if ok else "FAIL"
        if ok: passed += 1
        else:  failed += 1
        print(f"{tag}  EN: {src}")
        print(f"      TH: {out}")
        if not ok:
            missing = [n for n in expected if not out or n not in out]
            print(f"      missing: {missing}")
        print()

    print(f"--- {passed}/{passed+failed} pass ---")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
