# -*- coding: utf-8 -*-
"""一次性全库 BSTv2 重分级:冻结参数 -> 算 BSTv2 -> 新命名 -> 写 collection.db(保留手工夹) -> 导出变更数据。"""
import os, sys, json, math, struct, re, shutil
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bstv2 as V2

DELIVER = os.path.dirname(HERE)
MMOUT = r"C:\Users\Sutheros\osu4k_calc\mm_out.json"
OLD_DB = os.path.join(DELIVER, "4k_collections.db")

def mean(x): return sum(x)/len(x)
def sd(x): m=mean(x); return math.sqrt(sum((v-m)**2 for v in x)/len(x))

mm = json.load(open(MMOUT, encoding="utf-8"))   # md5 -> {old(BST), mm, type, title, version,...}
items = [(md5, v) for md5, v in mm.items() if v.get("mm") is not None and v.get("old") is not None]
BST = [v["old"] for _, v in items]; MM = [v["mm"] for _, v in items]
mb, sb = mean(BST), sd(BST); mmn, sm = mean(MM), sd(MM)
def zb(x): return (x-mb)/sb
def zm(x): return (x-mmn)/sm
# anchor from REFORM ladder: target = old(BST scaled) so 4k-13≈REFORM10 preserved
ref = [(md5,v) for md5,v in items if "reform" in ((v.get("title") or "")+(v.get("version") or "")).lower()
       and re.search(r"~\s*[0-9]+(?:st|nd|rd|th)\s*~",(v.get("version") or ""))]
reform_pairs = [(0.4*zb(v["old"])+0.6*zm(v["mm"]), v["old"]) for _,v in ref]
p = V2.freeze_params(BST, MM, reform_pairs)
print("冻结参数: mb=%.3f sb=%.3f mm=%.3f sm=%.3f A=%.3f B=%.3f"%(p["mb"],p["sb"],p["mm"],p["sm"],p["A"],p["B"]))

# 每图 BSTv2 + 桶 + 名
def old_name(b):  # 旧命名 4k-XX.X
    return "4k-%04.1f" % (math.floor(b*2)/2.0)
recs = []
for md5, v in items:
    b2 = V2.bstv2(v["old"], v["mm"], p)
    bk = V2.bucket_of(b2)
    recs.append({"md5":md5,"title":v.get("title"),"version":v.get("version"),"type":v.get("type"),
                 "bst":v["old"],"mm":v["mm"],"bstv2":b2,"bucket":bk,"name":V2.bucket_name(bk),
                 "old_bucket":math.floor(v["old"]*2)/2.0,"old_name":old_name(v["old"])})

# --- 手工夹从 realm 读(实时真相, 而非旧 db) ---
import realm_engine
OSU = os.path.dirname(DELIVER)   # .../12_osu
_rr = realm_engine.Realm(os.path.join(OSU, "client.realm"))
preserved = [(c["name"], c["hashes"]) for c in _rr.collections()
             if not c["name"].startswith("4k-") and not c["name"].startswith("[4k]")]
print("保留的手工夹(来自 realm):", [(n, len(h)) for n, h in preserved] or "(无)")

# --- 写新 collection.db ---
buckets={}
for r in recs: buckets.setdefault(r["name"],[]).append(r["md5"])
names_sorted=sorted(buckets, key=V2.name_value)
cols=[(n,buckets[n]) for n in names_sorted]+preserved
def wstr(buf,s):
    bb=s.encode("utf-8"); buf.append(b"\x0b"); n=len(bb); out=bytearray()
    while True:
        x=n&0x7f; n>>=7
        out.append(x|0x80 if n else x)
        if not n: break
    buf.append(bytes(out)); buf.append(bb)
buf=[struct.pack("<i",20240101), struct.pack("<i",len(cols))]
for nm,hs in cols:
    wstr(buf,nm); buf.append(struct.pack("<i",len(hs)))
    for h in hs: wstr(buf,h)
blob=b"".join(buf)
if os.path.exists(OLD_DB) and not os.path.exists(OLD_DB+".bak_v1"): shutil.copy2(OLD_DB, OLD_DB+".bak_v1")
open(OLD_DB,"wb").write(blob)
shutil.copy2(OLD_DB, os.path.join(HERE,"collection.db"))
print("写 collection.db: %d 收藏夹(%d 难度桶 + %d 手工夹), %d 成员, %d 字节"%(
    len(cols),len(buckets),len(preserved),sum(len(h) for _,h in cols),len(blob)))

# --- 变更数据 ---
import collections as C
dist=C.Counter(r["name"] for r in recs)
moved=sum(1 for r in recs if r["bucket"]!=r["old_bucket"])
big=sum(1 for r in recs if abs(r["bucket"]-r["old_bucket"])>=1.0)
out={"params":p,"n":len(recs),"recs":recs,
     "dist":[(n,dist[n]) for n in names_sorted],
     "moved":moved,"moved_pct":100*moved/len(recs),"big":big}
json.dump(out, open(os.path.join(HERE,"regrade_data.json"),"w"))
print("动桶: %d/%d (%.0f%%), 动>=1整桶 %d"%(moved,len(recs),100*moved/len(recs),big))
print("新桶数: %d ; 范围 %s .. %s"%(len(buckets), names_sorted[0], names_sorted[-1]))
print("BSTv2 范围 %.2f .. %.2f"%(min(r["bstv2"] for r in recs), max(r["bstv2"] for r in recs)))
print("dumped regrade_data.json ; 旧 db 备份 -> 4k_collections.db.bak_v1")
