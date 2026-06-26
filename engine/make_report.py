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
    叠加两条副轴折线：平均星数 avgSr 与 平均准确率 avgAcc（CHANGE 2）。"""
    import numpy as np
    import matplotlib.pyplot as plt

    buckets = data["buckets"]
    names = [b["name"] for b in buckets]
    x = np.arange(len(buckets))
    counts_by_type = {t: np.array([b["types"].get(t, 0) for b in buckets], dtype=float)
                      for t in TYPE_ORDER}
    totals = np.array([b["count"] for b in buckets], dtype=float)
    avg_sr = np.array([b.get("avgSr") for b in buckets], dtype=float)
    # avgAcc 可能为 None（该桶无有效成绩）-> 用 NaN 占位，仅在非空处画点
    avg_acc = np.array([(b["avgAcc"] * 100.0) if b.get("avgAcc") is not None else np.nan
                        for b in buckets], dtype=float)

    # 副轴折线配色（与谱面类型调色板区分开，清晰可读）
    SR_COLOR = "#222222"     # 深近黑：平均星数
    ACC_COLOR = "#D4537E"    # 玫红：平均准确率

    fig, ax = plt.subplots(figsize=(11.2, 5.4))

    # --- 堆叠柱（保留）---
    bottom = np.zeros(len(buckets))
    for t in TYPE_ORDER:
        vals = counts_by_type[t]
        ax.bar(x, vals, bottom=bottom, width=0.82,
               color=PALETTE[t], label=t, edgecolor="white", linewidth=0.25,
               zorder=1)
        bottom += vals

    ymax = totals.max() if len(totals) else 1
    ax.set_xticks(x)
    ax.set_xticklabels(names, rotation=90, fontsize=7)
    ax.set_ylabel("谱数 (count)", fontsize=11)
    ax.set_xlabel("难度桶 (bucket)", fontsize=11)
    ax.set_title("难度分布（按谱面类型堆叠）", fontsize=14, pad=10)
    ax.set_ylim(0, ymax * 1.16)
    ax.grid(axis="y", linestyle=":", linewidth=0.5, alpha=0.6)
    ax.spines["top"].set_visible(False)

    # --- 副轴 1：平均星数 avgSr（右轴）---
    ax_sr = ax.twinx()
    ln_sr, = ax_sr.plot(x, avg_sr, color=SR_COLOR, marker="o", markersize=4.0,
                        linewidth=1.6, markerfacecolor=SR_COLOR,
                        markeredgecolor="white", markeredgewidth=0.4,
                        zorder=5, label="平均星数 avgSr")
    ax_sr.set_ylabel("平均星数", fontsize=11, color=SR_COLOR)
    ax_sr.tick_params(axis="y", labelcolor=SR_COLOR)
    ax_sr.spines["top"].set_visible(False)

    # --- 副轴 2：平均准确率 avgAcc（第二条右轴，外移脊柱）---
    ax_acc = ax.twinx()
    ax_acc.spines["right"].set_position(("axes", 1.085))
    ax_acc.spines["top"].set_visible(False)
    # 仅在非空处连线（mask 掉 NaN，使折线只覆盖有成绩的桶）
    finite = np.isfinite(avg_acc)
    ln_acc, = ax_acc.plot(x[finite], avg_acc[finite], color=ACC_COLOR, marker="s",
                          markersize=4.2, linewidth=1.6, markerfacecolor=ACC_COLOR,
                          markeredgecolor="white", markeredgewidth=0.4,
                          zorder=6, label="平均准确率 avgAcc")
    ax_acc.set_ylabel("平均准确率 (%)", fontsize=11, color=ACC_COLOR)
    ax_acc.tick_params(axis="y", labelcolor=ACC_COLOR)
    if finite.any():
        amin = np.nanmin(avg_acc); amax = np.nanmax(avg_acc)
        pad = max(0.6, (amax - amin) * 0.15)
        ax_acc.set_ylim(amin - pad, amax + pad)

    # --- 合并图例：类型柱 + 两条折线 ---
    bar_handles, bar_labels = ax.get_legend_handles_labels()
    handles = bar_handles + [ln_sr, ln_acc]
    labels = bar_labels + ["平均星数", "平均准确率"]
    ax.legend(handles, labels, title="图例", ncol=4, fontsize=8.5,
              title_fontsize=9, loc="upper right", frameon=True, framealpha=0.92)

    fig.tight_layout()
    fig.savefig(out_path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def fig_typetotals(data, out_path):
    """全库各类型谱数横向条形图（替代饼图）。"""
    import numpy as np
    import matplotlib.pyplot as plt

    tot = compute_type_totals(data["buckets"])
    types = TYPE_ORDER
    vals = [tot[t] for t in types]
    total = sum(vals)

    fig, ax = plt.subplots(figsize=(7.6, 2.6))
    y = np.arange(len(types))
    bars = ax.barh(y, vals, color=[PALETTE[t] for t in types],
                   edgecolor="white", linewidth=0.4, height=0.66)
    ax.set_yticks(y)
    ax.set_yticklabels(types, fontsize=10)
    ax.invert_yaxis()
    ax.set_xlabel("谱数 (count)", fontsize=10)
    ax.set_title(f"全库 4K 谱按类型分布（合计 {total}）", fontsize=12, pad=8)
    vmax = max(vals) if vals else 1
    for yi, v in zip(y, vals):
        pct = 100.0 * v / total if total else 0
        ax.text(v + vmax * 0.012, yi, f"{v}  ({pct:.1f}%)",
                va="center", ha="left", fontsize=9, color="#333333")
    ax.set_xlim(0, vmax * 1.18)
    ax.grid(axis="x", linestyle=":", linewidth=0.5, alpha=0.6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def fig2_scatter(data, out_path):
    """Fig 2：成绩误差 vs 复合难度散点（CHANGE 3）。
    Y 轴 = 误差率 err=100-acc*100（对数刻度，反转，越靠上越准）；
    颜色按游玩先后均分 6 段（红->绿）；marker：RC=●，非RC=×；并叠加误差成倍增长拟合线。"""
    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    scores = sorted(data["scores"], key=lambda s: s["date"])
    n = len(scores)
    ramp = _ramp_6()

    # 均分 6 段（等数量）
    edges = [round(n * k / 6) for k in range(7)]
    bin_of = [0] * n
    for b in range(6):
        for i in range(edges[b], edges[b + 1]):
            bin_of[i] = b

    comp = np.array([s["comp"] for s in scores])
    # 误差率：100 - acc*100；加一个极小地板避免 log(0)
    err = np.array([max(100.0 - s["acc"] * 100.0, 0.2) for s in scores])
    is_rc = np.array([s["typ"] == "RC" for s in scores])

    fig, ax = plt.subplots(figsize=(9.6, 5.4))

    # 分两种 marker 绘制（同时按段着色）
    for marker, mask, label in [("o", is_rc, "RC"), ("x", ~is_rc, "非RC")]:
        if not mask.any():
            continue
        cols = [ramp[bin_of[i]] for i in range(n) if mask[i]]
        if marker == "o":
            ax.scatter(comp[mask], err[mask], c=cols, marker="o", s=22,
                       edgecolors="white", linewidths=0.3, alpha=0.9, zorder=3)
        else:
            ax.scatter(comp[mask], err[mask], c=cols, marker="x", s=26,
                       linewidths=1.0, alpha=0.9, zorder=3)

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
                alpha=0.9, zorder=4, label="拟合")
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

    ax.set_xlabel("复合难度 comp（composite difficulty）", fontsize=11)
    ax.set_ylabel("误差率 (100−acc)", fontsize=11)
    ax.set_title("成绩误差 vs 复合难度", fontsize=14, pad=10)
    ax.grid(True, which="major", linestyle=":", linewidth=0.5, alpha=0.6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    # 图例 1：6 段颜色（早->近，等数量）
    color_handles = [Line2D([0], [0], marker="s", linestyle="none",
                            markerfacecolor=ramp[b], markeredgecolor="none",
                            markersize=9) for b in range(6)]
    color_labels = ["早"] + [""] * 4 + ["近"]
    leg1 = ax.legend(color_handles, color_labels, title="游玩先后（6 等分）",
                     loc="lower left", ncol=6, columnspacing=0.5,
                     handletextpad=0.1, fontsize=9, title_fontsize=9,
                     frameon=True, framealpha=0.9)
    ax.add_artist(leg1)

    # 图例 2：marker 含义 + 拟合线
    marker_handles = [
        Line2D([0], [0], marker="o", linestyle="none", color="#555555",
               markersize=8, label="● = RC"),
        Line2D([0], [0], marker="x", linestyle="none", color="#555555",
               markersize=8, markeredgewidth=1.4, label="× = 非RC"),
        Line2D([0], [0], color="#222222", linewidth=1.8, linestyle="--",
               label="拟合（误差成倍）"),
    ]
    ax.legend(handles=marker_handles, loc="upper right", fontsize=9,
              frameon=True, framealpha=0.9)

    fig.tight_layout()
    fig.savefig(out_path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def fig3_progress(data, out_path):
    """Fig 3：准确率随游玩推进折线（6 等分时段）。"""
    import numpy as np
    import matplotlib.pyplot as plt

    prog = data["progress"]
    labels = [p["label"] for p in prog]
    vals = [p["avgAcc"] * 100.0 for p in prog]
    x = np.arange(len(prog))

    fig, ax = plt.subplots(figsize=(8.4, 4.2))
    ax.plot(x, vals, marker="o", markersize=7, linewidth=2.0,
            color="#1D9E75", markerfacecolor="#1D9E75",
            markeredgecolor="white", zorder=3)
    for xi, v in zip(x, vals):
        ax.annotate(f"{v:.2f}%", (xi, v), textcoords="offset points",
                    xytext=(0, 9), ha="center", fontsize=9, color="#222222")

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_xlabel("游玩顺序区间（第 n 次游玩）", fontsize=11)
    ax.set_ylabel("平均准确率 (%)", fontsize=11)
    ax.set_title("准确率随游玩推进（6 等分时段）", fontsize=14, pad=10)
    lo = min(vals) - 1.2
    hi = max(vals) + 1.2
    ax.set_ylim(lo, hi)
    ax.grid(True, linestyle=":", linewidth=0.5, alpha=0.6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
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
    ax.set_title("各类型平均准确率", fontsize=14, pad=10)
    # 用 85–96 区间放大对比（按数据自适应裁剪）
    lo = min(85.0, min(vals) - 1.0)
    hi = max(96.0, max(vals) + 1.5)
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
    }
    for k, v in repl.items():
        s = s.replace(k, v)
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
            return float(str(c["name"]).split("-")[1])
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

    # ---- 逐桶 longtable ----
    lt_rows = []
    for b in buckets:
        t = b["types"]
        lt_rows.append(
            " & ".join([
                tex_escape(b["name"]),
                f"{b['count']}",
                f"{b['avgSr']:.2f}",
                f"{b['avgScaled']:.2f}",
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

    # ---- 复合公式说明（注脚式小字）----
    formula_note = (
        r"复合难度计算：$\mathrm{scaled} = 1.30\cdot \mathrm{base}\cdot"
        r"(1 + 0.18\cdot \ln r + 0.12\cdot hb)$，其中 "
        r"$\mathrm{base} = (\mathrm{MSD} + 4\cdot \mathrm{ISR} + 4\cdot \mathrm{RSR})/9$；"
        r"以 Reform 10 锚定 $\approx 13$。"
    )

    # 图片相对路径（编译目录内）
    f1 = os.path.basename(fig_paths["fig1"])
    fT = os.path.basename(fig_paths["figtot"])
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
\caption{难度分布（按谱面类型堆叠）。横轴为 39 个难度桶，左轴为谱数（按谱面类型堆叠）。叠加两条折线：深色线 = 该桶平均星数（avgSr，右轴），玫红线 = 该桶有效成绩的平均准确率（avgAcc，仅 acc$\ge$80\%，第二右轴，仅在有成绩的桶绘制）。}
\end{figure}

\subsection*{全库各类型谱数}
按 \texttt{buckets[].types} 求和得到全库 4K 谱的类型构成：

__TYPES_TABLE__

\begin{figure}[H]
\centering
\includegraphics[width=0.82\linewidth]{__FIGTOT__}
\caption{全库 4K 谱按类型分布（数量与占比）。}
\end{figure}

\subsection*{逐桶明细}
下表列出每个难度桶的谱数、平均星数（avgSr）、平均复合难度（avgScaled）及各类型构成，共 39 行。

\begin{center}
\small
\begin{longtable}{l r r r r r r r r}
\toprule
桶 & 谱数 & 平均星 & 平均复合 & RC & LN & HB & MIX & Vibro \\
\midrule
\endfirsthead
\multicolumn{9}{l}{\small（续表）}\\
\toprule
桶 & 谱数 & 平均星 & 平均复合 & RC & LN & HB & MIX & Vibro \\
\midrule
\endhead
\midrule
\multicolumn{9}{r}{\small 接下页 \ldots}\\
\endfoot
\bottomrule
\endlastfoot
__LONGTABLE__
\end{longtable}
\end{center}

%% ===== Section 2 得分报告 =====
\section*{2\quad 得分报告}

\begin{figure}[H]
\centering
\includegraphics[width=0.96\linewidth]{__FIG2__}
\caption{成绩误差 vs 复合难度。横轴为复合难度 comp；y 轴为误差率（$100-\mathrm{acc}$）对数轴并反转，越靠上越准。颜色按游玩先后均分为 6 段（非真实日期线性，因记录集中在近期）；标记：\(\bullet\) = RC，\(\times\) = 非RC；虚线 = 误差随难度的成倍增长拟合。}
\end{figure}

\begin{figure}[H]
\centering
\includegraphics[width=0.9\linewidth]{__FIG3__}
\caption{准确率随游玩推进（6 等分时段）。横轴为按游玩顺序均分的 6 个等数量区间，纵轴为该区间平均准确率。}
\end{figure}

\begin{figure}[H]
\centering
\includegraphics[width=0.82\linewidth]{__FIG4__}
\caption{各类型平均准确率（已游玩谱面）。柱顶标注样本数 \(n\) 与平均准确率；纵轴区间经放大以突出差异。}
\end{figure}

\textbf{小结：}__TAKEAWAY__

\vspace{10pt}
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
           .replace("__TAKEAWAY__", takeaway)
           .replace("__FORMULA__", formula_note)
           .replace("__FIG1__", f1)
           .replace("__FIGTOT__", fT)
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
        "fig2": os.path.join(BUILD_DIR, "fig2_scatter.png"),
        "fig3": os.path.join(BUILD_DIR, "fig3_progress.png"),
        "fig4": os.path.join(BUILD_DIR, "fig4_typeperf.png"),
    }
    fig1_distribution(data, fig_paths["fig1"])
    fig_typetotals(data, fig_paths["figtot"])
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
