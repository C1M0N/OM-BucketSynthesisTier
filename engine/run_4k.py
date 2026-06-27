#!/usr/bin/env python3
"""
run_4k.py - orchestrator for the osu!mania 4K composite-difficulty data engine.

Steps:
  1. Copy the LIVE client.realm to /tmp (read-only source; never touch the game folder).
  2. Parse it with realm_engine (beatmaps / collections / scores).
  3. Resolve md5 -> sha256 via the content-addressed blob store (+ batch_all fallback).
  4. For every beatmap md5 NOT already present as "ok"/"skip" in results.json:
        fetch its .osu by sha256, 4K-filter, run the 3 calculators (node batch_4k.mjs),
        compute scaled+bucket+type, write back to results.json (checkpointed per chunk).
  5. Recompute scaled+bucket+type for ALL 4K maps with the FINAL LN-gated composite
     (parsing lnr fresh from .osu where the cache lacks it).
  6. If >= 1 NEW 4K map was added this run -> (re)write collection.db.
  7. ALWAYS emit report_data.json.

FINAL composite (authoritative, from task spec):
    base   = (MSD + 4*isr + 4*rsr) / 9
    lnr    = holdNotes / totalNotes
    hb     = 4*lnr*(1-lnr)
    scaled = 1.30 * base * (1 + 0.18*lnr + 0.12*hb)
    bucket = floor(scaled*2)/2            # 0.5-wide, NOT rounded
    name   = "4k-%04.1f" % bucket         # 4k-00.5 .. 4k-19.0
Sanity: Reform 10 dan -> scaled ~= 13.0.

NOTE on the cache: the pre-existing results.json `scaled`/`bucket` were computed with the
OLDER base-only formula (Reform 10 ~= 10.0). This run overwrites scaled/bucket with the
FINAL LN-gated values and adds `lnr`/`type`. msd/isr/rsr are preserved unchanged
(re-validated identical to cache). results.json stays backward-compatible (same keys + extras).
"""
import os, sys, json, math, shutil, subprocess, hashlib, time, datetime, tempfile

# ---------- paths ----------
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import realm_engine
import osu_meta
import bstv2          # BSTv2 融合定级 + [4k] 命名 + 冻结参数
import mapminus       # Map Minus (MM) 本地移植

# Session-portable: derive everything from this script's own location.
# HERE = <mnt>/12_osu/4k_classification/engine ; OSU_DIR = <mnt>/12_osu
OSU_DIR = os.path.dirname(os.path.dirname(HERE))   # <mnt>/12_osu
LIVE_REALM = os.path.join(OSU_DIR, "client.realm")
FILES_ROOT = os.path.join(OSU_DIR, "files")
REALMREAD = os.path.join(HERE, "cache")
RESULTS = os.path.join(REALMREAD, "results.json")
BATCH_ALL = os.path.join(REALMREAD, "batch_all.json")
# Cross-platform temp + calc-repo location. On Windows (local), set CALC_REPO to the
# extracted persistent calc env; temp files go to the OS temp dir. Defaults keep the
# original sandbox (/tmp) behavior working unchanged.
_TMP = os.environ.get("OSU4K_TMP") or tempfile.gettempdir()
BLOB_INDEX = os.path.join(_TMP, "osu4k_md5_to_sha.json")   # (rebuilt each run; see ensure_blob_index)
REPO = os.environ.get("CALC_REPO") or "/tmp/calc/ManiaMapAnalyser by Leo_Black"
RUNNER = os.path.join(REPO, "batch_4k.mjs")
TMP_REALM = os.path.join(_TMP, "osu4k_client_run.realm")
OUT_DIR = HERE
COLLECTION_DB = os.path.join(OUT_DIR, "collection.db")
REPORT = os.path.join(OUT_DIR, "report_data.json")
PRESERVE_COLLECTION = "Y.S.Z.D."
CHUNK = 60                                    # maps per node invocation


# ---------- composite ----------
def composite(msd, isr, rsr, lnr):
    base = (msd + 4 * isr + 4 * rsr) / 9.0
    hb = 4 * lnr * (1 - lnr)
    scaled = 1.30 * base * (1 + 0.18 * lnr + 0.12 * hb)
    return base, scaled


def bucket_of(scaled):
    return math.floor(scaled * 2) / 2.0


def bucket_name(b):
    return "4k-%04.1f" % b


# ---------- io ----------
def load_json(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def md5_hex(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def sha_path(sha):
    return os.path.join(FILES_ROOT, sha[0], sha[:2], sha)


# ---------- blob index ----------
def ensure_blob_index():
    # Always rebuild from the LIVE realm (cheap, ~0.1s via the MD5Hash+Hash columns).
    # A stale on-disk index on a persistent machine would miss newly-added maps and
    # leave them uncomputed; the sandbox got away with caching only because /tmp was wiped.
    idx = {}
    import re as _re
    rr = realm_engine.Realm(LIVE_REALM)
    H32 = _re.compile(r'^[0-9a-f]{32}$'); H64 = _re.compile(r'^[0-9a-f]{64}$')
    leaves, _ = rr.table_leaves('class_Beatmap')
    for leaf in leaves:
        lc = rr.read_refs(leaf)
        md5c = rr.decode_string_column(rr.leaf_col(lc, 'class_Beatmap', 'MD5Hash')) or []
        shac = rr.decode_string_column(rr.leaf_col(lc, 'class_Beatmap', 'Hash')) or []
        for m, s in zip(md5c, shac):
            if H32.match(m or '') and H64.match(s or ''):
                idx[m] = s
    if BLOB_INDEX:
        json.dump(idx, open(BLOB_INDEX, 'w'))
    return idx


# ---------- node calculator chunk ----------
def run_calculators(items):
    """items=[{md5,path}] -> {md5: rec}. Calls node batch_4k.mjs."""
    if not items:
        return {}
    inp = os.path.join(_TMP, "osu4k_batch_in.json")
    outp = os.path.join(_TMP, "osu4k_batch_out.json")
    json.dump(items, open(inp, "w"))
    subprocess.run(["node", RUNNER, inp, outp], cwd=REPO, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    res = load_json(outp, [])
    return {r["md5"]: r for r in res}


# ---------- main ----------
def main():
    # 控制台可能是 GBK(cp936)，桶名含 ℵ(U+2135) 等非 GBK 字符时直接 print 会崩；
    # 让 stdout/stderr 对无法编码的字符容错（仅影响打印，不影响写出的 db/json）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap_max = None
    phase = "all"   # all | compute | finalize
    for a in sys.argv[1:]:
        if a.startswith("--max-new="):
            ap_max = int(a.split("=", 1)[1])
        elif a.startswith("--phase="):
            phase = a.split("=", 1)[1]
        elif a.startswith("--realm="):
            globals()["LIVE_REALM"] = a.split("=", 1)[1]

    t0 = time.time()
    # 1. copy live realm
    shutil.copy2(LIVE_REALM, TMP_REALM)
    print("[copy] realm -> %s" % TMP_REALM)

    # 2/3. parse + resolve sha256
    blob_index = ensure_blob_index()
    batch_all = load_json(BATCH_ALL, [])
    parsed = realm_engine.emit(TMP_REALM, blob_index=blob_index, batch_all=batch_all)
    beatmaps = parsed["beatmaps"]
    collections = parsed["collections"]
    scores = parsed["scores"]
    bm_total = parsed["_meta"]["beatmap_total"]
    print("[parse] beatmaps=%d (declared %d) scores=%d collections=%d" % (
        len(beatmaps), bm_total, len(scores), len(collections)))

    md5_to_sha = {b["md5"]: b["sha256"] for b in beatmaps}
    md5_to_sr = {b["md5"]: b["sr"] for b in beatmaps}

    results = load_json(RESULTS, {})
    # Baseline = the 4K set captured before this engine run started. Stored once in a sidecar
    # so newMaps/dbRegenerated are correct even though the run is split across invocations
    # (sandbox processes do not persist between bash calls).
    BASELINE = os.path.join(OUT_DIR, ".baseline_4k.json")
    if not os.path.exists(BASELINE):
        json.dump(sorted(k for k, v in results.items() if v.get("status") == "ok"),
                  open(BASELINE, "w"))
    baseline_4k = set(load_json(BASELINE, []))

    # 4. find NEW maps (not ok/skip)
    new_md5 = [b["md5"] for b in beatmaps
               if results.get(b["md5"], {}).get("status") not in ("ok", "skip")]
    print("[new] %d beatmap md5s need computing" % len(new_md5))

    # build work items (only those with a resolvable + existing .osu blob)
    work = []
    unresolved = 0
    for m in new_md5:
        sha = md5_to_sha.get(m)
        if not sha:
            unresolved += 1
            continue
        p = sha_path(sha)
        if not os.path.exists(p):
            unresolved += 1
            continue
        work.append({"md5": m, "path": p, "sha256": sha})
    if unresolved:
        print("[new] %d new md5s have no usable .osu blob (skipped, left uncomputed)" % unresolved)

    if ap_max is not None:
        work = work[:ap_max]
        print("[new] capped to %d this invocation" % len(work))

    # process in chunks, checkpoint results.json each chunk
    newly_4k = 0
    done = 0
    if phase == "finalize":
        work = []   # skip computing; finalize uses whatever is already in results.json
    for i in range(0, len(work), CHUNK):
        chunk = work[i:i + CHUNK]
        recs = run_calculators([{"md5": w["md5"], "path": w["path"]} for w in chunk])
        for w in chunk:
            m = w["md5"]
            r = recs.get(m)
            sr = md5_to_sr.get(m)
            if r is None:
                continue
            if not r.get("mode") == 3 or round(r.get("cs") or 0) != 4:
                # non-4K -> skip
                results[m] = {"status": "skip", "sr": sr if sr is not None else 0.0}
                continue
            if not r.get("ok") or r.get("msd") is None:
                # failed compute; leave uncomputed (do not poison cache)
                continue
            msd, isr, rsr = r["msd"], r["isr"], r["rsr"]
            holds = r.get("holds", 0); total = r.get("total", 0)
            lnr = holds / total if total else 0.0
            meta = osu_meta.parse_file(w["path"])
            base, scaled = composite(msd, isr, rsr, lnr)
            b = bucket_of(scaled)
            try:
                _mmres = mapminus.compute(open(w["path"], encoding="utf-8", errors="replace").read())
                _mm = _mmres["rating"]; _mmskills = _mmres.get("skills")
            except Exception:
                _mm = None; _mmskills = None
            results[m] = {
                "status": "ok", "sr": sr if sr is not None else 0.0,
                "msd": msd, "isr": isr, "rsr": rsr,
                "mm": _mm, "mmSkills": _mmskills, "skills": r.get("msdSkills"),
                "scaled": scaled, "bucket": b,
                "lnr": lnr, "type": meta["type"],
                "title": r.get("title", ""), "version": r.get("version", ""),
            }
            newly_4k += 1
        done += len(chunk)
        json.dump(results, open(RESULTS, "w"))
        print("[compute] %d/%d done (new 4K so far %d, t=%.1fs)" % (done, len(work), newly_4k, time.time() - t0))

    if phase == "compute":
        json.dump(results, open(RESULTS, "w"))
        remaining = sum(1 for b in beatmaps
                        if results.get(b["md5"], {}).get("status") not in ("ok", "skip")
                        and md5_to_sha.get(b["md5"]) and os.path.exists(sha_path(md5_to_sha[b["md5"]])))
        print("[compute-phase] done this invocation; newly 4K=%d; remaining computable=%d (t=%.1fs)"
              % (newly_4k, remaining, time.time() - t0))
        return

    # 5. recompute scaled+bucket+type for ALL 4K with FINAL formula (+ fill lnr/type from .osu)
    print("[recompute] applying FINAL LN-gated composite to all 4K maps...")
    recomputed = 0
    for m, v in results.items():
        if v.get("status") != "ok":
            continue
        if "msd" not in v:
            continue
        lnr = v.get("lnr")
        typ = v.get("type")
        if lnr is None or typ is None:
            sha = md5_to_sha.get(m)
            if sha:
                p = sha_path(sha)
                if os.path.exists(p):
                    meta = osu_meta.parse_file(p)
                    lnr = meta["lnr"]; typ = meta["type"]
            if lnr is None:
                # fall back: derive lnr unknown -> 0 (rice); mark type RC
                lnr = 0.0; typ = typ or "RC"
            v["lnr"] = lnr; v["type"] = typ
        base, scaled = composite(v["msd"], v["isr"], v["rsr"], lnr)
        v["scaled"] = scaled
        # BSTv2: ensure MM (Map Minus overall + 6-skill vector) then fuse 0.4*z(BST)+0.6*z(MM)
        if v.get("mm") is None or v.get("mmSkills") is None:
            sha = md5_to_sha.get(m)
            if sha and os.path.exists(sha_path(sha)):
                try:
                    _r = mapminus.compute(open(sha_path(sha), encoding="utf-8", errors="replace").read())
                    v["mm"] = _r["rating"]; v["mmSkills"] = _r.get("skills")
                except Exception:
                    if v.get("mm") is None:
                        v["mm"] = None
        if v.get("mm") is not None:
            v["bstv2"] = bstv2.bstv2(scaled, v["mm"])
            v["bucket"] = bstv2.bucket_of(v["bstv2"])
        else:
            v["bstv2"] = scaled            # no MM (deleted-map remnant) -> fall back to BST scale
            v["bucket"] = bucket_of(scaled)
        recomputed += 1
    json.dump(results, open(RESULTS, "w"))
    print("[recompute] %d 4K maps re-graded with BSTv2" % recomputed)

    # build bucket membership (md5 lists) from current 4K results, restricted to realm beatmaps
    realm_md5 = set(b["md5"] for b in beatmaps)
    buckets = {}            # name -> [md5]
    bucket_rows = {}        # name -> list of result dicts
    for m, v in results.items():
        if v.get("status") != "ok" or "bucket" not in v:
            continue
        if m not in realm_md5:
            continue
        name = bstv2.bucket_name(v["bucket"])
        buckets.setdefault(name, []).append(m)
        bucket_rows.setdefault(name, []).append(v)
    total_4k = sum(len(x) for x in buckets.values())
    print("[buckets] %d non-empty 4K buckets, %d total 4K maps" % (len(buckets), total_4k))

    # 建出完整阶梯（[4k]0 .. [4k]Z+，桶值 0.0..20.5）+ ℵ 顶档；空桶也保留为空，
    # 让报告分布与收藏夹梯子都连续到顶。
    ladder = [bstv2.bucket_name(i * 0.5) for i in range(0, 42)]  # 0.0 .. 20.5
    ladder.append(bstv2.bucket_name(21.0))                       # ℵ
    for nm in ladder:
        buckets.setdefault(nm, [])
        bucket_rows.setdefault(nm, [])

    # newMaps = 4K maps now present that were NOT in the pre-run baseline
    cur_4k = set()
    for m, v in results.items():
        if v.get("status") == "ok" and "bucket" in v and m in realm_md5:
            cur_4k.add(m)
    new_maps_total = len(cur_4k - baseline_4k)
    print("[new] newMaps vs baseline = %d" % new_maps_total)

    # preserved collections = every NON-"4k-*" collection currently in the realm (the user's
    # manual collections). The original spec hard-coded a single name ("Y.S.Z.D."), but that
    # collection no longer exists and the user now has others (e.g. "10SKIP"); preserving ALL
    # non-bucket collections by name is the robust generalization so none are lost on import.
    preserved = [(c["name"], c["hashes"]) for c in collections
                 if not c["name"].startswith("4k-") and not c["name"].startswith("[4k]")]
    print("[preserve] %d manual (non-4k) collection(s): %s" % (
        len(preserved), ", ".join("%s(%d)" % (n, len(h)) for n, h in preserved) or "(none)"))

    # 6. write collection.db if >=1 new 4K map this run
    db_regenerated = False
    if new_maps_total >= 1 or not os.path.exists(COLLECTION_DB):
        write_collection_db(COLLECTION_DB, buckets, preserved)
        db_regenerated = True
        print("[db] wrote %s (regenerated=%s)" % (COLLECTION_DB, db_regenerated))
    else:
        print("[db] no new 4K maps this run -> collection.db NOT regenerated")

    # bucket changes vs the live realm's existing 4k-* collections
    old_counts = {c["name"]: len(c["hashes"]) for c in collections if c["name"].startswith("[4k]")}
    new_counts = {name: len(v) for name, v in buckets.items()}
    bucket_changes = []
    for name in sorted(set(old_counts) | set(new_counts)):
        delta = new_counts.get(name, 0) - old_counts.get(name, 0)
        if delta != 0:
            bucket_changes.append({"name": name, "delta": delta})

    # 7. report_data.json
    report = build_report(results, beatmaps, scores, buckets, bucket_rows,
                          new_maps_total, total_4k, len(beatmaps), db_regenerated, bucket_changes,
                          md5_to_sha)
    json.dump(report, open(REPORT, "w"), indent=1)
    print("[report] wrote %s" % REPORT)

    # summary
    print("\n===== SUMMARY =====")
    print("newMaps(new 4K):", new_maps_total)
    print("total4k:", total_4k)
    print("totalBeatmaps:", len(beatmaps))
    print("dbRegenerated:", db_regenerated)
    print("per-bucket counts:")
    for name in sorted(buckets):
        print("   %-9s %d" % (name, len(buckets[name])))
    print("bucketChanges:", bucket_changes)
    print("scores:", len(scores))
    print("elapsed: %.1fs" % (time.time() - t0))


# ---------- collection.db writer ----------
def write_string(buf, s):
    b = s.encode("utf-8")
    buf.append(b"\x0b")
    n = len(b)
    out = bytearray()
    while True:
        x = n & 0x7f
        n >>= 7
        if n:
            out.append(x | 0x80)
        else:
            out.append(x)
            break
    buf.append(bytes(out))
    buf.append(b)


def write_collection_db(path, buckets, preserved):
    import struct as _s
    # buckets 已含完整阶梯（[4k]0 .. [4k]Z+ + ℵ，空桶在内），按桶值升序写出。
    names = sorted(buckets.keys(), key=bstv2.name_value)
    cols = [(n, buckets[n]) for n in names]
    if preserved:
        cols.extend(preserved)   # append manual (non-4k) collections after the 4k-* buckets
    buf = []
    buf.append(_s.pack("<i", 20240101))      # version
    buf.append(_s.pack("<i", len(cols)))     # numCollections
    for name, hashes in cols:
        write_string(buf, name)
        buf.append(_s.pack("<i", len(hashes)))
        for h in hashes:
            write_string(buf, h)
    with open(path, "wb") as f:
        f.write(b"".join(buf))


# ---------- report builder ----------
def build_report(results, beatmaps, scores, buckets, bucket_rows,
                 new_maps, total_4k, total_beatmaps, db_regen, bucket_changes,
                 md5_to_sha):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # sha256 -> 4K map info (for joining scores)
    sha_to_map = {}
    realm_md5 = set(b["md5"] for b in beatmaps)
    for b in beatmaps:
        v = results.get(b["md5"])
        if v and v.get("status") == "ok" and "scaled" in v and b.get("sha256"):
            sha_to_map[b["sha256"]] = {"comp": v.get("bstv2", v["scaled"]), "typ": v.get("type") or "RC"}

    # scores joined to 4K maps
    # CHANGE 0 (global): treat any score with acc < 0.80 as INVALID. Applied here at
    # score-collection time so scores[], typePerf[], progress[] AND the new per-bucket
    # avgAcc (change 2) are all computed only from valid scores (acc >= 0.80).
    ACC_MIN = 0.80
    sarr = []
    valid_by_md5 = {}     # md5 -> [valid acc, ...] for per-bucket avgAcc (change 2)
    sha_to_md5 = {b["sha256"]: b["md5"] for b in beatmaps if b.get("sha256")}
    for s in scores:
        mp = sha_to_map.get(s["sha256"])
        if not mp:
            continue
        a = s.get("acc")
        if a is None or a < ACC_MIN:
            continue
        sarr.append({
            "comp": mp["comp"],
            "acc": a,
            "date": s.get("date"),
            "typ": mp["typ"],
        })
        m = sha_to_md5.get(s["sha256"])
        if m is not None:
            valid_by_md5.setdefault(m, []).append(a)

    # best-per-map for the scatter: one point per 4K map = its BEST acc, sized by
    # attempt count. Attempts count ALL plays (incl. failed/<80%) joined to the map,
    # so heavily-retried maps render larger. Only maps whose best is valid (>=80%)
    # are emitted (matches the chart's "real plays" axis range).
    per_map = {}
    for s in scores:
        mp = sha_to_map.get(s["sha256"])
        if not mp:
            continue
        a = s.get("acc")
        if a is None:
            continue
        key = sha_to_md5.get(s["sha256"]) or s["sha256"]
        d = per_map.get(key)
        if d is None:
            per_map[key] = {"comp": mp["comp"], "typ": mp["typ"], "acc": a, "attempts": 1}
        else:
            d["attempts"] += 1
            if a > d["acc"]:
                d["acc"] = a
    best_scores = [d for d in per_map.values() if d["acc"] >= ACC_MIN]

    # buckets array
    # CHANGE 2 (data): per-bucket avgAcc = mean accuracy (0..1) of VALID scores
    # (acc>=0.80) whose map falls in that bucket. Scores join to maps by
    # sha256->md5->bucket; `buckets[name]` already holds that bucket's md5 list.
    # MM 6-skill dimension labels (RC=米流 ST=耐力 SP=速度 LN=面条 CO=协调 PR=精准)
    SKILL_ABBR = ["RC", "ST", "SP", "LN", "CO", "PR"]
    barr = []
    for name in sorted(buckets):
        rows = bucket_rows[name]
        cnt = len(rows)
        srs = sorted([r["sr"] for r in rows if r.get("sr") is not None])
        scs = [r.get("bstv2", r.get("scaled")) for r in rows if (r.get("bstv2") is not None or r.get("scaled") is not None)]
        types = {"RC": 0, "LN": 0, "HB": 0, "MIX": 0, "Vibro": 0}
        for r in rows:
            t = r.get("type") or "RC"
            if t in types:
                types[t] += 1
            else:
                types["RC"] += 1
        # per-bucket mean of each MM skill (维度体系：每个难度桶的技能构成)
        sk_sum = [0.0] * 6
        sk_n = 0
        for r in rows:
            ms = r.get("mmSkills")
            if ms and len(ms) >= 6:
                for i in range(6):
                    sk_sum[i] += ms[i]
                sk_n += 1
        skills_mean = {SKILL_ABBR[i]: (sk_sum[i] / sk_n if sk_n else 0.0) for i in range(6)}
        # MinaCalc（Etterna MSD）8 技能集均值：数据已在缓存 r["skills"]，此处按桶汇总
        msd_sum = {}
        msd_n = 0
        for r in rows:
            sk = r.get("skills")
            if isinstance(sk, dict) and sk:
                for k, val in sk.items():
                    if val is not None:
                        msd_sum[k] = msd_sum.get(k, 0.0) + float(val)
                msd_n += 1
        msd_mean = {k: (v / msd_n) for k, v in msd_sum.items()} if msd_n else {}
        # 本桶已游玩谱数（用于“当前涉足范围”可视化）
        played_cnt = sum(1 for m in buckets.get(name, []) if m in valid_by_md5)
        # 置信标志 provisional：仅当 .osu 源文件缺失（已删谱残留）导致 MM/技能向量算不出时才标记。
        # 官方星(sr)在定级中根本不参与，故绝不作为判据（之前误纳入 -> 大量误报）。
        prov = sum(1 for r in rows
                   if (r.get("mm") is None or not r.get("mmSkills")))
        try:
            b = bstv2.name_value(name)
        except Exception:
            b = None
        # collect every valid score acc whose map md5 is in this bucket
        bucket_accs = []
        for m in buckets.get(name, []):
            bucket_accs.extend(valid_by_md5.get(m, []))
        # best-per-map 准确率（>=80%）—— 供“误差率箱线图”按桶绘制分布
        acc_best = [per_map[m]["acc"] for m in buckets.get(name, [])
                    if m in per_map and per_map[m].get("acc") is not None
                    and per_map[m]["acc"] >= ACC_MIN]
        barr.append({
            "bucket": b, "name": name, "count": cnt,
            "avgSr": (sum(srs) / len(srs)) if srs else None,
            "srMin": srs[0] if srs else None,
            "srMed": srs[len(srs) // 2] if srs else None,
            "srMax": srs[-1] if srs else None,
            "avgScaled": (sum(scs) / len(scs)) if scs else None,
            "avgAcc": (sum(bucket_accs) / len(bucket_accs)) if bucket_accs else None,
            "accBest": acc_best,
            "types": types,
            "skills": skills_mean,
            "msdSkills": msd_mean,
            "playedCount": played_cnt,
            "provisional": prov,
        })

    # typePerf —— 用 best-per-map（全曲最佳），与误差率口径统一
    typeagg = {}
    for s in best_scores:
        t = s.get("typ") or "RC"
        d = typeagg.setdefault(t, [0, 0.0])
        d[0] += 1
        d[1] += s["acc"]
    typePerf = [{"typ": t, "count": d[0], "avgAcc": (d[1] / d[0]) if d[0] else 0.0}
                for t, d in sorted(typeagg.items())]

    # progress: split all joined scores into 6 equal-count bins by play-order (date asc)
    dated = [s for s in sarr if s.get("date") is not None]
    dated.sort(key=lambda s: s["date"])
    progress = []
    nb = 6
    n = len(dated)
    if n:
        for i in range(nb):
            lo = i * n // nb
            hi = (i + 1) * n // nb
            chunk = dated[lo:hi]
            accs = [c["acc"] for c in chunk if c["acc"] is not None]
            comps = [c["comp"] for c in chunk if c.get("comp") is not None]
            progress.append({
                "idx": i + 1,
                "label": "%d-%d" % (lo + 1, hi),
                "avgAcc": (sum(accs) / len(accs)) if accs else 0.0,
                "avgComp": (sum(comps) / len(comps)) if comps else 0.0,
                "count": len(chunk),
            })

    # user skill profile: mean MM 6-skill vector over the whole 4K library vs over played maps
    lib_sum = [0.0] * 6
    lib_n = 0
    prov_total = 0
    for m, v in results.items():
        if v.get("status") != "ok" or m not in realm_md5 or "bucket" not in v:
            continue
        if v.get("mm") is None or not v.get("mmSkills"):
            prov_total += 1
        ms = v.get("mmSkills")
        if ms and len(ms) >= 6:
            for i in range(6):
                lib_sum[i] += ms[i]
            lib_n += 1
    library_profile = {SKILL_ABBR[i]: (lib_sum[i] / lib_n if lib_n else 0.0) for i in range(6)}
    pl_sum = [0.0] * 6
    pl_n = 0
    for m in valid_by_md5:
        v = results.get(m)
        ms = v.get("mmSkills") if v else None
        if ms and len(ms) >= 6:
            for i in range(6):
                pl_sum[i] += ms[i]
            pl_n += 1
    played_profile = {SKILL_ABBR[i]: (pl_sum[i] / pl_n if pl_n else 0.0) for i in range(6)}

    # MinaCalc 8 技能集画像（全库 vs 已游玩）
    MSD_ORDER = ["Overall", "Stream", "Jumpstream", "Handstream",
                 "Stamina", "JackSpeed", "Chordjack", "Technical"]

    def _msd_mean(md5_iter):
        acc = {k: 0.0 for k in MSD_ORDER}
        nn = 0
        for m in md5_iter:
            v = results.get(m)
            sk = v.get("skills") if v else None
            if isinstance(sk, dict) and sk:
                for k in MSD_ORDER:
                    if sk.get(k) is not None:
                        acc[k] += float(sk[k])
                nn += 1
        return ({k: (acc[k] / nn if nn else 0.0) for k in MSD_ORDER}, nn)

    lib_md5 = [m for m, v in results.items()
               if v.get("status") == "ok" and m in realm_md5 and "bucket" in v]
    msd_library, msd_lib_n = _msd_mean(lib_md5)
    msd_played, msd_pl_n = _msd_mean(list(valid_by_md5.keys()))

    return {
        "generatedAt": now,
        "summary": {
            "newMaps": new_maps,
            "total4k": total_4k,
            "totalBeatmaps": total_beatmaps,
            "dbRegenerated": db_regen,
            "bucketChanges": bucket_changes,
            "provisionalTotal": prov_total,
        },
        "skillNames": SKILL_ABBR,
        "skillProfile": {
            "library": library_profile, "libraryN": lib_n,
            "played": played_profile, "playedN": pl_n,
        },
        "msdSkillNames": MSD_ORDER,
        "msdSkillProfile": {
            "library": msd_library, "libraryN": msd_lib_n,
            "played": msd_played, "playedN": msd_pl_n,
        },
        "buckets": barr,
        "scores": sarr,
        "bestScores": best_scores,
        "typePerf": typePerf,
        "progress": progress,
    }


if __name__ == "__main__":
    main()
