#!/usr/bin/env python3
"""Merge carrier-direct tracking with the platform mirror into one scan-level table.

Outputs (in --out-dir, default: current directory):
    events.csv         one row per physical scan
    stats.csv          one row per waybill on the waybill list
    drops.csv          input rows that did not become events
    merge_summary.txt  merged / dropped / resends plus a one-line notes entry
"""

import argparse
import csv
import datetime as dt
import os
import re
import sys
from collections import defaultdict

# Two clocks may disagree by a minute or two; allow up to 5 minutes.  In this
# batch every genuine carrier/mirror pair differs by <= 4 minutes and the next
# candidate sits ~5.4 minutes away, so the cut is not sensitive.
MAX_SKEW_SECONDS = 300

TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$")
SEQ_RE = re.compile(r"^\d+$")

REASON_FOREIGN = "foreign_waybill"
REASON_BAD = "bad_row"

NOTES = (
    "notes 同一扫描:同waybill且event与station均相同、时间差绝对值<=300s,按时差由小到大一对一配对;"
    "重发组的任一版本都可用来认亲,但输出内容(event/station/seq/ts)一律取最后到达版本;"
    "ts记承运商时间,仅镜像行记镜像时间;seq仅镜像行留空,合并行用承运商seq;"
    "承运商重发按(waybill,seq)保留最后到达行,其余只计入resends,不列入drops;"
    "drops的reason:foreign_waybill=运单不在清单,bad_row=站点空或时间不可解析"
)


def parse_ts(raw):
    m = TS_RE.match(raw.strip())
    if not m:
        return None
    try:
        return dt.datetime(*(int(x) for x in m.groups()))
    except ValueError:
        return None


def fmt_ts(value):
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def read_rows(path, width):
    """Read a CSV with a header; pad/trim each row to `width`, strip whitespace."""
    rows = []
    with open(path, "r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # header
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue  # blank line
            cells = [cell.strip() for cell in row[:width]]
            if len(cells) < width:
                cells.extend([""] * (width - len(cells)))
            rows.append(cells)
    return rows


def load_waybills(path):
    rows = read_rows(path, 1)
    return {row[0] for row in rows if row[0]}


def merge(carrier_path, mirror_path, waybills_path):
    waybills = load_waybills(waybills_path)
    drops = []          # (src, waybill, reason)
    resends = 0

    # ---- carrier: last-arrival wins per (waybill, seq) -------------------
    # A resend carries a fresh copy of one physical scan, possibly with a
    # corrected event/station/time, so all versions stay attached to the group
    # (they identify one physical scan) while the last one supplies the values.
    groups = {}         # key -> [versions in arrival order]
    for waybill, seq, event, station, ts_raw in read_rows(carrier_path, 5):
        if waybill not in waybills:
            drops.append(("carrier", waybill, REASON_FOREIGN))
            continue
        key = (waybill, seq)
        if key in groups:
            resends += 1
            groups[key].append((event, station, ts_raw))
        else:
            groups[key] = [(event, station, ts_raw)]

    carrier_scans = {}  # key -> usable scan built from the last version
    group_versions = {} # key -> [(event, station, ts)] versions that parsed
    for key, versions in groups.items():
        parsed = []
        for event, station, ts_raw in versions:
            ts = parse_ts(ts_raw)
            if event and station and ts is not None:
                parsed.append((event, station, ts))
        event, station, ts_raw = versions[-1]
        ts = parse_ts(ts_raw)
        if not SEQ_RE.match(key[1]) or not event or not station or ts is None:
            drops.append(("carrier", key[0], REASON_BAD))
            continue
        carrier_scans[key] = {
            "waybill": key[0], "seq": key[1], "event": event,
            "station": station, "ts": ts,
        }
        group_versions[key] = parsed

    # ---- mirror ----------------------------------------------------------
    mirror_rows = defaultdict(list)
    for waybill, event, station, ts_raw in read_rows(mirror_path, 4):
        if waybill not in waybills:
            drops.append(("mirror", waybill, REASON_FOREIGN))
            continue
        ts = parse_ts(ts_raw)
        if not event or not station or ts is None:
            drops.append(("mirror", waybill, REASON_BAD))
            continue
        mirror_rows[waybill].append(
            {"waybill": waybill, "event": event, "station": station, "ts": ts}
        )

    # Deterministic iteration: scans by (waybill, numeric seq); mirrors by ts.
    scan_keys = sorted(carrier_scans, key=lambda k: (k[0], int(k[1]) if SEQ_RE.match(k[1]) else 0, k[1]))
    for wb in mirror_rows:
        mirror_rows[wb].sort(key=lambda r: (r["ts"], r["event"], r["station"]))

    # ---- pair one carrier scan with one mirror scan ----------------------
    # Identity = same waybill, same event, same station, clocks within
    # MAX_SKEW_SECONDS.  Any version of a resent scan may establish identity.
    candidates = []
    for key in scan_keys:
        wb = key[0]
        for mi, m in enumerate(mirror_rows.get(wb, ())):
            best = None
            for event, station, ts in group_versions[key]:
                if event != m["event"] or station != m["station"]:
                    continue
                skew = abs((ts - m["ts"]).total_seconds())
                if skew <= MAX_SKEW_SECONDS and (best is None or skew < best):
                    best = skew
            if best is not None:
                candidates.append((best, key, mi))
    candidates.sort()

    used_c, used_m = set(), set()
    for skew, key, mi in candidates:
        if key in used_c or (key[0], mi) in used_m:
            continue
        used_c.add(key)
        used_m.add((key[0], mi))

    # ---- events ----------------------------------------------------------
    events = []   # dicts with a stable tie-break index
    order = 0
    for key in scan_keys:
        c = carrier_scans[key]
        events.append({
            "waybill": c["waybill"], "seq": c["seq"], "event": c["event"],
            "station": c["station"], "ts": c["ts"],
            "origin": "both" if key in used_c else "carrier",
            "tid": order,
        })
        order += 1
    for wb in sorted(mirror_rows):
        for mi, m in enumerate(mirror_rows[wb]):
            if (wb, mi) in used_m:
                continue
            events.append({
                "waybill": wb, "seq": "", "event": m["event"],
                "station": m["station"], "ts": m["ts"], "origin": "mirror",
                "tid": order,
            })
            order += 1

    # waybill asc; ts asc; at the same instant numbered rows first, by seq;
    # rows without a seq after numbered ones.
    events.sort(key=lambda e: (
        e["waybill"], e["ts"], 0 if e["seq"] else 1,
        int(e["seq"]) if e["seq"] else 0,
        e["event"], e["station"], e["origin"], e["tid"],
    ))

    drops.sort(key=lambda d: (d[0], d[1], d[2]))
    return waybills, events, drops, resends


def write_events(path, events):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["waybill", "seq", "event", "station", "ts", "origin"])
        for e in events:
            w.writerow([e["waybill"], e["seq"], e["event"], e["station"],
                        fmt_ts(e["ts"]), e["origin"]])


def write_stats(path, waybills, events):
    per = defaultdict(list)
    for e in events:
        per[e["waybill"]].append(e)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["waybill", "first_ts", "last_ts", "events",
                    "carriers", "mirrors"])
        for wb in sorted(waybills):
            evs = per.get(wb, [])
            first = evs[0]["ts"] if evs else None
            last = evs[-1]["ts"] if evs else None
            carriers = sum(1 for e in evs if e["origin"] == "carrier")
            mirrors = sum(1 for e in evs if e["origin"] == "mirror")
            w.writerow([wb, fmt_ts(first), fmt_ts(last), len(evs),
                        carriers, mirrors])


def write_drops(path, drops):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["src", "waybill", "reason"])
        for src, waybill, reason in drops:
            w.writerow([src, waybill, reason])


def write_summary(path, merged, dropped, resends):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.write("merged %d\n" % merged)
        fh.write("dropped %d\n" % dropped)
        fh.write("resends %d\n" % resends)
        fh.write(NOTES + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("carrier", nargs="?", default="carrier.csv")
    parser.add_argument("mirror", nargs="?", default="mirror.csv")
    parser.add_argument("waybills", nargs="?", default="waybills.csv")
    parser.add_argument("--out-dir", default=".")
    args = parser.parse_args(argv)

    waybills, events, drops, resends = merge(
        args.carrier, args.mirror, args.waybills
    )

    def out(name):
        return os.path.join(args.out_dir, name)

    write_events(out("events.csv"), events)
    write_stats(out("stats.csv"), waybills, events)
    write_drops(out("drops.csv"), drops)
    write_summary(out("merge_summary.txt"), len(events), len(drops), resends)

    print("merged=%d dropped=%d resends=%d" % (len(events), len(drops), resends))
    return 0


if __name__ == "__main__":
    sys.exit(main())
