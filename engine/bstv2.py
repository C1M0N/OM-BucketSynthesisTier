# -*- coding: utf-8 -*-
"""BSTv2 = anchor(0.4*z(BST) + 0.6*z(MM))  —— BST(复合scaled) 与 MM(Map Minus) 的融合定级。
冻结参数(z 标准化 + REFORM 全阶梯锚定)使单图分值稳定、不随库增减漂移。
命名: [4k] 前缀;0..9 阿拉伯, 10..18 罗马 X..XVIII, 各带 '+' 半桶;>=19 -> Z(避免 XIX 破坏字典序)。"""
import math, json, os

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARAM = os.path.join(_HERE, "bstv2_params.json")

# 冻结参数(由 freeze_params() 写出;缺失时回退到内置默认)
_DEFAULT = {"mb": 0.0, "sb": 1.0, "mm": 0.0, "sm": 1.0, "A": 0.0, "B": 1.0}

def load_params():
    try:
        with open(_PARAM, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(_DEFAULT)

def bstv2(bst, mm, p=None):
    """融合分值(scaled 量纲)。bst=复合scaled, mm=Map Minus overall。"""
    if p is None: p = load_params()
    zb = (bst - p["mb"]) / (p["sb"] or 1.0)
    zm = (mm - p["mm"]) / (p["sm"] or 1.0)
    zf = 0.4 * zb + 0.6 * zm
    return p["A"] + p["B"] * zf

def bucket_of(x):
    return math.floor(x * 2) / 2.0

_ROMAN = {10: "X", 11: "XI", 12: "XII", 13: "XIII", 14: "XIV",
          15: "XV", 16: "XVI", 17: "XVII", 18: "XVIII"}

def bucket_name(b):
    """0.5 宽桶 -> [4k] 命名。"""
    if b < 0: b = 0.0
    i = int(math.floor(b))
    half = (b - i) >= 0.5
    if i >= 19:
        return "[4k]Z"
    core = str(i) if i <= 9 else _ROMAN[i]
    return "[4k]" + core + ("+" if half else "")

def name_value(name):
    """逆映射: [4k] 名 -> 桶下界数值(供报告排序)。"""
    s = name.replace("[4k]", "")
    if s == "Z": return 19.0
    half = s.endswith("+")
    if half: s = s[:-1]
    inv = {v: k for k, v in _ROMAN.items()}
    base = inv.get(s, None)
    if base is None:
        try: base = int(s)
        except Exception: base = 0
    return base + (0.5 if half else 0.0)

def freeze_params(bst_list, mm_list, reform_pairs):
    """从当前库冻结参数。reform_pairs=[(bstv2_z, target_scaled)] 用于锚定。"""
    def mean(x): return sum(x) / len(x)
    def sd(x):
        m = mean(x); return math.sqrt(sum((v - m) ** 2 for v in x) / len(x))
    mb, sb = mean(bst_list), sd(bst_list)
    mm_, sm = mean(mm_list), sd(mm_list)
    # 锚: target ≈ A + B * zfuse
    zf = [zf for zf, _ in reform_pairs]; tg = [t for _, t in reform_pairs]
    mzf, mtg = mean(zf), mean(tg)
    B = sum((a - mzf) * (c - mtg) for a, c in zip(zf, tg)) / sum((a - mzf) ** 2 for a in zf)
    A = mtg - B * mzf
    p = {"mb": mb, "sb": sb, "mm": mm_, "sm": sm, "A": A, "B": B}
    with open(_PARAM, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=1)
    return p
