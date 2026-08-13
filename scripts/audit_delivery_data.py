# One-off audit: summarize months of delivery data from the state sheet.
# Read-only — never writes to any tab.
import collections
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

creds_raw = os.environ.get("GSHEET_CREDS", "").strip().strip('"').strip("'")
root = os.path.join(os.path.dirname(__file__), "..")
if creds_raw.startswith("{"):
    creds = Credentials.from_service_account_info(json.loads(creds_raw), scopes=SCOPES)
else:
    path = creds_raw if os.path.exists(creds_raw) else os.path.join(root, "creds.json")
    creds = Credentials.from_service_account_file(path, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(os.environ["GSHEET_ID"])

out = {}

def rows_of(tab):
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        return None
    vals = ws.get_all_values()
    if not vals:
        return []
    hdr = vals[0]
    return [dict(zip(hdr, r)) for r in vals[1:] if any(c.strip() for c in r)]

def month_of(ts):
    return (ts or "")[:7]

# ---- tab row counts ----
tabs = [w.title for w in sh.worksheets()]
out["tabs"] = {}
for t in tabs:
    try:
        out["tabs"][t] = sh.worksheet(t).row_count
    except Exception as e:
        out["tabs"][t] = f"err {e}"

# ---- sent_log ----
sent = rows_of("sent_log") or []
out["sent_log_total"] = len(sent)
by_month_type = collections.Counter()
by_type = collections.Counter()
status_ctr = collections.Counter()
for r in sent:
    rt = r.get("route_type", "?")
    by_type[rt] += 1
    by_month_type[(month_of(r.get("sent_ts")), rt)] += 1
    status_ctr[r.get("line_status", "?")] += 1
out["sent_by_type"] = dict(by_type)
out["sent_by_month_type"] = {f"{m}|{t}": c for (m, t), c in sorted(by_month_type.items())}
out["sent_line_status"] = dict(status_ctr)

# ---- event_state ----
ev = rows_of("event_state") or []
out["event_state_total"] = len(ev)
topic_ctr = collections.Counter(r.get("topic_bucket", "?") for r in ev)
status2 = collections.Counter(r.get("status", "?") for r in ev)
score_hist = collections.Counter()
for r in ev:
    try:
        s = float(r.get("score") or 0)
        score_hist[f"{int(s)}-{int(s)+1}"] += 1
    except ValueError:
        pass
out["event_topics"] = dict(topic_ctr.most_common(15))
out["event_status"] = dict(status2)
out["event_score_hist"] = dict(sorted(score_hist.items()))

# ---- calibration_log ----
cal = rows_of("calibration_log") or []
out["calibration_total"] = len(cal)
pred_ctr = collections.Counter()
graded = collections.Counter()
by_country = collections.Counter()
by_month_acc = collections.defaultdict(lambda: [0, 0])  # month -> [correct, wrong]
miss_titles = collections.Counter()
routed_ctr = collections.Counter(r.get("routed_as", "?") for r in cal)
FLAT = 0.10
for r in cal:
    pd_ = (r.get("predicted_dir") or "").strip().lower()
    if not pd_:
        continue
    pred_ctr[pd_] += 1
    by_country[r.get("country", "?")] += 1
    ret = r.get("xau_return_15m", "")
    try:
        ret_f = float(ret)
    except (TypeError, ValueError):
        graded["pending_or_blank"] += 1
        continue
    actual = "up" if ret_f > FLAT else ("down" if ret_f < -FLAT else "flat")
    m = month_of(r.get("first_seen_ts"))
    if pd_ in ("up", "down"):
        if actual == "flat":
            graded["flat_excluded"] += 1
        elif actual == pd_:
            graded["correct"] += 1
            by_month_acc[m][0] += 1
        else:
            graded["wrong"] += 1
            by_month_acc[m][1] += 1
            miss_titles[(r.get("title") or "")[:60]] += 1
    else:
        graded["neutral_pred"] += 1
out["cal_predicted_dir"] = dict(pred_ctr)
out["cal_graded"] = dict(graded)
out["cal_by_country"] = dict(by_country.most_common(10))
out["cal_month_accuracy"] = {
    m: {"correct": c, "wrong": w, "acc": round(c / (c + w) * 100, 1) if c + w else None}
    for m, (c, w) in sorted(by_month_acc.items())
}
out["cal_top_miss_titles"] = dict(miss_titles.most_common(12))

# ---- scorecard_daily ----
sc = rows_of("scorecard_daily") or []
out["scorecard_days"] = len(sc)
if sc:
    out["scorecard_recent"] = [
        {k: r.get(k) for k in ("date_ict", "n_correct", "n_wrong", "n_flat", "n_pending", "accuracy_pct")}
        for r in sc[-14:]
    ]

# ---- social_feed ----
sf = rows_of("social_feed") or []
out["social_feed_total"] = len(sf)
if sf:
    hdr = list(sf[0].keys())
    out["social_feed_headers"] = hdr
    appr = collections.Counter((r.get("approved") or "").strip().lower() or "(blank)" for r in sf)
    posted = sum(1 for r in sf if (r.get("posted") or "").strip())
    out["social_approved"] = dict(appr)
    out["social_posted_count"] = posted
    by_m = collections.Counter(month_of(r.get("created_at") or r.get("ts") or r.get("timestamp")) for r in sf)
    out["social_by_month"] = dict(sorted(by_m.items()))

# ---- health_log ----
hl = rows_of("health_log") or []
out["health_log_total"] = len(hl)
hw = collections.Counter((r.get("source_id", "?"), r.get("warning_type", "?")) for r in hl)
out["health_top"] = {f"{s}|{w}": c for (s, w), c in hw.most_common(12)}

# ---- source_state ----
ss = rows_of("source_state") or []
out["sources"] = {
    r.get("source_id"): {
        "errors": r.get("consecutive_errors"),
        "last_success": (r.get("last_success_ts") or "")[:10],
        "last_item": (r.get("last_item_ts") or "")[:10],
        "status": r.get("last_status"),
    }
    for r in ss
}

# ---- translation_cache ----
tc = rows_of("translation_cache") or []
out["translation_cache_rows"] = len(tc)
hits = 0
for r in tc:
    try:
        hits += int(r.get("hits") or 0)
    except ValueError:
        pass
out["translation_cache_total_hits"] = hits

print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
