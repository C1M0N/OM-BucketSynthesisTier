#!/usr/bin/env python3
"""
osu_meta.py - fast .osu metadata + 4K classifier (no calculators).

Parses a .osu file's text and returns:
  mode, cs, holds, total, lnr, type, vibro_frac, chord_frac, jack_frac

4K filter:  mode == 3 AND round(cs) == 4
type rules (from task spec):
  lnr = holds/total
  lnr >= 0.5 -> "LN"
  lnr >= 0.2 -> "HB"
  else rice -> compute per-column consecutive same-column time gaps:
    vibro_frac = fraction of notes whose same-column gap < 80 ms
    chord_frac = fraction of time-rows with >= 2 simultaneous notes
    jack_frac  = fraction of same-column repeats (note whose previous same-col note exists)
    vibro_frac >= 0.08                       -> "Vibro"
    chord_frac >= 0.35 and jack_frac >= 0.12 -> "MIX"
    else                                     -> "RC"
"""
import sys, json


def parse_osu(text):
    mode = None
    cs = None
    section = ""
    # per-column note start times (column index from x for mania)
    notes = []  # (time, column, is_hold)
    keycount = 4
    lines = text.split("\n")
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "General":
            if line.lower().startswith("mode:"):
                try:
                    mode = int(line.split(":", 1)[1].strip())
                except Exception:
                    pass
        elif section == "Difficulty":
            if line.lower().startswith("circlesize:"):
                try:
                    cs = float(line.split(":", 1)[1].strip())
                except Exception:
                    pass
        elif section == "HitObjects":
            parts = line.split(",")
            if len(parts) < 5:
                continue
            try:
                x = int(parts[0]); t = int(parts[2]); typ = int(parts[3])
            except Exception:
                continue
            is_hold = bool(typ & 128)
            notes.append((t, x, is_hold))

    k = int(round(cs)) if cs else keycount
    if k < 1:
        k = 4
    total = len(notes)
    holds = sum(1 for n in notes if n[2])
    lnr = holds / total if total else 0.0

    # classify
    out = {"mode": mode, "cs": cs, "holds": holds, "total": total, "lnr": lnr,
           "type": None, "vibro_frac": 0.0, "chord_frac": 0.0, "jack_frac": 0.0}
    if total == 0:
        out["type"] = "RC"
        return out
    if lnr >= 0.5:
        out["type"] = "LN"
        return out
    if lnr >= 0.2:
        out["type"] = "HB"
        return out
    # rice analysis: map x -> column
    def col_of(x):
        c = int(x * k // 512)
        if c < 0:
            c = 0
        if c >= k:
            c = k - 1
        return c
    by_col = {}
    times_count = {}
    for (t, x, _h) in notes:
        c = col_of(x)
        by_col.setdefault(c, []).append(t)
        times_count[t] = times_count.get(t, 0) + 1
    # vibro: same-column gap < 80ms
    vibro = 0
    jackrep = 0
    for c, ts in by_col.items():
        ts.sort()
        for i in range(1, len(ts)):
            jackrep += 1  # this note repeats its column
            if ts[i] - ts[i - 1] < 80:
                vibro += 1
    vibro_frac = vibro / total
    jack_frac = jackrep / total
    n_rows = len(times_count)
    chord_rows = sum(1 for t, c in times_count.items() if c >= 2)
    chord_frac = chord_rows / n_rows if n_rows else 0.0
    out["vibro_frac"] = vibro_frac
    out["chord_frac"] = chord_frac
    out["jack_frac"] = jack_frac
    if vibro_frac >= 0.08:
        out["type"] = "Vibro"
    elif chord_frac >= 0.35 and jack_frac >= 0.12:
        out["type"] = "MIX"
    else:
        out["type"] = "RC"
    return out


def parse_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return parse_osu(fh.read())


if __name__ == "__main__":
    print(json.dumps(parse_file(sys.argv[1]), indent=2))
