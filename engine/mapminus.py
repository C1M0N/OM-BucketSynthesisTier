# -*- coding: utf-8 -*-
"""Map Minus v6.1 (yumu-bot SkillMania6) faithful Python port.
Computes 6 skill dims (RC/ST/SP/LN/CO/PR), 13 NoteType bases, overall rating, RF/UJ dan
directly from a 4K .osu file. Ported from yumu-bot model/skill/SkillMania6.kt + Skill6 + Dan.
"""
import math, sys

E = math.e
frac16 = 1000/48.0; frac12 = 1000/36.0; frac8 = 1000/24.0; frac6 = 1000/18.0
frac4 = 1000/12.0;  frac3 = 1000/9.0;   frac2 = 1000/6.0;  frac1 = 1000/3.0
CALC_UNIT = 5000
B_CONST = 0.176
K_CONST = 0.5 / math.log(1.0 + B_CONST)
FATIGUE_HL = 20.0; BURST_HL = 2.0

# NoteType indices
STREAM,BRACKET,JACK,FATIGUE,TRILL,BURST,RELEASE,SHIELD,REVERSE_SHIELD,HAND_LOCK,OVERLAP,GRACE,DELAYED_TAIL = range(13)
EVAL = [
    (1.461e0,5.878e-1),(2.993e0,5.712e-1),(2.188e0,5.928e-1),  # S B J
    (1.310e-2,9.376e-1),                                       # F
    (1.593e0,3.964e-1),(1.307e-1,8.938e-1),                    # T U
    (4.384e0,4.746e-1),(4.466e0,4.170e-1),(3.596e0,5.749e-1),  # R E V
    (3.264e0,3.434e-1),(5.536e0,3.438e-1),                     # H O
    (1.423e0,6.051e-1),(4.510e0,4.033e-1),                     # G Y
]
ABBR = ["RC","ST","SP","LN","CO","PR"]

# fingers: THUMB0 INDEX1 MIDDLE2 RING3 PINKY4 ; hands 'L'/'R'/'B'
_GEST = [1.0, 1.1,1.0, 1.1,1.0,1.0, 1.2,1.2,1.4,1.0, 1.4,1.2,1.6,1.8,1.0]
def gesture_bonus(f1,f2):
    i=max(f1,f2); j=min(f1,f2); return _GEST[(i*(i+1))//2 + j]
def hand_punish(h1,h2):
    return 1.0 if (h1==h2 and h1!='B') else 0.5

def inverse(t, standard=frac2, mx=frac1, mn=frac16):
    x=abs(t)
    if x<=mn: return standard/mn
    if x<=mx: return standard/x
    return 0.0
def approach(t, standard):
    return 1.0 - math.exp(-2.0*abs(t/standard))
def exponent(t, standard=frac8, mx=frac3):
    if standard<=0.0 or not (0.0 <= t <= mx): return 0.0
    x=abs(t/standard)
    return (E*x/math.exp(x))**2.0
def square(x,a=1.0,b=1.0,c=1.0,d=0.0):
    base=c*x
    if base<0: return d
    return a*(base**b)+d
def sort_and_sum(lst):
    s=sorted(lst, reverse=True); n=len(s)
    if n==1: return s[0]
    if n==2: return 0.7*s[0]+0.3*s[1]
    if n==3: return 0.6*s[0]+0.3*s[1]+0.1*s[2]
    if n==4: return 0.4*s[0]+0.3*s[1]+0.2*s[2]+0.1*s[2]
    return s[0] if s else 0.0
def aggregate(lst, decay=0.85):
    s=sorted([v for v in lst if v>0], reverse=True)
    total=0.0; w=1.0
    for v in s:
        total+=v*w; w*=decay
        if w<0.001: break
    return total
def chord_bonus(chord, total_key):
    if not (1<=chord<=total_key): return 0.0
    cv=math.log(chord+B_CONST)*K_CONST
    return cv*0.8 if (chord==total_key and total_key>=4) else cv
def map_skill_rating(skills):
    s=sorted(skills[:6], reverse=True)
    def g(i): return s[i] if i<len(s) else 0.0
    return 0.6*g(1)+0.4*g(2)+0.2*g(3)

# --- .osu parse (mania) ---
def parse_osu(text):
    cs=4.0; section=""; hits=[]
    for raw in text.split("\n"):
        line=raw.strip()
        if line.startswith("[") and line.endswith("]"): section=line; continue
        if not line: continue
        if section=="[Difficulty]":
            if line.startswith("CircleSize"):
                try: cs=float(line.split(":",1)[1].strip())
                except: pass
        elif section=="[HitObjects]":
            p=line.split(",")
            if len(p)<4: continue
            try:
                x=int(p[0]); t=int(p[2]); ty=int(p[3])
            except: continue
            if ty & 1:        # CIRCLE
                hits.append((x, t, t, True))
            elif ty & 128:    # LONGNOTE
                end=t
                if len(p)>=6:
                    try: end=int(p[5].split(":")[0])
                    except: end=t
                hits.append((x, t, end, False))
            # sliders/spinners ignored (not in mania)
    return cs, hits

def get_column(x, key):
    if key<=0: return 0
    return max(0, min(key-1, int(x*key/512.0)))

PLAYSTYLE_4K = [(2,'L'),(1,'L'),(1,'R'),(2,'R')]  # col0..3: (finger,hand)

class Act:
    __slots__=("finger","hand","column","start","end","ln")
    def __init__(s,finger,hand,column,start,end,ln):
        s.finger=finger; s.hand=hand; s.column=column; s.start=start; s.end=end; s.ln=ln

def compute(text):
    cs, hits = parse_osu(text)
    total_key=int(cs)
    if total_key!=4:
        playstyle=[(1,'B')]*total_key
    else:
        playstyle=PLAYSTYLE_4K
    # build hitobjects with column + RICE/LN type
    objs=[]
    for (x,start,end,is_circle) in hits:
        col=get_column(x, total_key)
        objs.append((col,start,end,is_circle))
    # sort by start then column (stable: sortBy column then sortBy start)
    objs.sort(key=lambda o:(o[1], o[0]))
    # objectsToActions
    actions=[]; batch=[]
    for o in objs:
        if not batch:
            batch=[o]; continue
        conflict = any(b[0]==o[0] for b in batch)
        timeout = (o[1] - batch[-1][1]) > frac8
        if conflict or timeout:
            actions.append(batch_to_action(batch, playstyle)); batch=[]
        batch.append(o)
    if batch: actions.append(batch_to_action(batch, playstyle))
    # actionsToNoteData (zipWithNext)
    legacy=[]; burst_b=0.0; fat_b=0.0; data=[]
    for i in range(len(actions)-1):
        it=actions[i]; after=actions[i+1]
        min_start=min((a.start for a in it), default=0)
        colset=set(a.column for a in it)
        legacy=[l for l in legacy if not (l.end < (min_start+frac16) or l.column in colset)]
        nd=calculate(it, legacy, after, burst_b, fat_b, total_key, min_start)
        legacy=legacy+[a for a in it if a.ln]
        burst_b=nd['v'][BURST]; fat_b=nd['v'][FATIGUE]
        data.append(nd)
    # grouping (5000 windows)
    group=grouping(data)
    # bases
    bases=note_data_to_sub(group)
    skills=[sort_and_sum([bases[0],bases[1],bases[2]]), bases[3],
            sort_and_sum([bases[4],bases[5]]), sort_and_sum([bases[6],bases[7],bases[8]]),
            sort_and_sum([bases[9],bases[10]]), sort_and_sum([bases[11],bases[12]])]
    rating=map_skill_rating(skills)
    dan=dan_from_beatmap(skills, cs)
    return {"cs":cs,"bases":bases,"skills":skills,"rating":rating,"dan":dan,
            "names":ABBR}

def batch_to_action(batch, playstyle):
    out=[]
    for (col,start,end,is_circle) in batch:
        ps=playstyle[col]
        ln = (not is_circle) and ((end-start) > frac12)
        et = end if ln else start
        out.append(Act(ps[0], ps[1], col, start, et, ln))
    return out

def newdata():
    return {'v':[0.0]*13, 't':0}
def add(d, o):
    for i in range(13): d['v'][i]+=o['v'][i]

def calculate(action, holdings, after, burst_b, fat_b, total_key, _minstart):
    d=newdata()
    after_max = max((a.start for a in after), default=0)
    start_max = max((a.start for a in action), default=0)
    start_min = min((a.start for a in action), default=0)
    chord=len(action)
    cb=chord_bonus(chord, total_key)
    for it in action:
        leftL=[a for a in after if a.column<it.column]
        leftA=max(leftL, key=lambda a:a.column) if leftL else None
        itA=next((a for a in after if a.column==it.column), None)
        rightL=[a for a in after if a.column>it.column]
        rightA=min(rightL, key=lambda a:a.column) if rightL else None
        for h in holdings:
            add(d, calc_aside_release(it, h))
        if itA is not None:
            add(d, calc_after(it, itA))
        else:
            for la in leftL: add(d, calc_aside_hit(it, la, len(action), len(leftL), cb, total_key))
            for ra in rightL: add(d, calc_aside_hit(it, ra, len(action), len(rightL), cb, total_key))
            if leftA is not None and rightA is not None:
                add(d, calc_both_side(it, leftA, rightA))
    for a,b in zip(action, action[1:]):
        if a.ln or b.ln: add(d, calc_aside_release(a,b))
    grace_delta=start_max-start_min
    if grace_delta>=frac8 and chord>1:
        for a,b in zip(action, action[1:]):
            d['v'][GRACE]+=exponent(a.start-b.start, frac8, frac4)
    delta=after_max-start_max
    d['v'][FATIGUE]=fat_b*(0.5**((delta/1000.0)/FATIGUE_HL))+cb
    d['v'][BURST]=burst_b*(0.5**((delta/1000.0)/BURST_HL))+cb
    d['t']=start_min
    return d

def calc_after(it, after):
    d=newdata()
    if not it.ln:
        if not after.ln:
            d['v'][JACK]+=inverse(after.start-it.start, frac2,frac1,frac16)
        else:
            d['v'][REVERSE_SHIELD]+=inverse(after.start-it.start, frac2,frac1,frac16)
    else:
        d['v'][SHIELD]+=inverse(after.start-it.end, frac4,frac1,frac16)
    return d

def calc_aside_hit(it, aside, it_chord, aside_chord, cb, total_key):
    d=newdata()
    bonus=gesture_bonus(it.finger, aside.finger)
    punish=hand_punish(it.hand, aside.hand)
    if it.hand!=aside.hand and (it_chord+aside_chord>2 or total_key<4):
        d['v'][TRILL]+=cb*exponent(aside.start-it.start, frac4, frac1)
    else:
        d['v'][STREAM]+=bonus*punish*exponent(aside.start-it.start, frac4, frac1)
    d['v'][GRACE]+=exponent(aside.start-it.start, frac8, frac4)
    return d

def calc_aside_release(it, aside):
    d=newdata()
    if not aside.ln: return d
    bonus=gesture_bonus(it.finger, aside.finger)
    if not it.ln:
        endDelta=aside.end-it.start; startDelta=it.start-aside.start
        isIn = endDelta>0 and startDelta>0
        dl=math.sqrt(approach(endDelta,frac2)*approach(startDelta,frac2))
        d['v'][HAND_LOCK]+= (bonus*dl) if isIn else 0.0
    else:
        if abs(aside.column-it.column)==1 and it.hand==aside.hand:
            pressDelta=abs(aside.start-it.start); releaseDelta=abs(aside.end-it.end)
            changeDelta=min(it.end,aside.end)-max(it.start,aside.start)
            dl=0 if changeDelta<=0 else int((pressDelta*releaseDelta*changeDelta*1.0)**(1.0/3.0))
            d['v'][OVERLAP]+=exponent(dl, frac2, 3*frac2)
        d['v'][RELEASE]+=exponent(aside.end-it.end, frac4, frac1)
        d['v'][DELAYED_TAIL]+=exponent(aside.end-it.end, frac6, frac3)
    return d

def calc_both_side(it, left, right):
    d=newdata()
    mn=min(left.start, right.start)
    if mn > it.start+frac16:
        d['v'][BRACKET]+=exponent(mn-it.start, frac4, frac1)
    return d

def grouping(data):
    if not data: return []
    times=[d['t'] for d in data]
    start=min(times); end=max(times)
    gm={}
    for d in data: gm.setdefault(d['t']//CALC_UNIT, []).append(d)
    res=[]
    for w in range(start//CALC_UNIT, end//CALC_UNIT+1):
        notes=gm.get(w)
        nd=newdata(); nd['t']=w*CALC_UNIT
        if notes:
            for n in notes:
                for i in range(13): nd['v'][i]+=n['v'][i]
            c=float(len(notes))
            for i in range(13): nd['v'][i]/=c
        res.append(nd)
    return res

def note_data_to_sub(group):
    bases=[]
    for idx in range(13):
        a,b=EVAL[idx]
        if idx==BURST:
            val=max((g['v'][BURST] for g in group), default=0.0)
            bases.append(square(val,a,b))
        elif idx==FATIGUE:
            val=max((g['v'][FATIGUE] for g in group), default=0.0)
            bases.append(square(val,a,b))
        else:
            agg=aggregate([g['v'][idx] for g in group])
            bases.append(square(agg,a,b))
    return bases

# --- Dan ---
REFORM=dict(name="reform",
    boundary=[0.0,1.5,2.5,3.5,3.9,4.2,4.5,4.8,5.1,5.4,5.7,6.0,6.5,7.0,7.5,8.0,8.5,9.0,9.5,10.0,10.8,11.6],
    grade=["-",".1",".2",".3","1","2","3","4","5","6","7","8","9","10","A","B","G","D","E","Z","H","S"],
    max=12.0, offset=-3, use=[1,2,3])
UNDERJOY=dict(name="underjoy",
    boundary=[0.0,4.0,4.5,5.0,5.5,6.0,6.5,7.0,7.4,7.7,8.0,8.3,8.6,8.8],
    grade=["-","1","2","3","4","5","6","7","8","9","10","11","12","13"],
    max=9.0, offset=0, use=[4,5,6])
def dan_result(skills, dan):
    sub=sorted([skills[i-1] for i in dan["use"] if i-1<len(skills)], reverse=True)
    def g(i): return sub[i] if i<len(sub) else 0.0
    s=0.5*g(0)+0.3*g(1)+0.2*g(2)
    bd=dan["boundary"]; gr=dan["grade"]
    idx=max(0, max((i for i,bv in enumerate(bd) if s>=bv), default=0))
    base=idx+dan["offset"]
    if s>=(dan["max"] if dan["max"] is not None else 1e18):
        return {"name":dan["name"],"level":len(bd)+dan["offset"],"grade":gr[-1]+"+"}
    if idx>=len(bd)-1:
        return {"name":dan["name"],"level":float(base),"grade":gr[-1]}
    lo=bd[idx]; hi=bd[idx+1]; frac=(s-lo)/(hi-lo)
    plus="+" if 0.5<=frac<1.0 else ""
    grade=gr[idx] if idx==0 else gr[idx]+plus
    return {"name":dan["name"],"level":base+frac,"grade":grade}
def dan_from_beatmap(skills, cs):
    if (cs or 4.0)<5.5:
        return {"reform":dan_result(skills,REFORM),"underjoy":dan_result(skills,UNDERJOY)}
    return {}

if __name__=="__main__":
    text=open(sys.argv[1],encoding="utf-8",errors="replace").read()
    r=compute(text)
    NT=["S","B","J","F","T","U","R","E","V","H","O","G","Y"]
    print("cs",r["cs"])
    print("bases:", "  ".join("%s=%.2f"%(NT[i],r["bases"][i]) for i in range(13)))
    print("skills:", "  ".join("%s=%.2f"%(ABBR[i],r["skills"][i]) for i in range(6)))
    print("overall rating: %.2f"%r["rating"])
    for k,v in r["dan"].items(): print("dan %s: %s (level %.2f)"%(k, v["grade"], v["level"]))
