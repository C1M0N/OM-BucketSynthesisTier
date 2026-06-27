#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
make_report.py — osu!mania 4K 难度分级报告生成器
================================================

读取 report_data.json，生成所有图表（matplotlib），写出 .tex，使用 XeLaTeX
在 /tmp 中编译，并把最终 report.pdf 复制到输出路径。

特点
----
* 幂等 / 可重复运行：主工作流每次跑完都可直接调用本脚本。
* 不重新计算任何数据，只做可视化与排版。
* 可选命令行参数：
      python3 make_report.py [input_json] [output_pdf]
  默认：
      input  = /sessions/.../4k_engine/report_data.json
      output = /sessions/.../4k_engine/report.pdf

环境要点
--------
* matplotlib 中文字体：系统只有 Noto CJK 的 .ttc 合集，matplotlib 无法
  直接按族名注册 .ttc，因此本脚本用 fontTools 从 .ttc 中抽出 “Noto Sans CJK SC”
  子字体为独立 .otf，再 addfont 注册 —— 实测零缺字（tofu）。
* XeLaTeX：本机缺少 ctex / xeCJK 依赖（ctexhook.sty 缺失），因此改用纯
  fontspec：\setmainfont{Noto Serif CJK SC}[Script=CJK]，并用 XeTeX 原生
  断行原语处理中文折行 —— 无需任何额外宏包。
* 输出挂载点对 unlink/rename 受限，会导致 LaTeX 辅助文件 churn 失败，
  因此一律在 /tmp/texbuild 中编译，最后只把 PDF 复制出去。
"""

import sys
import os
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

# ----------------------------------------------------------------------------
# 路径与常量
# ----------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import bstv2  # BSTv2 [4k] 命名解析
DEFAULT_INPUT = os.path.join(_HERE, "report_data.json")
DEFAULT_OUTPUT = os.path.join(_HERE, "report.pdf")

BUILD_DIR = os.environ.get("OSU4K_TEXBUILD") or os.path.join(tempfile.gettempdir(), "osu4k_texbuild")

# ---- 中文字体（本地 Windows：直接用系统自带 CJK 字体，无需 Noto）----
# matplotlib：用一个 TTF 文件直接 addfont（DengXian 是单一 TTF，省去从 .ttc 抽取的步骤）。
# XeLaTeX：用 fontspec 的 Path= 按文件名解析，不依赖系统字体数据库 —— 最稳、跨机器一致。
_WINFONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
MPL_FONT_FILE = os.environ.get("OSU4K_MPL_FONT") or os.path.join(_WINFONTS, "Deng.ttf")  # DengXian 等线
TEX_FONT_PATH = _WINFONTS.replace("\\", "/").rstrip("/") + "/"
TEX_MAIN_FILE = "simsun.ttc"    # 宋体（衬线，正文）
TEX_MAIN_BOLD = "simsunb.ttf"
TEX_SANS_FILE = "Deng.ttf"      # DengXian（无衬线，标题）
TEX_SANS_BOLD = "Dengb.ttf"

# 谱面类型调色板
PALETTE = {
    "RC": "#378ADD",
    "LN": "#7F77DD",
    "HB": "#1D9E75",
    "MIX": "#EF9F27",
    "Vibro": "#E24B4A",
}
TYPE_ORDER = ["RC", "LN", "HB", "MIX", "Vibro"]

# MM（Map Minus）6 技能维度（来自 yumu SkillMania6）。这是 /mm 输出右侧那“一大串”的
# 技能向量；现在它在桶分级报告中有了一席之地。配色采用 yumu 的维度配色（建议五）。
SKILL_ORDER = ["RC", "ST", "SP", "LN", "CO", "PR"]
SKILL_COLORS = {
    "RC": "#22AC38",   # 绿：米流（连打/括号/叠键）
    "ST": "#FF9800",   # 橙：耐力（续航）
    "SP": "#D32F2F",   # 红：速度（颤音/爆发）
    "LN": "#00A0E9",   # 蓝：长条（放手/盾/反盾）
    "CO": "#9C27B0",   # 紫：协调（手锁/重叠）
    "PR": "#C9A100",   # 金：精准（倚音/延迟尾）
}
SKILL_LABEL = {  # 维度全称（中文）
    "RC": "米流", "ST": "耐力", "SP": "速度",
    "LN": "面条", "CO": "协调", "PR": "精准",
}
SKILL_CH = {  # 单字（表格“主导维度”列用，避免与谱型 RC/LN 混淆）
    "RC": "米", "ST": "耐", "SP": "速",
    "LN": "面", "CO": "协", "PR": "精",
}

# MinaCalc（Etterna MSD）8 技能集 —— 即 BST 里 MSD 那一项的细分维度，数据已在缓存。
MSD_ORDER = ["Overall", "Stream", "Jumpstream", "Handstream",
             "Stamina", "JackSpeed", "Chordjack", "Technical"]
MSD_SHORT = {
    "Overall": "OV", "Stream": "Str", "Jumpstream": "JS", "Handstream": "HS",
    "Stamina": "Stam", "JackSpeed": "Jack", "Chordjack": "CJ", "Technical": "Tech",
}
MSD_LABEL = {
    "Overall": "综合", "Stream": "单押流", "Jumpstream": "双押流", "Handstream": "三押流",
    "Stamina": "耐力", "JackSpeed": "叠键速", "Chordjack": "和弦叠", "Technical": "技术",
}
MSD_COLORS = {
    "Overall": "#444444", "Stream": "#2E7D32", "Jumpstream": "#1565C0",
    "Handstream": "#6A1B9A", "Stamina": "#EF6C00", "JackSpeed": "#C62828",
    "Chordjack": "#00838F", "Technical": "#AD1457",
}


def nice_step(lo, hi, target=6):
    """整洁刻度步长（借鉴 yumu LineChartGrid 的 10^n 取整思路）。
    返回 (step, lo_aligned, hi_aligned)：使刻度落在 1/2/5×10^k 上。"""
    import math
    span = max(hi - lo, 1e-9)
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            step = m * mag
            break
    else:
        step = 10 * mag
    lo_a = math.floor(lo / step) * step
    hi_a = math.ceil(hi / step) * step
    return step, lo_a, hi_a


def bucket_ticklabels(names):
    """[4k]X+ -> 两行横排标签：数字在上，'+' 在下（统一阿拉伯数字）。"""
    import math
    out = []
    for nm in names:
        if nm.endswith("ℵ"):          # 顶档：用数学符号渲染（matplotlib mathtext）
            out.append(r"$\aleph$")
            continue
        try:
            v = bstv2.name_value(nm)
        except Exception:
            out.append(nm)
            continue
        base = int(math.floor(v + 1e-9))
        plus = "+" if (v - base) >= 0.5 - 1e-9 else ""
        out.append(f"{base}\n{plus}")
    return out


def _acc_cmap():
    """准确率配色（输入 = 误差率/20 ∈ [0,1]）：0% = 白、5%(S) = 绿、10% = 橙、>=20% = 红。"""
    from matplotlib.colors import LinearSegmentedColormap
    return LinearSegmentedColormap.from_list(
        "wgor", [(0.0, "#FFFFFF"), (0.25, "#22AC38"), (0.5, "#FF9800"), (1.0, "#E24B4A")])


def _err_color(err, cmap=None):
    """误差率(%) -> 颜色；无成绩(None) -> 灰。"""
    cmap = cmap or _acc_cmap()
    if err is None:
        return "#cccccc"
    e = max(0.0, min(20.0, err))
    return cmap(e / 20.0)


def _bucket_err2s(b):
    """该桶“已通关谱最佳成绩”误差率的 +2σ（均值 + 2 标准差）。
    比均值更严：只有整桶最佳成绩都稳定低误差，+2σ 才低（偏绿）。无成绩 -> None。"""
    ab = b.get("accBest") or []
    if not ab:
        return None
    errs = [100.0 - a * 100.0 for a in ab]
    m = sum(errs) / len(errs)
    if len(errs) >= 2:
        sd = (sum((e - m) ** 2 for e in errs) / (len(errs) - 1)) ** 0.5
    else:
        sd = 0.0
    return m + 2.0 * sd


def _heatmap_with_strip(out_path, names, M, row_labels, row_colors, strip_vals, cmap, title,
                        strip_ylabel="已通关\n占比%", strip_colors=None):
    """通用：顶部条（已通关占比，颜色可按准确率映射）+ 下方按技能行归一化的热力图。"""
    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib.gridspec import GridSpec

    nb = len(names)
    nrow = M.shape[0]
    Mn = np.zeros_like(M)
    for i in range(nrow):
        rmax = M[i].max()
        Mn[i] = M[i] / rmax if rmax > 0 else M[i]

    fig = plt.figure(figsize=(11.8, 1.6 + 0.46 * nrow))
    gs = GridSpec(2, 1, height_ratios=[1, max(3.2, nrow)], hspace=0.08)
    axt = fig.add_subplot(gs[0])
    axh = fig.add_subplot(gs[1], sharex=axt)

    axt.bar(np.arange(nb), strip_vals, width=0.9,
            color=(strip_colors if strip_colors is not None else "#1D9E75"),
            edgecolor="#bbbbbb", linewidth=0.3, alpha=0.95)
    axt.set_ylim(0, 100)          # 绝对纵坐标：0%~100%
    axt.set_yticks([0, 50, 100])
    axt.set_ylabel(strip_ylabel, fontsize=8, rotation=0, ha="right", va="center")
    axt.tick_params(axis="x", labelbottom=False, bottom=False)
    axt.tick_params(axis="y", labelsize=7)
    for sp in ("top", "right"):
        axt.spines[sp].set_visible(False)
    axt.set_title(title, fontsize=13, pad=8)

    im = axh.imshow(Mn, aspect="auto", cmap=cmap, vmin=0, vmax=1)
    axh.set_yticks(range(nrow))
    axh.set_yticklabels(row_labels, fontsize=8.5)
    for lab, c in zip(axh.get_yticklabels(), row_colors):
        lab.set_color(c)
    axh.set_xticks(range(nb))
    axh.set_xticklabels(bucket_ticklabels(names), fontsize=7.5)
    axh.set_xlabel("难度桶 (bucket)", fontsize=11)

    cbar = fig.colorbar(im, ax=[axt, axh], fraction=0.025, pad=0.01)
    cbar.set_label("相对强度（行归一化）", fontsize=9)
    cbar.ax.tick_params(labelsize=8)

    fig.savefig(out_path, dpi=170, bbox_inches="tight")
    plt.close(fig)

# 游玩先后 6 段红->绿渐变（#e24b4a -> #1d9e75）
def _ramp_6():
    import numpy as np
    c0 = (0xE2 / 255, 0x4B / 255, 0x4A / 255)  # red
    c1 = (0x1D / 255, 0x9E / 255, 0x75 / 255)  # green
    out = []
    for i in range(6):
        t = i / 5.0
        out.append(tuple(c0[k] + (c1[k] - c0[k]) * t for k in range(3)))
    return out


# ----------------------------------------------------------------------------
# 字体准备（matplotlib）
# ----------------------------------------------------------------------------
def prepare_matplotlib_font():
    """注册一个本地中文 TTF 到 matplotlib（DengXian，直接 addfont，无需从 .ttc 抽取）。
    返回注册后的字体族名（用于 rcParams['font.family']）。"""
    from matplotlib import font_manager as fm, rcParams
    if not os.path.exists(MPL_FONT_FILE):
        raise RuntimeError("缺少中文字体文件：%s" % MPL_FONT_FILE)
    fm.fontManager.addfont(MPL_FONT_FILE)
    fam_name = fm.FontProperties(fname=MPL_FONT_FILE).get_name()
    # 字体回退链：中文取自 DengXian，DengXian 缺的字形（如 U+2212 减号、部分拉丁/符号）
    # 由 matplotlib 自带的 DejaVu Sans 逐字回退补齐 —— 彻底消除 tofu。
    rcParams["font.family"] = [fam_name, "DejaVu Sans"]
    rcParams["axes.unicode_minus"] = False  # 负号优先用 ASCII '-'

    # 自检：渲染一个中文标签，捕获缺字告警；若有 tofu 立即报错
    import warnings
    import matplotlib.pyplot as plt
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        fig, ax = plt.subplots(figsize=(2, 1))
        ax.text(0.5, 0.5, "难度分布 准确率 复合 -1.5", ha="center", va="center")
        ax.axis("off")
        fig.canvas.draw()
        plt.close(fig)
        bad = [str(x.message) for x in w if "missing from font" in str(x.message)]
    if bad:
        raise RuntimeError("matplotlib 中文字体存在缺字（tofu）：%s" % bad[:3])
    return fam_name


# ----------------------------------------------------------------------------
# 工具：数据派生
# ----------------------------------------------------------------------------
def compute_type_totals(buckets):
    """对 buckets[].types 求和得到全库各类型谱数。"""
    tot = {t: 0 for t in TYPE_ORDER}
    for b in buckets:
        for t in TYPE_ORDER:
            tot[t] += int(b["types"].get(t, 0))
    return tot


# ----------------------------------------------------------------------------
# 图表生成
# ----------------------------------------------------------------------------
def fig1_distribution(data, out_path):
    """Fig 1：按类型堆叠的难度分布直方图。
    avgSr 以粉色一位小数标注于柱顶；avgAcc 用对数误差率折线（深红，与 Fig7 同口径）；
    底部叠加“通关覆盖率”条（y 向下），高度 = 该桶已通关谱占比，颜色按准确率绿->红。"""
    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib.gridspec import GridSpec
    from matplotlib.lines import Line2D

    buckets = data["buckets"]
    names = [b["name"] for b in buckets]
    nb = len(buckets)
    x = np.arange(nb)
    counts_by_type = {t: np.array([b["types"].get(t, 0) for b in buckets], dtype=float)
                      for t in TYPE_ORDER}
    totals = np.array([b["count"] for b in buckets], dtype=float)
    avg_sr = [b.get("avgSr") for b in buckets]
    avg_acc = [b.get("avgAcc") for b in buckets]   # 0..1 或 None
    clear_rate = np.array([(b.get("playedCount", 0) / b["count"]) if b.get("count") else 0.0
                           for b in buckets], dtype=float) * 100.0

    SR_COLOR = "#D4537E"     # 粉色：平均星数（柱顶数字）
    ACC_COLOR = "#8B0000"    # 深红：平均误差率折线

    fig = plt.figure(figsize=(11.6, 6.6))
    gs = GridSpec(2, 1, height_ratios=[5.0, 1.35], hspace=0.08)
    ax = fig.add_subplot(gs[0])
    axb = fig.add_subplot(gs[1], sharex=ax)

    # --- 堆叠柱 ---
    bottom = np.zeros(nb)
    for t in TYPE_ORDER:
        vals = counts_by_type[t]
        ax.bar(x, vals, bottom=bottom, width=0.82, color=PALETTE[t], label=t,
               edgecolor="white", linewidth=0.25, zorder=1)
        bottom += vals
    ymax = totals.max() if nb else 1
    ax.set_ylim(0, ymax * 1.20)
    ax.set_ylabel("谱数 (count)", fontsize=11)
    ax.set_title("难度分布（按谱面类型堆叠）", fontsize=14, pad=10)
    ax.grid(axis="y", linestyle=":", linewidth=0.5, alpha=0.6)
    ax.spines["top"].set_visible(False)
    ax.tick_params(axis="x", labelbottom=False)

    # --- avgSr：粉色一位小数，标在上下两部分中间的空隙（上图基线下方）---
    for xi, sr in zip(x, avg_sr):
        if sr is None:
            continue
        ax.annotate(f"{sr:.1f}", xy=(xi, 0), xytext=(0, -3), textcoords="offset points",
                    ha="center", va="top", fontsize=5.6, color=SR_COLOR,
                    clip_on=False, annotation_clip=False, zorder=6)

    # --- avgAcc：对数误差率折线（深红，右轴；与 Fig7 同口径）---
    ax_acc = ax.twinx()
    ax_acc.set_yscale("log")
    ax_acc.set_ylim(22, 0.3)
    yt = [0.5, 1, 2, 5, 10, 20]
    ax_acc.set_yticks(yt)
    ax_acc.set_yticklabels([f"{v:g}%" for v in yt])
    ax_acc.minorticks_off()
    ax_acc.set_ylabel("误差率 (100−acc，对数)", fontsize=10, color="#333333")
    ax_acc.tick_params(axis="y", labelcolor="#333333")
    ax_acc.spines["top"].set_visible(False)
    # 每桶误差率箱线图（黑色、细）：用 best-per-map 准确率换算误差率，展示分布而非单一均值
    box_data, box_pos = [], []
    for i, b in enumerate(buckets):
        ab = b.get("accBest") or []
        if not ab:
            continue
        box_data.append([max(100.0 - a * 100.0, 0.2) for a in ab])
        box_pos.append(i)
    if box_data:
        ax_acc.boxplot(
            box_data, positions=box_pos, widths=0.5, manage_ticks=False,
            whis=3.0,   # 极端离群值 extreme outlier：须 = Q±3·IQR（外栅栏），须外即离群点
            showfliers=True,
            flierprops=dict(marker="o", markersize=1.6, markerfacecolor="black",
                            markeredgecolor="none", alpha=0.6),
            boxprops=dict(color="black", linewidth=0.5),
            whiskerprops=dict(color="black", linewidth=0.5),
            capprops=dict(color="black", linewidth=0.5),
            medianprops=dict(color="black", linewidth=0.9))
    # S 线：误差 5%（= acc 95%）—— 只画线，不标字
    ax_acc.axhline(5.0, color="#FF2D2D", linestyle="--", linewidth=1.2, alpha=0.9, zorder=4)

    # --- 底部条：通关覆盖率（y 向下），按准确率绿->红上色 ---
    cmap = _acc_cmap()
    cols = [_err_color(_bucket_err2s(b), cmap) for b in buckets]
    axb.bar(x, clear_rate, width=0.82, color=cols, edgecolor="#bbbbbb", linewidth=0.3)
    axb.set_ylim(0, 100)          # 绝对纵坐标：0%~100%
    axb.set_yticks([0, 50, 100])
    axb.invert_yaxis()            # y 轴向下（0% 在顶、100% 在底）
    axb.set_ylabel("已通关\n占比 %", fontsize=8, rotation=0, ha="right", va="center")
    # 每桶通关条正下方：黑色=未完成曲目数（count-已通关；含 acc<80 与未游玩）；
    # 其下红色=已通关但未达 S（误差>5% 即 acc<95%）的成绩数。
    for xi, cr, b in zip(x, clear_rate, buckets):
        unfinished = int(b.get("count", 0)) - int(b.get("playedCount", 0))
        ab = b.get("accBest") or []
        not_s = sum(1 for a in ab if a < 0.95)
        yb = min(cr + 5, 86)
        axb.text(xi, yb, str(unfinished), ha="center", va="top", fontsize=5.0, color="black")
        if ab:
            axb.text(xi, min(yb + 7, 96), str(not_s), ha="center", va="top",
                     fontsize=5.0, color="#E03030")
    axb.set_xticks(x)
    axb.set_xticklabels(bucket_ticklabels(names), fontsize=7.5)
    axb.set_xlabel("难度桶 (bucket)", fontsize=11)
    axb.spines["bottom"].set_visible(False)
    axb.tick_params(axis="y", labelsize=7)
    axb.grid(axis="y", linestyle=":", linewidth=0.4, alpha=0.5)

    # --- 图例：类型柱 + 误差率折线 + avgSr 数字 + 覆盖率条 ---
    handles, labels = ax.get_legend_handles_labels()
    handles += [
        Line2D([0], [0], color="black", marker="s", markerfacecolor="none",
               markersize=6, linestyle="none"),
        Line2D([0], [0], color=SR_COLOR, marker="s", linestyle="none", markersize=7),
        plt.Rectangle((0, 0), 1, 1, color=cmap(0.15)),
    ]
    labels += ["每桶误差率箱线", "中间数字=平均星", "底部=通关率(色=准确率)"]
    ax.legend(handles, labels, title="图例", ncol=4, fontsize=7.8,
              title_fontsize=9, loc="upper right", frameon=True, framealpha=0.92)

    ax.set_xlim(-0.5, nb - 0.5)   # 收紧 x 轴：去掉 0 左侧与末桶右侧的空白

    fig.savefig(out_path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def fig_typetotals(data, out_path):
    """全库各类型谱数饼图。"""
    import matplotlib.pyplot as plt

    tot = compute_type_totals(data["buckets"])
    types = [t for t in TYPE_ORDER if tot[t] > 0]
    vals = [tot[t] for t in types]
    total = sum(vals) or 1
    colors = [PALETTE[t] for t in types]

    def _autopct(pct):
        return f"{pct:.1f}%" if pct >= 3.0 else ""

    fig, ax = plt.subplots(figsize=(6.6, 5.0))
    wedges, _texts, autotexts = ax.pie(
        vals, colors=colors, startangle=90, counterclock=False,
        autopct=_autopct, pctdistance=0.74,
        textprops=dict(color="white", fontsize=10, weight="bold"),
        wedgeprops=dict(edgecolor="white", linewidth=1.2))
    ax.axis("equal")
    ax.set_title(f"全库 4K 谱按类型分布（合计 {total}）", fontsize=13, pad=12)
    labels = [f"{t}   {v}  ({v / total * 100:.1f}%)" for t, v in zip(types, vals)]
    ax.legend(wedges, labels, title="谱型", loc="center left",
              bbox_to_anchor=(1.0, 0.5), fontsize=10, title_fontsize=11, frameon=False)
    fig.savefig(out_path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def fig_skill_radar(data, out_path):
    """技能维度雷达：全库均值 vs 已游玩均值（MM 6 技能 RC/ST/SP/LN/CO/PR）。
    把 /mm 输出右侧那“一大串”技能向量可视化 —— 维度体系在报告中的核心一席。"""
    import numpy as np
    import matplotlib.pyplot as plt

    prof = data.get("skillProfile") or {}
    lib = prof.get("library") or {}
    pl = prof.get("played") or {}
    labels = [f"{s}\n{SKILL_LABEL[s]}" for s in SKILL_ORDER]
    lib_v = [float(lib.get(s, 0.0)) for s in SKILL_ORDER]
    pl_v = [float(pl.get(s, 0.0)) for s in SKILL_ORDER]

    ang = np.linspace(0, 2 * np.pi, len(SKILL_ORDER), endpoint=False).tolist()
    ang += ang[:1]

    def close(v):
        return v + v[:1]

    fig, ax = plt.subplots(figsize=(6.4, 6.0), subplot_kw=dict(polar=True))
    ax.set_theta_offset(np.pi / 2)
    ax.set_theta_direction(-1)
    ax.set_xticks(ang[:-1])
    ax.set_xticklabels(labels, fontsize=10)

    vmax = max(lib_v + pl_v + [1.0])
    ax.set_ylim(0, vmax * 1.12)

    # 全库
    ax.plot(ang, close(lib_v), color="#888888", linewidth=1.8, label="全库均值")
    ax.fill(ang, close(lib_v), color="#888888", alpha=0.12)
    # 已游玩
    ax.plot(ang, close(pl_v), color="#1D9E75", linewidth=2.2, label="已游玩均值")
    ax.fill(ang, close(pl_v), color="#1D9E75", alpha=0.18)

    # 每个轴用其维度配色着色刻度标签端点
    for a, s, v in zip(ang[:-1], SKILL_ORDER, lib_v):
        ax.plot([a], [v], marker="o", markersize=5, color=SKILL_COLORS[s], zorder=5)

    ax.set_title("技能维度雷达：全库 vs 已游玩", fontsize=14, pad=22)
    ax.legend(loc="upper right", bbox_to_anchor=(1.18, 1.10), fontsize=9, frameon=True)
    ax.grid(True, linestyle=":", linewidth=0.6, alpha=0.7)
    fig.tight_layout()
    fig.savefig(out_path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def fig_skill_heatmap(data, out_path):
    """各难度桶的技能构成热力图（MM 6 维，按技能行归一化）+ 顶部已通关占比绿柱。"""
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap

    buckets = data["buckets"]
    names = [b["name"] for b in buckets]
    M = np.zeros((6, len(buckets)), dtype=float)
    for j, b in enumerate(buckets):
        sk = b.get("skills") or {}
        for i, s in enumerate(SKILL_ORDER):
            M[i, j] = float(sk.get(s, 0.0))
    clear = np.array([(b.get("playedCount", 0) / b["count"] * 100.0) if b.get("count") else 0.0
                      for b in buckets], dtype=float)
    acc_cm = _acc_cmap()
    strip_colors = [_err_color(_bucket_err2s(b), acc_cm) for b in buckets]
    cmap = LinearSegmentedColormap.from_list("yumu", ["#F4F6F8", "#1D6FB8", "#0B2E59"])
    _heatmap_with_strip(
        out_path, names, M,
        [f"{s} {SKILL_LABEL[s]}" for s in SKILL_ORDER],
        [SKILL_COLORS[s] for s in SKILL_ORDER], clear, cmap,
        "各难度桶的技能构成（MM 6 维；顶部=已通关占比，色=准确率→当前涉足范围）",
        strip_colors=strip_colors)


def fig_msd_radar(data, out_path):
    """MinaCalc 7 技能雷达（不含综合 Overall）：全库均值 vs 已游玩均值。"""
    import numpy as np
    import matplotlib.pyplot as plt

    prof = data.get("msdSkillProfile") or {}
    lib = prof.get("library") or {}
    pl = prof.get("played") or {}
    comps = [s for s in MSD_ORDER if s != "Overall"]
    labels = [f"{MSD_SHORT[s]}\n{MSD_LABEL[s]}" for s in comps]
    lib_v = [float(lib.get(s, 0.0)) for s in comps]
    pl_v = [float(pl.get(s, 0.0)) for s in comps]

    ang = np.linspace(0, 2 * np.pi, len(comps), endpoint=False).tolist()
    ang += ang[:1]

    def close(v):
        return v + v[:1]

    fig, ax = plt.subplots(figsize=(6.4, 6.0), subplot_kw=dict(polar=True))
    ax.set_theta_offset(np.pi / 2)
    ax.set_theta_direction(-1)
    ax.set_xticks(ang[:-1])
    ax.set_xticklabels(labels, fontsize=9)
    vmax = max(lib_v + pl_v + [1.0])
    ax.set_ylim(0, vmax * 1.12)

    ax.plot(ang, close(lib_v), color="#888888", linewidth=1.8, label="全库均值")
    ax.fill(ang, close(lib_v), color="#888888", alpha=0.12)
    ax.plot(ang, close(pl_v), color="#C0563B", linewidth=2.2, label="已游玩均值")
    ax.fill(ang, close(pl_v), color="#C0563B", alpha=0.18)
    for a, s, v in zip(ang[:-1], comps, lib_v):
        ax.plot([a], [v], marker="o", markersize=5, color=MSD_COLORS[s], zorder=5)

    ax.set_title("MinaCalc 7 技能雷达（不含综合）：全库 vs 已游玩", fontsize=13, pad=22)
    ax.legend(loc="upper right", bbox_to_anchor=(1.18, 1.10), fontsize=9, frameon=True)
    ax.grid(True, linestyle=":", linewidth=0.6, alpha=0.7)
    fig.tight_layout()
    fig.savefig(out_path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def fig_msd_heatmap(data, out_path):
    """各难度桶的 MinaCalc 8 维构成热力图（按技能行归一化）+ 顶部已通关占比绿柱。"""
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap

    buckets = data["buckets"]
    names = [b["name"] for b in buckets]
    M = np.zeros((len(MSD_ORDER), len(buckets)), dtype=float)
    for j, b in enumerate(buckets):
        sk = b.get("msdSkills") or {}
        for i, s in enumerate(MSD_ORDER):
            M[i, j] = float(sk.get(s, 0.0))
    clear = np.array([(b.get("playedCount", 0) / b["count"] * 100.0) if b.get("count") else 0.0
                      for b in buckets], dtype=float)
    acc_cm = _acc_cmap()
    strip_colors = [_err_color(_bucket_err2s(b), acc_cm) for b in buckets]
    # 暖色系，区别于 MM（蓝）热力图
    cmap = LinearSegmentedColormap.from_list("ett", ["#F6F2F0", "#C0563B", "#5A1A0C"])
    _heatmap_with_strip(
        out_path, names, M,
        [f"{MSD_SHORT[s]} {MSD_LABEL[s]}" for s in MSD_ORDER],
        [MSD_COLORS[s] for s in MSD_ORDER], clear, cmap,
        "各难度桶的 MinaCalc 8 维构成（顶部=已通关占比，色=准确率→当前涉足范围）",
        strip_colors=strip_colors)


def fig2_scatter(data, out_path):
    """Fig 2：成绩误差 vs 复合难度散点。每张谱只画一个点（取最佳成绩）；
    点面积 = 该谱尝试次数（练得越多越大）；点按谱型上色；叠加误差成倍增长拟合线。"""
    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    # 优先用 best-per-map（每谱最佳 + 尝试次数）；老数据缺失时回退到逐次成绩。
    rows = data.get("bestScores")
    if not rows:
        rows = [{"comp": s["comp"], "acc": s["acc"], "typ": s.get("typ"), "attempts": 1}
                for s in data.get("scores", [])]
    n = len(rows)

    comp = np.array([r["comp"] for r in rows])
    err = np.array([max(100.0 - r["acc"] * 100.0, 0.2) for r in rows])
    typ = [r.get("typ") or "RC" for r in rows]
    att = np.array([max(int(r.get("attempts", 1)), 1) for r in rows], dtype=float)

    # 尝试次数 -> 点面积（次方压缩，避免极端值过大）
    def _ssize(c):
        return 16.0 + 13.0 * (np.asarray(c, dtype=float) ** 0.7)

    from matplotlib.gridspec import GridSpec
    fig = plt.figure(figsize=(9.6, 6.0))
    gs = GridSpec(2, 1, height_ratios=[1, 6], hspace=0.06)
    axt = fig.add_subplot(gs[0])
    ax = fig.add_subplot(gs[1], sharex=axt)

    # 顶部：各难度桶已通关占比，对齐 comp 轴；颜色按已通关谱最佳成绩准确率（白→绿→橙→红）
    acc_cm = _acc_cmap()
    bx, bh, bc = [], [], []
    for b in data.get("buckets", []):
        if b.get("bucket") is None:
            continue
        bx.append(b["bucket"] + 0.25)
        bh.append((b.get("playedCount", 0) / b["count"] * 100.0) if b.get("count") else 0.0)
        bc.append(_err_color(_bucket_err2s(b), acc_cm))
    if bx:
        axt.bar(bx, bh, width=0.46, color=bc, edgecolor="#bbbbbb", linewidth=0.3)
    axt.set_ylim(0, 100)          # 绝对纵坐标：0%~100%
    axt.set_yticks([0, 50, 100])
    axt.set_ylabel("已通关\n占比%", fontsize=8, rotation=0, ha="right", va="center")
    axt.tick_params(axis="x", labelbottom=False, bottom=False)
    axt.tick_params(axis="y", labelsize=7)
    for sp in ("top", "right"):
        axt.spines[sp].set_visible(False)
    axt.set_title("成绩误差 vs 复合难度（每谱最佳成绩，点大小 = 尝试次数；顶部 = 已通关占比）",
                  fontsize=13, pad=8)

    # 按谱型分组上色（统一调色板），点大小按尝试次数
    present_types = []
    for t in TYPE_ORDER:
        mask = np.array([tt == t for tt in typ])
        if not mask.any():
            continue
        present_types.append(t)
        ax.scatter(comp[mask], err[mask], c=PALETTE[t], marker="o", s=_ssize(att[mask]),
                   edgecolors="white", linewidths=0.4, alpha=0.8, zorder=3, label=t)

    # --- 对数 + 反转 Y 轴：小误差在顶部（越好越高）---
    ax.set_yscale("log")
    ax.set_ylim(22, 0.3)   # bottom=22%，top=0.3% (反转)
    yticks = [0.5, 1, 2, 5, 10, 20]
    ax.set_yticks(yticks)
    ax.set_yticklabels([f"{t:g}%" for t in yticks])
    ax.minorticks_off()

    # --- 拟合：log2(err) 对 comp 线性回归；err_fit = 2**(b0 + b1*comp) ---
    annot = None
    if n >= 2:
        y2 = np.log2(err)
        b1, b0 = np.polyfit(comp, y2, 1)   # slope, intercept
        xs = np.linspace(comp.min(), comp.max(), 100)
        err_fit = 2.0 ** (b0 + b1 * xs)
        ax.plot(xs, err_fit, color="#222222", linewidth=1.8, linestyle="--",
                alpha=0.9, zorder=4)
        if b1 > 0:
            annot = "拟合：难度每 +%.2f，误差翻倍" % (1.0 / b1)
        elif b1 < 0:
            annot = "拟合：难度每 +%.2f，误差减半" % (1.0 / (-b1))
        else:
            annot = "拟合：误差与难度无关"
        # 标注放在右上区域（顶部=低误差侧）
        ax.text(0.985, 0.04, annot, transform=ax.transAxes, ha="right", va="bottom",
                fontsize=10, color="#222222",
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#888888", alpha=0.9))

    ax.set_xlabel("复合难度 comp = BSTv2（composite difficulty）", fontsize=11)
    ax.set_ylabel("误差率 (100−acc)", fontsize=11)
    ax.grid(True, which="major", linestyle=":", linewidth=0.5, alpha=0.6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    # 限定到实际成绩的难度范围（否则顶部桶柱会把共享 x 轴拉到全库最高桶，点挤在左侧）
    if n:
        ax.set_xlim(max(-0.3, float(comp.min()) - 0.4), float(comp.max()) + 0.5)
    # S 线：误差 5%（= acc 95%）—— 只画线，不标字
    ax.axhline(5.0, color="#FF2D2D", linestyle="--", linewidth=1.3, alpha=0.9, zorder=2)

    # 图例 1：谱型 + 拟合线
    handles = [Line2D([0], [0], marker="o", linestyle="none", color=PALETTE[t],
                      markersize=8, label=t) for t in present_types]
    handles.append(Line2D([0], [0], color="#222222", linewidth=1.8, linestyle="--",
                          label="拟合（误差成倍）"))
    leg1 = ax.legend(handles=handles, loc="upper right", fontsize=9, ncol=2,
                     frameon=True, framealpha=0.9)
    ax.add_artist(leg1)

    # 图例 2：尺寸 = 尝试次数（取几个参考值）
    amax = int(att.max()) if n else 1
    refs = sorted(set([1, max(2, amax // 4), max(3, amax // 2), amax]))
    size_handles = [Line2D([0], [0], marker="o", linestyle="none", color="#9aa0a6",
                           markeredgecolor="white", markersize=np.sqrt(_ssize(c)),
                           label=f"{c} 次") for c in refs]
    ax.legend(handles=size_handles, title="尝试次数", loc="lower left",
              labelspacing=1.1, borderpad=0.8, handletextpad=1.0,
              fontsize=8.5, title_fontsize=9, frameon=True, framealpha=0.9)

    fig.savefig(out_path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def fig3_progress(data, out_path):
    """Fig 3：准确率随游玩推进折线（6 等分时段）。
    叠加难度基线（各段平均难度，灰柱副轴）+ 整体中位准确率参考线（建议三）：
    避免把“后段 acc 下降”误读为退步——可能只是该段玩了更难的谱。"""
    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    prog = data["progress"]
    labels = [p["label"] for p in prog]
    vals = [p["avgAcc"] * 100.0 for p in prog]
    comps = [p.get("avgComp", 0.0) for p in prog]
    x = np.arange(len(prog))

    fig, ax = plt.subplots(figsize=(8.8, 4.4))

    # 副轴：各段平均难度（灰柱，置于底层）
    ax_d = ax.twinx()
    ax_d.bar(x, comps, width=0.55, color="#C9D2DA", alpha=0.7, zorder=1,
             label="该段平均难度")
    ax_d.set_ylabel("该段平均难度 comp", fontsize=10, color="#6B7782")
    ax_d.tick_params(axis="y", labelcolor="#6B7782")
    if comps:
        ax_d.set_ylim(0, max(comps) * 1.5)
    ax_d.spines["top"].set_visible(False)

    # 主轴：准确率折线
    ln, = ax.plot(x, vals, marker="o", markersize=7, linewidth=2.2,
                  color="#1D9E75", markerfacecolor="#1D9E75",
                  markeredgecolor="white", zorder=4)
    for xi, v in zip(x, vals):
        ax.annotate(f"{v:.2f}%", (xi, v), textcoords="offset points",
                    xytext=(0, 9), ha="center", fontsize=9, color="#222222", zorder=5)
    # S 线：准确率 95%（= 误差 5%）—— 只画线，不标字
    ax.axhline(95.0, color="#FF2D2D", linestyle="--", linewidth=1.3, zorder=4)

    ax.set_zorder(ax_d.get_zorder() + 1)   # 折线在柱之上
    ax.patch.set_visible(False)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_xlabel("游玩顺序区间（第 n 次游玩）", fontsize=11)
    ax.set_ylabel("平均准确率 (%)", fontsize=11)
    ax.set_title("准确率随游玩推进（叠加难度基线）", fontsize=14, pad=10)
    lo = min(vals) - 1.4
    hi = max(vals + [95.0]) + 1.6
    step, la, ha = nice_step(lo, hi, target=6)
    ax.set_yticks(np.arange(la, ha + step / 2, step))
    ax.set_ylim(lo, hi)
    ax.grid(axis="y", linestyle=":", linewidth=0.5, alpha=0.6)
    ax.spines["top"].set_visible(False)

    handles = [ln, plt.Rectangle((0, 0), 1, 1, color="#C9D2DA", alpha=0.7)]
    ax.legend(handles, ["平均准确率", "该段平均难度"],
              loc="lower right", fontsize=8.5, frameon=True, framealpha=0.9)

    fig.tight_layout()
    fig.savefig(out_path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def fig4_typeperf(data, out_path):
    """Fig 4：各类型平均准确率条形图（按 RC,HB,MIX,LN 排序），标注 n 与 acc%。"""
    import numpy as np
    import matplotlib.pyplot as plt

    perf = {p["typ"]: p for p in data["typePerf"]}
    order = [t for t in ["RC", "HB", "MIX", "LN", "Vibro"] if t in perf]
    vals = [perf[t]["avgAcc"] * 100.0 for t in order]
    cnts = [perf[t]["count"] for t in order]

    fig, ax = plt.subplots(figsize=(7.8, 4.2))
    x = np.arange(len(order))
    bars = ax.bar(x, vals, width=0.6, color=[PALETTE[t] for t in order],
                  edgecolor="white", linewidth=0.5)
    for xi, v, c in zip(x, vals, cnts):
        ax.annotate(f"{v:.2f}%\n(n={c})", (xi, v), textcoords="offset points",
                    xytext=(0, 5), ha="center", va="bottom", fontsize=9,
                    color="#222222")

    ax.set_xticks(x)
    ax.set_xticklabels(order, fontsize=11)
    ax.set_ylabel("平均准确率 (%)", fontsize=11)
    ax.set_title("各类型平均准确率（全曲最佳）", fontsize=14, pad=10)
    # 用 85–96 区间放大对比（按数据自适应裁剪）；整洁整数刻度（建议五：自动网格）
    lo = min(85.0, min(vals) - 1.0)
    hi = max(96.0, max(vals) + 1.5)
    step, la, ha = nice_step(lo, hi, target=6)
    ax.set_yticks(np.arange(la, ha + step / 2, step))
    ax.set_ylim(lo, hi)
    ax.grid(axis="y", linestyle=":", linewidth=0.5, alpha=0.6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_path, dpi=170, bbox_inches="tight")
    plt.close(fig)


# ----------------------------------------------------------------------------
# LaTeX 转义与表格构造
# ----------------------------------------------------------------------------
def tex_escape(s):
    s = str(s)
    repl = {
        "\\": r"\textbackslash{}",
        "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
        "[": "{[}", "]": "{]}",   # 防止行首 [4k] 被当作 \\ 的可选行距参数
    }
    for k, v in repl.items():
        s = s.replace(k, v)
    # ℵ（U+2135）SimSun 可能缺字 -> 用数学符号 \aleph 渲染（须在上面的 \ 与 $ 转义之后）
    s = s.replace("ℵ", r"$\aleph$")
    return s


def fmt_signed(n):
    n = int(n)
    return f"+{n}" if n > 0 else (f"{n}" if n < 0 else "0")


# ----------------------------------------------------------------------------
# 组装 .tex
# ----------------------------------------------------------------------------
def build_tex(data, fig_paths):
    s = data["summary"]
    buckets = data["buckets"]
    prog = data["progress"]
    perf = {p["typ"]: p for p in data["typePerf"]}
    tot = compute_type_totals(buckets)

    # 解析生成时间
    gen_raw = data.get("generatedAt", "")
    try:
        dt = datetime.fromisoformat(gen_raw)
        gen_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        gen_date = dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        gen_str = gen_raw
        gen_date = gen_raw[:10]

    db_str = "是" if s.get("dbRegenerated") else "否"

    # 桶变化（按桶难度升序，仅 delta != 0）
    # CHANGE 1: 解析桶名 "4k-XX.X" -> float，升序排列（4k-00.0, 4k-01.5, ...）。
    def _bucket_diff(c):
        try:
            return bstv2.name_value(str(c["name"]))
        except Exception:
            return float("inf")
    changes = [c for c in s.get("bucketChanges", []) if int(c.get("delta", 0)) != 0]
    changes = sorted(changes, key=_bucket_diff)

    # 派生：准确率推进首尾、最高/最低类型
    first_p = prog[0]["avgAcc"] * 100.0
    last_p = prog[-1]["avgAcc"] * 100.0
    delta_p = last_p - first_p
    # 类型平均准确率最高/最低（仅看出现在 typePerf 的）
    perf_list = [(t, perf[t]["avgAcc"] * 100.0, perf[t]["count"]) for t in perf]
    perf_sorted = sorted(perf_list, key=lambda x: -x[1])
    best_t, best_v, _ = perf_sorted[0]
    worst_t, worst_v, _ = perf_sorted[-1]

    # ---- 概览表 ----
    overview_rows = [
        ("本次新增 4K 谱", f"{s['newMaps']}"),
        ("4K 总数", f"{s['total4k']}"),
        ("谱面总数", f"{s['totalBeatmaps']}"),
        ("含暂定(provisional)谱", f"{s.get('provisionalTotal', 0)}"),
        ("是否重新生成 collection.db", db_str),
    ]
    overview_tex = "\n".join(
        f"{tex_escape(k)} & {tex_escape(v)} \\\\" for k, v in overview_rows
    )

    # ---- 桶变化表（多列排布以节省空间）----
    if changes:
        # 每行放 3 组 (name, delta)
        per_row = 3
        rows = []
        for i in range(0, len(changes), per_row):
            chunk = changes[i:i + per_row]
            cells = []
            for c in chunk:
                cells.append(tex_escape(c["name"]))
                cells.append(fmt_signed(c["delta"]))
            # 补齐空单元
            while len(cells) < per_row * 2:
                cells.append("")
            rows.append(" & ".join(cells) + " \\\\")
        change_body = "\n".join(rows)
        change_header = " & ".join(["桶", "$\\Delta$"] * per_row) + " \\\\"
        change_colspec = "l r " * per_row
        change_table = (
            "\\begin{center}\n"
            "\\small\n"
            f"\\begin{{tabular}}{{{change_colspec.strip()}}}\n"
            "\\toprule\n"
            f"{change_header}\n"
            "\\midrule\n"
            f"{change_body}\n"
            "\\bottomrule\n"
            "\\end{tabular}\n"
            "\\end{center}\n"
        )
    else:
        change_table = "（本次无桶变化。）\n"

    # ---- 类型总数内联表 ----
    tot_total = sum(tot.values())
    type_cells = " & ".join(f"{tot[t]}" for t in TYPE_ORDER)
    type_header = " & ".join(TYPE_ORDER)
    types_table = (
        "\\begin{center}\n"
        "\\small\n"
        "\\begin{tabular}{l r r r r r r}\n"
        "\\toprule\n"
        f"类型 & {type_header} & 合计 \\\\\n"
        "\\midrule\n"
        f"谱数 & {type_cells} & {tot_total} \\\\\n"
        "\\bottomrule\n"
        "\\end{tabular}\n"
        "\\end{center}\n"
    )

    # ---- 逐桶 longtable（含主导维度列 + provisional 标记）----
    def _dom_skill(bk):
        sk = bk.get("skills") or {}
        if not sk or sum(sk.values()) == 0:
            return "—"
        best = max(SKILL_ORDER, key=lambda s: sk.get(s, 0.0))
        return SKILL_CH.get(best, best)

    lt_rows = []
    for b in buckets:
        t = b["types"]
        nm = tex_escape(b["name"])
        if b.get("provisional", 0) > 0:
            nm = nm + r"$^\dagger$"
        lt_rows.append(
            " & ".join([
                nm,
                f"{b['count']}",
                (f"{b['avgSr']:.2f}" if b.get('avgSr') is not None else "—"),
                (f"{b['avgScaled']:.2f}" if b.get('avgScaled') is not None else "—"),
                _dom_skill(b),
                f"{t.get('RC', 0)}",
                f"{t.get('LN', 0)}",
                f"{t.get('HB', 0)}",
                f"{t.get('MIX', 0)}",
                f"{t.get('Vibro', 0)}",
            ]) + " \\\\"
        )
    longtable_body = "\n".join(lt_rows)

    # ---- 文本派生句 ----
    takeaway = (
        f"准确率随游玩推进整体上升：从首段的 {first_p:.2f}\\% 升至末段的 "
        f"{last_p:.2f}\\%，累计提升约 {delta_p:.2f} 个百分点，"
        f"反映出随练习的稳步进步。"
        f"分类型看，{tex_escape(best_t)} 平均准确率最高（{best_v:.2f}\\%），"
        f"{tex_escape(worst_t)} 最低（{worst_v:.2f}\\%）；"
        f"二者相差约 {best_v - worst_v:.2f} 个百分点，"
        f"提示 {tex_escape(worst_t)} 类谱面是当前相对薄弱、值得针对性练习的方向。"
    )

    # ---- BSTv2 公式说明（注脚式小字）----
    formula_note = (
        r"定级 $=$ \textbf{BSTv2}：$\mathrm{BSTv2} = A + B\,(0.4\,z(\mathrm{BST}) + 0.6\,z(\mathrm{MM}))$，"
        r"$z(x)=(x-\mu_x)/\sigma_x$。其中 "
        r"$\mathrm{BST} = 1.30\cdot \mathrm{base}\cdot(1 + 0.18\,\mathrm{lnr} + 0.12\,\mathrm{hb})$，"
        r"$\mathrm{base} = (\mathrm{MSD} + 4\,\mathrm{ISR} + 4\,\mathrm{RSR})/9$；"
        r"$\mathrm{MM}$ 为 Map Minus（yumu SkillMania6 本地移植）总分。"
        r"冻结参数 $\mu_{\mathrm{BST}}{=}7.319,\ \sigma_{\mathrm{BST}}{=}3.589,\ "
        r"\mu_{\mathrm{MM}}{=}3.585,\ \sigma_{\mathrm{MM}}{=}1.776,\ A{=}6.690,\ B{=}3.709$；"
        r"由 REFORM 全阶梯最小二乘锚定（REFORM 10 段 $\approx 13$）。"
    )

    # ---- 技能维度（MM 6 维）说明表 + 用户画像句 ----
    prof = data.get("skillProfile") or {}
    lib_prof = prof.get("library") or {}
    played_prof = prof.get("played") or {}
    SK_DESC = {
        "RC": "米流（连打 / 括号 / 叠键）",
        "ST": "耐力（长时间续航）",
        "SP": "速度（颤音 / 爆发）",
        "LN": "面条（放手 / 盾 / 反盾）",
        "CO": "协调（手锁 / 重叠）",
        "PR": "精准（倚音 / 延迟尾）",
    }
    skill_rows = []
    for sk in SKILL_ORDER:
        skill_rows.append(" & ".join([
            sk, SK_DESC[sk],
            f"{lib_prof.get(sk, 0.0):.2f}",
            f"{played_prof.get(sk, 0.0):.2f}",
        ]) + " \\\\")
    skill_table_body = "\n".join(skill_rows)
    libN = prof.get("libraryN", 0)
    playedN = prof.get("playedN", 0)
    prof_sentence = "（暂无足够数据生成技能画像。）"
    lib_tot = sum(lib_prof.values()) if lib_prof else 0.0
    pl_tot = sum(played_prof.values()) if played_prof else 0.0
    if lib_prof and played_prof and playedN and lib_tot > 0 and pl_tot > 0:
        # 用“维度占比”对比形状：剔除整体量级差（你尚未游玩最难谱，会让已游玩各维绝对值普遍偏低）。
        share = {sk: (played_prof.get(sk, 0.0) / pl_tot) - (lib_prof.get(sk, 0.0) / lib_tot)
                 for sk in SKILL_ORDER}
        hi = max(share, key=lambda x: share[x])
        lo = min(share, key=lambda x: share[x])
        prof_sentence = (
            f"按维度占比对比全库（已游玩 {playedN} 张 vs 全库 {libN} 张；占比剔除了整体量级差）："
            f"你在「{SKILL_LABEL[hi]}（{hi}）」维度上占比偏高（{share[hi] * 100:+.1f}\\%，相对偏好），"
            f"在「{SKILL_LABEL[lo]}（{lo}）」维度上占比偏低（{share[lo] * 100:+.1f}\\%，相对回避 / 薄弱）；"
            f"后者可能是值得针对性补强的方向。"
        )

    # ---- 算法局限 itemize ----
    sparse_names = [b["name"] for b in buckets if b.get("count") and b["count"] <= 9]
    sparse_str = "、".join(tex_escape(x) for x in sparse_names) if sparse_names else "无"
    prov_total = s.get("provisionalTotal", 0)
    limitations_body = "\n".join([
        r"\item \textbf{校准来源}：分桶由 BSTv2 决定，参数经 REFORM 全阶梯最小二乘锚定"
        r"（REFORM 10 段 $\approx 13$），并以全库一次性冻结 $\mu,\sigma,A,B$，单图分值不随库增减漂移。",
        r"\item \textbf{高难/稀疏桶置信度}：成员数 $\le 9$ 的桶样本偏少，类型与技能均值波动较大，"
        r"仅供参考 —— 当前为：" + sparse_str + "。",
        r"\item \textbf{provisional（暂定谱）}：仅指 .osu 源文件已不在 lazer 文件库中"
        r"（已删除 / 已更新谱面的旧缓存残留）、因而无法计算 MM 的谱；当前库内为 "
        + str(prov_total) + r" 张（若有则在逐桶表中以 $^\dagger$ 标注）。"
        r"其余所有谱均直接由 .osu 谱面数据按公式实算，含完整 MM 与技能维度，"
        r"\textbf{全程不使用官方星数（sr 仅作展示，不参与定级）}。",
        r"\item \textbf{MM 模型}：Map Minus 为 yumu \textbf{SkillMania6} 的本地 Python 移植，"
        r"已对齐其 /mm 输出，但与官方实现仍可能存在细微差异。",
        r"\item \textbf{有效成绩口径}：准确率 $<80\%$ 的成绩一律视为无效，不计入任何统计。",
    ])

    # ---- MinaCalc 8 维：说明表 + 键型出现桶 + 当前涉足 ----
    msd_prof = data.get("msdSkillProfile") or {}
    msd_lib = msd_prof.get("library") or {}
    msd_played = msd_prof.get("played") or {}
    MSD_DESC = {
        "Overall": "综合（总体难度）",
        "Stream": "单押流（连续单点）",
        "Jumpstream": "双押流（双押夹单押）",
        "Handstream": "三押流（三押为主）",
        "Stamina": "耐力（长时间高密度）",
        "JackSpeed": "叠键速（单列连击 jack）",
        "Chordjack": "和弦叠（多列同时叠键）",
        "Technical": "技术（复杂 / 不规则排布）",
    }
    msd_rows = []
    for s in MSD_ORDER:
        msd_rows.append(" & ".join([
            MSD_SHORT[s], MSD_DESC[s],
            f"{msd_lib.get(s, 0.0):.2f}",
            f"{msd_played.get(s, 0.0):.2f}",
        ]) + " \\\\")
    msd_table_body = "\n".join(msd_rows)

    # 键型“显著出现桶”（库内该维度首次达到自身峰值 70% 的桶）+ 峰值桶
    buckets_sorted = sorted(
        buckets, key=lambda b: (b.get("bucket") if b.get("bucket") is not None else 1e9))
    emerg_rows = []
    for s in MSD_ORDER:
        if s == "Overall":
            continue
        series = [(b["name"], float((b.get("msdSkills") or {}).get(s, 0.0))) for b in buckets_sorted]
        vals = [v for _, v in series]
        mx = max(vals) if vals else 0.0
        emerg = peak = "—"
        if mx > 0:
            thr = 0.7 * mx
            for nm, v in series:
                if v >= thr:
                    emerg = nm
                    break
            peak = max(series, key=lambda t: t[1])[0]
        emerg_rows.append(" & ".join([
            MSD_SHORT[s], tex_escape(MSD_LABEL[s]),
            tex_escape(emerg), tex_escape(peak)]) + " \\\\")
    msd_emerg_body = "\n".join(emerg_rows)

    # 当前涉足范围
    played_bk = [b for b in buckets_sorted if b.get("playedCount", 0) > 0]
    if played_bk:
        frontier = played_bk[-1]["name"]
        mostb = max(buckets_sorted, key=lambda b: b.get("playedCount", 0))["name"]
        frontier_sentence = (
            r"你目前已涉足到 \textbf{" + tex_escape(frontier) + r"} 桶"
            r"（游玩最密集在 " + tex_escape(mostb) + r" 附近）；上表中“显著出现桶”高于此的键型，"
            r"正是你随难度提升后才会逐渐遇到的。"
        )
    else:
        frontier_sentence = "（暂无已游玩谱面记录。）"

    # 图片相对路径（编译目录内）
    f1 = os.path.basename(fig_paths["fig1"])
    fT = os.path.basename(fig_paths["figtot"])
    fR = os.path.basename(fig_paths["figradar"])
    fH = os.path.basename(fig_paths["figheat"])
    fMR = os.path.basename(fig_paths["figmsdradar"])
    fMH = os.path.basename(fig_paths["figmsdheat"])
    f2 = os.path.basename(fig_paths["fig2"])
    f3 = os.path.basename(fig_paths["fig3"])
    f4 = os.path.basename(fig_paths["fig4"])

    # ------------------------------------------------------------------
    # 装配文档
    # ------------------------------------------------------------------
    tex = r"""\documentclass[11pt,a4paper]{article}

\usepackage{fontspec}
\usepackage{geometry}
\geometry{a4paper, margin=2.2cm}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage{longtable}
\usepackage{array}
\usepackage{xcolor}
\usepackage{caption}
\usepackage{float}
\usepackage[hidelinks]{hyperref}

%% ---- 字体：纯 fontspec，按文件路径解析系统 CJK 字体（本机无 ctex/xeCJK 依赖）----
%% SimSun（宋体）无独立粗体文件（simsunb.ttf 实为 SimSun-ExtB 生僻字，缺常用字），
%% 故正文用 AutoFakeBold 合成粗体；无衬线 DengXian 有真粗体 Dengb.ttf。
\setmainfont{__MAINFILE__}[Path=__FONTPATH__, AutoFakeBold=2]
\setsansfont{__SANSFILE__}[Path=__FONTPATH__, BoldFont=__SANSBOLD__]

%% ---- XeTeX 原生中文断行（无需 xeCJK）----
\XeTeXlinebreaklocale "zh"
\XeTeXlinebreakskip = 0pt plus 1pt minus 0.1pt

\definecolor{accent}{HTML}{1D9E75}
\definecolor{rule}{HTML}{888888}

\captionsetup{font=small, labelfont=bf, skip=4pt}
\setlength{\parindent}{0pt}
\setlength{\parskip}{5pt}

\renewcommand{\arraystretch}{1.15}

\begin{document}

%% ===== 标题 =====
\begin{center}
{\sffamily\bfseries\LARGE osu!mania 4K 难度分级报告}\\[4pt]
{\small 生成时间：__GENSTR__}
\end{center}
\vspace{2pt}
{\color{rule}\hrule height 0.8pt}
\vspace{6pt}

%% ===== Section 0 概览 =====
\section*{0\quad 概览}

\begin{center}
\small
\begin{tabular}{l r}
\toprule
项目 & 数值 \\
\midrule
__OVERVIEW__
\bottomrule
\end{tabular}
\end{center}

仅当本次出现新增谱面时才会重新生成 \texttt{collection.db}；若无新增则沿用现有收藏夹数据库，避免不必要的写回。

\subsection*{本次相对库中现有收藏夹的桶变化}
下表为本次结果相对于用户库中现有收藏夹的各难度桶谱数变化（仅列出有变化的桶，按难度升序）。

__CHANGE_TABLE__

%% ===== Section 1 难度分布 =====
\section*{1\quad 难度分布}

\begin{figure}[H]
\centering
\includegraphics[width=\linewidth]{__FIG1__}
\caption{难度分布（按谱面类型堆叠）。柱顶粉色数字 = 该桶平均星数（avgSr，一位小数）；黑色细箱线图（右轴，对数误差率）= 该桶各谱\textbf{最佳成绩}的误差率分布（仅 acc$\ge$80\%；盒=四分位、须=Q$\pm$3\,IQR、圆点=极端离群值 extreme outlier，即超出 3\,IQR 外栅栏者）。右轴 5\% 处红色虚线 = S 线（acc 95\%）。底部条（y 轴向下，绝对 0\%–100\%）= 该桶已通关（拥有 $\ge$80\% 成绩）谱占比，颜色按已通关谱最佳成绩误差的 \textbf{+2$\sigma$}（0\%=白、5\%(S)=绿、10\%=橙、$\ge$20\%=红）；条下方黑色数字 = 未完成曲目数（acc$<$80\% 或未游玩），其下红色数字 = 已通关但未达 S（误差$>$5\%）的成绩数。横轴桶号统一阿拉伯数字，半桶 '+' 置于下行。}
\end{figure}

\subsection*{全库各类型谱数}
按 \texttt{buckets[].types} 求和得到全库 4K 谱的类型构成：

__TYPES_TABLE__

\begin{figure}[H]
\centering
\includegraphics[width=0.66\linewidth]{__FIGTOT__}
\caption{全库 4K 谱按类型分布（饼图，含数量与占比）。}
\end{figure}

\subsection*{逐桶明细}
下表列出每个难度桶的谱数、平均星数（avgSr）、平均 BSTv2 难度、主导技能维度及各谱型构成。

\begin{center}
\small
\begin{longtable}{l r r r c r r r r r}
\toprule
桶 & 谱数 & 平均星 & 平均BSTv2 & 主导维度 & RC & LN & HB & MIX & Vibro \\
\midrule
\endfirsthead
\multicolumn{10}{l}{\small（续表）}\\
\toprule
桶 & 谱数 & 平均星 & 平均BSTv2 & 主导维度 & RC & LN & HB & MIX & Vibro \\
\midrule
\endhead
\midrule
\multicolumn{10}{r}{\small 接下页 \ldots}\\
\endfoot
\bottomrule
\endlastfoot
__LONGTABLE__
\end{longtable}
\end{center}
{\footnotesize 注：\,主导维度 = 该桶 MM 6 技能均值最大者（米=米流 / 耐=耐力 / 速=速度 / 面=面条 / 协=协调 / 精=精准）；桶名带 $^\dagger$ 表示含暂定(provisional)谱。}

%% ===== Section 2 技能维度 =====
\section*{2\quad 技能维度（MM 6 维）}

osu!mania 的难度并非单一标量。Map Minus（MM，移植自 yumu 的 SkillMania6）把每张谱拆成 6 个技能维度 —— 也就是 yumu \texttt{/mm} 输出右侧那“一大串”。BSTv2 的定级已融合 MM；此处把这套维度体系单独呈现，看清你的库构成与练习偏好。

\begin{center}
\small
\begin{tabular}{c l r r}
\toprule
维度 & 含义（主要 NoteType） & 全库均值 & 已游玩均值 \\
\midrule
__SKILL_TABLE__
\bottomrule
\end{tabular}
\end{center}

\begin{figure}[H]
\centering
\includegraphics[width=0.6\linewidth]{__FIGRADAR__}
\caption{技能维度雷达：灰色 = 全库均值，绿色 = 已游玩均值。两形对比即你的练习偏好相对全库的取舍。}
\end{figure}

\textbf{技能画像：}__PROF_SENTENCE__

\begin{figure}[H]
\centering
\includegraphics[width=\linewidth]{__FIGHEAT__}
\caption{各难度桶的技能构成（MM 6 维；行 = 技能，列 = 难度桶）。按技能行归一化，颜色越深表示该维度在此桶相对自身最强；可据此看出不同难度区间分别由哪些维度主导。顶部条 = 各桶已通关占比（拥有 $\ge$80\% 成绩的谱占比），颜色按已通关谱最佳成绩误差的 +2$\sigma$（0\%=白、5\%=绿、10\%=橙、$\ge$20\%=红），标示当前涉足范围。}
\end{figure}

\subsection*{MinaCalc 8 维（Etterna 计算器）}
MM 偏重手型与节奏型；BST 里的 MSD 来自 Etterna 的 MinaCalc，它把难度拆成另一套 8 个技能集（综合 + 7 个分项）。这套维度更贴近“键型 / pattern”语义，便于看清随难度上升会陆续遇到哪些键型。

\begin{center}
\small
\begin{tabular}{c l r r}
\toprule
技能集 & 含义 & 全库均值 & 已游玩均值 \\
\midrule
__MSD_TABLE__
\bottomrule
\end{tabular}
\end{center}

\begin{figure}[H]
\centering
\includegraphics[width=0.58\linewidth]{__FIGMSDRADAR__}
\caption{MinaCalc 7 技能雷达（不含综合 Overall）：灰色 = 全库均值，暖色 = 已游玩均值。}
\end{figure}

\begin{figure}[H]
\centering
\includegraphics[width=\linewidth]{__FIGMSDHEAT__}
\caption{各难度桶的 MinaCalc 8 维构成（行 = 8 技能集，列 = 难度桶；按技能行归一化，颜色越深表示该键型在此桶相对自身最强）。顶部条 = 各桶已通关占比，颜色按已通关谱最佳成绩误差的 +2$\sigma$（0\%=白、5\%=绿、10\%=橙、$\ge$20\%=红），标示当前涉足的难度范围。}
\end{figure}

下表给出每个键型“显著出现”的难度桶（库内该维度首次达到自身峰值 70\% 处）与峰值桶——结合上图顶部的涉足范围，即可读出哪些键型还在你前方：

\begin{center}
\small
\begin{tabular}{c l c c}
\toprule
键型 & 含义 & 显著出现桶 & 峰值桶 \\
\midrule
__MSD_EMERG__
\bottomrule
\end{tabular}
\end{center}

\textbf{当前涉足：}__FRONTIER__

%% ===== Section 3 得分报告 =====
\section*{3\quad 得分报告}

\begin{figure}[H]
\centering
\includegraphics[width=0.96\linewidth]{__FIG2__}
\caption{成绩误差 vs 复合难度。每张谱仅画一个点（取该谱最佳成绩），点面积 = 该谱尝试次数（含未通过，练得越多越大）。横轴为复合难度 comp（即 BSTv2）；y 轴为误差率（$100-\mathrm{acc}$）对数轴并反转，越靠上越准。点按谱型上色（RC/LN/HB/MIX/Vibro）；虚线 = 误差随难度的成倍增长拟合。y 轴 5\% 处红色虚线 = S 线（acc 95\%，“练习完成”标志）。顶部条 = 各难度桶已通关占比，颜色按已通关谱最佳成绩误差的 +2$\sigma$（0\%=白、5\%=绿、10\%=橙、$\ge$20\%=红）。}
\end{figure}

\begin{figure}[H]
\centering
\includegraphics[width=0.9\linewidth]{__FIG3__}
\caption{准确率随游玩推进（6 等分时段）。横轴为按游玩顺序均分的 6 个等数量区间，绿线 = 该区间平均准确率；灰柱（副轴）= 该区间平均难度 comp；红色虚线 = S 线（acc 95\%）。叠加难度基线可避免把“后段 acc 下降”误读为退步——可能只是该段玩了更难的谱。}
\end{figure}

\begin{figure}[H]
\centering
\includegraphics[width=0.82\linewidth]{__FIG4__}
\caption{各类型平均准确率（按已通关谱\textbf{最佳成绩}，每谱一张）。柱顶标注样本数 \(n\)（谱数）与平均准确率；纵轴区间经放大以突出差异。}
\end{figure}

\textbf{小结：}__TAKEAWAY__

%% ===== Section 4 算法局限 =====
\section*{4\quad 算法局限与说明}
\begin{itemize}\setlength{\itemsep}{2pt}
__LIMITATIONS__
\end{itemize}

\vspace{6pt}
{\color{rule}\hrule height 0.4pt}
\vspace{4pt}
{\footnotesize __FORMULA__}

\end{document}
"""

    # 占位符替换
    tex = (tex
           .replace("__FONTPATH__", TEX_FONT_PATH)
           .replace("__MAINFILE__", TEX_MAIN_FILE)
           .replace("__SANSFILE__", TEX_SANS_FILE)
           .replace("__SANSBOLD__", TEX_SANS_BOLD)
           .replace("__GENSTR__", tex_escape(gen_str))
           .replace("__OVERVIEW__", overview_tex)
           .replace("__CHANGE_TABLE__", change_table)
           .replace("__TYPES_TABLE__", types_table)
           .replace("__LONGTABLE__", longtable_body)
           .replace("__SKILL_TABLE__", skill_table_body)
           .replace("__PROF_SENTENCE__", prof_sentence)
           .replace("__MSD_TABLE__", msd_table_body)
           .replace("__MSD_EMERG__", msd_emerg_body)
           .replace("__FRONTIER__", frontier_sentence)
           .replace("__LIMITATIONS__", limitations_body)
           .replace("__TAKEAWAY__", takeaway)
           .replace("__FORMULA__", formula_note)
           .replace("__FIG1__", f1)
           .replace("__FIGTOT__", fT)
           .replace("__FIGRADAR__", fR)
           .replace("__FIGHEAT__", fH)
           .replace("__FIGMSDRADAR__", fMR)
           .replace("__FIGMSDHEAT__", fMH)
           .replace("__FIG2__", f2)
           .replace("__FIG3__", f3)
           .replace("__FIG4__", f4))
    return tex


# ----------------------------------------------------------------------------
# 编译
# ----------------------------------------------------------------------------
def compile_pdf(tex_path):
    """在 BUILD_DIR 中用 xelatex 编译两遍（保证 longtable / 引用稳定）。"""
    base = os.path.splitext(os.path.basename(tex_path))[0]
    log_path = os.path.join(BUILD_DIR, base + ".log")

    cmd = ["xelatex", "-interaction=nonstopmode", "-halt-on-error", base + ".tex"]
    last = None
    for _ in range(2):
        last = subprocess.run(cmd, cwd=BUILD_DIR,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace")
    pdf_path = os.path.join(BUILD_DIR, base + ".pdf")
    if not os.path.exists(pdf_path):
        tail = ""
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8", errors="replace") as f:
                tail = "".join(f.readlines()[-40:])
        raise RuntimeError("xelatex 未生成 PDF。日志尾部：\n" + tail)

    # 检查缺字（tofu）
    missing = 0
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "Missing character" in line:
                    missing += 1
    if missing:
        raise RuntimeError(f"LaTeX 日志中存在 {missing} 处 'Missing character'（CJK 缺字）。")
    return pdf_path, log_path


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main(argv):
    input_json = argv[1] if len(argv) > 1 else DEFAULT_INPUT
    output_pdf = argv[2] if len(argv) > 2 else DEFAULT_OUTPUT

    if not os.path.exists(input_json):
        raise SystemExit(f"输入文件不存在：{input_json}")

    # 重置编译目录（保证幂等）；保留已抽取的 OTF 以加速
    os.makedirs(BUILD_DIR, exist_ok=True)
    for fn in os.listdir(BUILD_DIR):
        if fn.endswith(".otf"):
            continue
        p = os.path.join(BUILD_DIR, fn)
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass

    with open(input_json, encoding="utf-8") as f:
        data = json.load(f)

    # ---- 配置 matplotlib（务必在 import pyplot 之前用 Agg）----
    import matplotlib
    matplotlib.use("Agg")
    prepare_matplotlib_font()

    # ---- 生成图表 ----
    fig_paths = {
        "fig1": os.path.join(BUILD_DIR, "fig1_distribution.png"),
        "figtot": os.path.join(BUILD_DIR, "fig_typetotals.png"),
        "figradar": os.path.join(BUILD_DIR, "fig_skill_radar.png"),
        "figheat": os.path.join(BUILD_DIR, "fig_skill_heatmap.png"),
        "figmsdradar": os.path.join(BUILD_DIR, "fig_msd_radar.png"),
        "figmsdheat": os.path.join(BUILD_DIR, "fig_msd_heatmap.png"),
        "fig2": os.path.join(BUILD_DIR, "fig2_scatter.png"),
        "fig3": os.path.join(BUILD_DIR, "fig3_progress.png"),
        "fig4": os.path.join(BUILD_DIR, "fig4_typeperf.png"),
    }
    fig1_distribution(data, fig_paths["fig1"])
    fig_typetotals(data, fig_paths["figtot"])
    fig_skill_radar(data, fig_paths["figradar"])
    fig_skill_heatmap(data, fig_paths["figheat"])
    fig_msd_radar(data, fig_paths["figmsdradar"])
    fig_msd_heatmap(data, fig_paths["figmsdheat"])
    fig2_scatter(data, fig_paths["fig2"])
    fig3_progress(data, fig_paths["fig3"])
    fig4_typeperf(data, fig_paths["fig4"])

    # ---- 写 .tex ----
    tex_path = os.path.join(BUILD_DIR, "report.tex")
    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(build_tex(data, fig_paths))

    # ---- 编译 ----
    pdf_path, log_path = compile_pdf(tex_path)

    # ---- 复制到输出 ----
    os.makedirs(os.path.dirname(output_pdf), exist_ok=True)
    # 输出挂载点 unlink 受限：先尝试覆盖写入（open 'wb' 不需要 unlink）
    with open(pdf_path, "rb") as src, open(output_pdf, "wb") as dst:
        shutil.copyfileobj(src, dst)

    size = os.path.getsize(output_pdf)
    print(f"[OK] 报告已生成：{output_pdf}  ({size} bytes)")
    print(f"[OK] 编译目录：{BUILD_DIR}  日志：{log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
