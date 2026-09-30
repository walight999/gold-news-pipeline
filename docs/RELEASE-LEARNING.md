# Release learning: how assets react to economic releases

Offline research code lives in `research/`. The only thing the live bot reads is
`config/release_stats.json`, which `.github/workflows/release_learning.yml`
refreshes every Saturday.

## Data
- **Releases:** ForexFactory month pages, 2019-01 → now (`research/ff_history.py`).
  Every page embeds JSON with a UTC epoch per event, so there is no timezone guessing.
  The dataset keeps USD high+medium releases plus high-impact releases from the other majors:
  5,219 releases with numeric actual and forecast.
- **Prices:** 1-minute bars for XAU, EURUSD, USDJPY, GBPUSD and US500 (`research/prices.py`).
  - The history comes from HistData.
  - The weekly refresh uses Dukascopy (`research/prices_duka.py`).
  - **HistData caveat.** HistData documents its stamps as fixed EST, but during EU summer
    time they run 60 min late (measured in every year 2019–2026, with zero error
    once corrected). `parse_m1` corrects this. Before the fix, half of every
    year's reactions were read an hour off the print, which dragged every
    accuracy number toward 50%.
- **Dataset** (`research/dataset.csv`, committed):
  - Reference price p0 is the last price before the print.
  - Moves are measured at +5, +15 and +60 min after the print, plus the drift from T-60 and T-15 into it.

## Model (`research/learn.py`)
1. **Surprise size.** Robust z = (actual − forecast) / (1.4826·MAD of that series).
   A plain SD is unusable here, because the 2020 NFP and claims outliers inflate it.
2. **Direction prior.** The hawkish sign comes from the live rules in `src/calendar.py`.
   The logic has to be right first.
3. **One market event per minute.** Everything that prints in the same minute is combined:
   C = Σ w·sign·z. The per-series weight w is learned, shrunk toward 50%, and weighted
   toward recent years (half-life 2 years).
4. **Confidence gate.** A call is made only when |C| clears the threshold whose training
   precision is ≥ 85% with n ≥ 20. The gate switches off when the last 180 days fall
   below 75%.
5. **Grading.** Moves below 0.10% (XAU, US500) or 0.05% (FX) count as flat: neither right
   nor wrong. This is the same rule the live scorecard uses.

## Honest results: walk-forward, out-of-sample 2021-01 → 2026-09
The model is refit at the start of every month on everything before it, then
tested on that month.

| | gated calls: precision (right/graded) | coverage | all calls | current hand rules |
|---|---|---|---|---|
| XAU 5m  | **90.4%** (75/83) | 7.7% | 78.8% | 78.4% |
| XAU 15m | 71% (5/7) | 0.6% | 70.5% | 68.7% |
| XAU 60m | none (no gated calls) | 0% | 61.3% | 60.5% |
| USDJPY 5m / 15m | **93.7%** (104/111) / **88.3%** (83/94) | 10% / 8% | 82.7% / 74.6% | – |
| EURUSD 5m | **98.4%** (60/61) | 6% | 81.5% | – |

- **The hand rules are right.** The learned direction for every asset matches them
  (hawkish US data → gold, EURUSD, GBPUSD and SPX down; USDJPY up). The learned model
  adds about 0–2 pp over the rules on "all calls". The real gain comes from the gate,
  which knows when to stay quiet.
- **Only XAU at 5 minutes reliably reaches 85% or more.** At 15 and 60 minutes, other
  flows swamp the release, so gold goes to 🟡 (no call) almost always.
- **XAU 5m by year:**

  | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
  |---|---|---|---|---|---|
  | 7/8 | 11/12 | 11/11 | 16/17 | 22/23 | **8/12** |

  The 2026 misses were minor series (PPI, ISM, JOLTS, Philly Fed) in Jan–Feb.
  There have been no misses since March. The large NFP and CPI calls were all right
  (e.g. 2026-09-04 NFP: call ↓, gold −1.7%).
- **Before the print: there is no signal.** Moves into a release follow the consensus
  change (forecast − previous) 47–55% of the time for every asset. The T-15 card
  therefore shows a two-scenario map, not a call.

## On the cards
- **T-15 and Released:** 📈 "ย้อนหลัง 2019–2026 (n): ทองไปตามทิศนี้ X% ใน 5 นาที · Y% ใน 15 นาที".
  This is per-series history, in-sample and labelled as such, and it is hidden when n < 12.
- **Released only:** 🎯 "สัญญาณรวมผ่านเกณฑ์: ทอง ↓ 5 นาที (แม่นย้อนหลัง 90%, n=83) …".
  It appears only when the gate passes, and it quotes the out-of-sample precision.
- **Caveat:** calendar_check runs every 10 min, so the card often arrives after the
  5-minute reaction has happened. It tells the trader what the release meant; it is
  not a lead.

## Re-running
```
python -m research.ff_history --start 2019-01      # ~2 min, caches pages
python -m research.dataset                         # downloads HistData zips once
python -m research.learn                           # ~2 min; writes release_stats.json
python -m research.refresh                         # what CI runs weekly
```
Tunables in learn.py: HALF_LIFE_Y (env LEARN_HALF_LIFE_Y), drift window
(LEARN_DRIFT_DAYS), GATE_PREC, FLAT.

## Non-USD releases (2026-09-30)
Hawkish EUR/GBP/CAD/AUD/CHF surprises move **gold** at coin-flip odds
(44–50% in-sample; walk-forward 2023+ out-of-sample 30–55% for every
currency, whichever direction the rule takes). The currency's **own USD
pair** does react: EURUSD 86–88%, GBPUSD 79–84% out of sample.

Consequences on the cards (`src/calendar.py::NON_USD_GOLD_RATIONALE`):
- XAU pill on a non-USD release = 🟡 neutral (no directional gold call, not graded).
- ECU / counter pills keep their direction (that is what the data supports).
- History line for EUR/GBP/JPY series shows their pair
  (`config/release_stats.json → pair_series`, `research/learn.py::pair_series_table`),
  e.g. "GBPUSD ไปตามทิศนี้ 90% ใน 5 นาที". Shown only with n ≥ 12.
