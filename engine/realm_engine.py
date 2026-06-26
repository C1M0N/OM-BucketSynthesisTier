#!/usr/bin/env python3
"""
realm_engine.py - hand-rolled osu!lazer client.realm parser.

Parses a *copy* of an osu!lazer client.realm and emits:
  - beatmaps   : [{md5, sha256, sr}]   (sha256/sr resolved from blob store + batch_all fallback)
  - collections: [{name, hashes}]      (hashes = beatmap MD5s)
  - scores     : [{sha256, acc, date}] (every score row; sha256 = BeatmapHash, date = unix seconds)

Realm format read by hand (no Realm SDK). Helpers mirror outputs/realmread/*.py fragments.

Storage notes discovered while reconstructing this engine:
  * Top array @ u64LE(8 if (d[22]&1) else 0); tv[0]=table-name strings, tv[1]=table roots.
  * Table root array T: T[0]=spec [types,names,...]; T[2]=cluster b+tree root.
  * cluster root (is_inner): elem[0]=compact key array, elem[1]=tag, elem[2]=tagged TOTAL count,
    elem[3:]=leaf clusters. Leaf cluster element k = column k (spec column order, no offset).
  * String columns: layout A compact [offsets_u16, blob(wtype=2), null]; layout B per-row refs
    (size==rowcount) each child a blob leaf with one null-terminated string (sha256 65 bytes).
  * Double columns (Accuracy/StarRating) have wtype=1 and width under-reports; data is real
    float64 at stride 8 -> read as f64.
  * Timestamp (Date) column = has_refs [seconds_int_array, nanos_int_array]; seconds has a
    leading sentinel so per-row seconds = secs[-rowcount:].
"""
import struct, re, json, os, sys

HEX32 = re.compile(r'^[0-9a-f]{32}$')
HEX64 = re.compile(r'^[0-9a-f]{64}$')


class Realm:
    def __init__(self, path):
        with open(path, "rb") as fh:
            self.d = fh.read()
        self.N = len(self.d)

    def hdr(self, off):
        d = self.d
        h = d[off:off + 8]
        f = h[4]
        return dict(is_inner=bool(f & 0x80), has_refs=bool(f & 0x40), ctx=bool(f & 0x20),
                    wtype=(f >> 3) & 3, width=(1 << (f & 7)) >> 1,
                    size=(h[5] << 16) | (h[6] << 8) | h[7], data=off + 8, flag=f)

    def safe(self, off):
        return off and off % 2 == 0 and 0 < off < self.N - 8

    def read_ints(self, off):
        H = self.hdr(off); w = H['width']; n = H['size']; b = H['data']; d = self.d; N = self.N
        if w == 0:
            return [0] * n
        sz = w // 8
        if sz and b + sz * n > N:
            n = max(0, (N - b) // sz)
        fmt = {8: '<B', 16: '<H', 32: '<I', 64: '<Q'}.get(w)
        if fmt:
            return [struct.unpack_from(fmt, d, b + sz * i)[0] for i in range(n)]
        m = (1 << w) - 1
        return [(d[b + ((i * w) >> 3)] >> ((i * w) & 7)) & m for i in range(n)]

    read_refs = read_ints

    def read_ints_signed(self, off):
        H = self.hdr(off); w = H['width']; n = H['size']; b = H['data']; d = self.d; N = self.N
        if w == 0:
            return [0] * n
        sz = w // 8
        if sz and b + sz * n > N:
            n = max(0, (N - b) // sz)
        fmt = {8: '<b', 16: '<h', 32: '<i', 64: '<q'}.get(w)
        if fmt:
            return [struct.unpack_from(fmt, d, b + sz * i)[0] for i in range(n)]
        return self.read_ints(off)

    def read_f64_at(self, off, count):
        H = self.hdr(off); b = H['data']; d = self.d; N = self.N
        out = []
        for i in range(count):
            if b + 8 * i + 8 > N:
                break
            out.append(struct.unpack_from('<d', d, b + 8 * i)[0])
        return out

    def read_strs_fixed(self, off):
        H = self.hdr(off); w = H['width']; n = H['size']; b = H['data']; d = self.d; out = []
        if w == 0:
            return [''] * n
        for i in range(n):
            slot = d[b + i * w:b + (i + 1) * w]
            if len(slot) < w:
                out.append(None); continue
            last = slot[w - 1]; L = (w - 1) - last
            out.append(slot[:L].decode('utf-8', 'replace') if 0 <= L <= w - 1 else None)
        return out

    def decode_string_column(self, ref):
        H = self.hdr(ref); d = self.d; N = self.N
        if H['has_refs']:
            kids = self.read_refs(ref)
            if len(kids) >= 2 and self.safe(kids[0]) and self.safe(kids[1]):
                k1 = self.hdr(kids[1]); k0 = self.hdr(kids[0])
                if k1['wtype'] == 2 and k0['wtype'] != 2 and H['size'] <= 4:
                    offs = self.read_refs(kids[0])
                    blob = d[k1['data']:k1['data'] + min(k1['size'], N - k1['data'])]
                    out = []; prev = 0
                    for e in offs:
                        if e > len(blob):
                            e = len(blob)
                        out.append(blob[prev:e].rstrip(b'\x00').decode('utf-8', 'replace'))
                        prev = e
                    return out
            out = []
            for k in kids:
                if not self.safe(k):
                    out.append(""); continue
                kh = self.hdr(k)
                seg = d[kh['data']:kh['data'] + min(kh['size'], N - kh['data'])]
                out.append(seg.rstrip(b'\x00').decode('utf-8', 'replace'))
            return out
        if H['width'] > 0 and H['wtype'] == 1:
            return self.read_strs_fixed(ref)
        return None

    def top(self):
        d = self.d
        flags = d[22]; sel = flags & 1
        topref = struct.unpack_from('<Q', d, 8 if sel else 0)[0]
        tv = self.read_refs(topref)
        names = self.read_strs_fixed(tv[0])
        trefs = self.read_refs(tv[1])
        return names, trefs

    def table_root(self, name):
        names, trefs = self.top()
        for i, nm in enumerate(names):
            if nm == name:
                return trefs[i]
        raise KeyError(name)

    def table_spec(self, name):
        root = self.table_root(name)
        tvv = self.read_refs(root)
        sv = self.read_refs(tvv[0])
        types = self.read_ints(sv[0]) if self.safe(sv[0]) else []
        cnames = self.read_strs_fixed(sv[1]) if self.safe(sv[1]) else []
        return types, cnames

    def table_leaves(self, name):
        root = self.table_root(name)
        tvv = self.read_refs(root)
        ctree = self.read_refs(tvv[2])
        total = ctree[2] >> 1 if len(ctree) > 2 else None
        leaves = [x for x in ctree[3:] if self.safe(x)]
        return leaves, total

    # Leaf cluster arrays carry a leading b+tree key column at index 0, so a spec column
    # with declaration index i lives at leaf element (i + 1).
    KEY_OFFSET = 1

    def column_index(self, name, col):
        _types, cnames = self.table_spec(name)
        return cnames.index(col)

    def leaf_col(self, lc, name, col):
        return lc[self.column_index(name, col) + self.KEY_OFFSET]

    def beatmaps_md5(self):
        leaves, total = self.table_leaves('class_Beatmap')
        out = []
        for leaf in leaves:
            lc = self.read_refs(leaf)
            col = self.decode_string_column(self.leaf_col(lc, 'class_Beatmap', 'MD5Hash')) or []
            out += list(col)
        return [m for m in out if HEX32.match(m or '')], total

    def scores(self):
        leaves, total = self.table_leaves('class_Score')
        out = []
        for leaf in leaves:
            lc = self.read_refs(leaf)
            rown = self.hdr(self.leaf_col(lc, 'class_Score', 'MaxCombo'))['size']
            bh = self.decode_string_column(self.leaf_col(lc, 'class_Score', 'BeatmapHash')) or []
            acc = self.read_f64_at(self.leaf_col(lc, 'class_Score', 'Accuracy'), rown)
            secs = []
            dref = self.leaf_col(lc, 'class_Score', 'Date')
            if self.safe(dref):
                kids = self.read_refs(dref)
                if kids and self.safe(kids[0]):
                    s = self.read_ints_signed(kids[0])
                    secs = s[-rown:] if len(s) > rown else s
            for i in range(rown):
                h = bh[i] if i < len(bh) else ''
                if not HEX64.match(h or ''):
                    continue
                a = acc[i] if i < len(acc) else None
                t = secs[i] if i < len(secs) else None
                out.append({"sha256": h, "acc": a, "date": int(t) if t else None})
        return out, total

    def collections(self):
        root = self.table_root('class_BeatmapCollection')
        bcv = self.read_refs(root)
        cl = self.read_refs(bcv[2])
        names = self.read_strs_fixed(self.leaf_col(cl, 'class_BeatmapCollection', 'Name'))
        md5refs = self.read_refs(self.leaf_col(cl, 'class_BeatmapCollection', 'BeatmapMD5Hashes'))
        out = []
        for i in range(len(names)):
            ref = md5refs[i] if i < len(md5refs) else 0
            hashes = [h for h in self._list_strings(ref) if h] if ref else []
            out.append({"name": names[i], "hashes": hashes})
        return out

    def _list_strings(self, ref):
        if not self.safe(ref):
            return []
        H = self.hdr(ref)
        if H['is_inner']:
            kids = self.read_refs(ref)
            res = []
            for k in kids[:-1]:
                if self.safe(k):
                    res += self._list_strings(k)
            return res
        s = self.decode_string_column(ref)
        if s is not None:
            return list(s)
        if not H['has_refs'] and H['wtype'] == 1:
            return list(self.read_strs_fixed(ref))
        return []


def build_blob_index(files_root, out_path=None, only_dirs=None, existing=None):
    import hashlib
    idx = dict(existing) if existing else {}
    dirs = only_dirs or [f"{c:x}" for c in range(16)]
    for d0 in dirs:
        p0 = os.path.join(files_root, d0)
        if not os.path.isdir(p0):
            continue
        for d1 in os.listdir(p0):
            p1 = os.path.join(p0, d1)
            if not os.path.isdir(p1):
                continue
            for fn in os.listdir(p1):
                if len(fn) != 64:
                    continue
                p = os.path.join(p1, fn)
                try:
                    with open(p, 'rb') as fh:
                        head = fh.read(16)
                        if head != b'osu file format ':
                            continue
                        data = head + fh.read()
                    idx[hashlib.md5(data).hexdigest()] = fn
                except Exception:
                    pass
    if out_path:
        json.dump(idx, open(out_path, 'w'))
    return idx


def emit(realm_path, blob_index=None, batch_all=None):
    r = Realm(realm_path)
    md5s, bm_total = r.beatmaps_md5()
    cols = r.collections()
    scores, sc_total = r.scores()
    bi = dict(blob_index) if blob_index else {}
    ba = {row['md5']: row for row in (batch_all or [])}
    beatmaps = []
    for m in md5s:
        sha = bi.get(m) or ba.get(m, {}).get('sha256')
        sr = ba.get(m, {}).get('sr')
        beatmaps.append({"md5": m, "sha256": sha, "sr": sr})
    return {"beatmaps": beatmaps, "collections": cols, "scores": scores,
            "_meta": {"beatmap_total": bm_total, "score_total": sc_total}}


if __name__ == "__main__":
    realm = sys.argv[1] if len(sys.argv) > 1 else "/tmp/client_live.realm"
    r = Realm(realm)
    md5s, bt = r.beatmaps_md5()
    sc, st = r.scores()
    co = r.collections()
    print("beatmaps:", len(md5s), "(declared", bt, ")")
    print("scores:  ", len(sc), "(declared", st, ")")
    print("collections:", len(co))
    for c in co:
        print("   %-12s %d" % (c['name'], len(c['hashes'])))
    accs = [s['acc'] for s in sc if s['acc'] is not None]
    if accs:
        print("acc min/max/mean: %.4f %.4f %.4f" % (min(accs), max(accs), sum(accs) / len(accs)))
