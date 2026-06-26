#!/usr/bin/env bash
# 4K 难度分级 一键工作流 — 持久安装在 osu 文件夹内, 自定位, 换会话/挂载名都能跑
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../12_osu/4k_classification/engine
OSU_DIR="$(dirname "$(dirname "$HERE")")"               # .../12_osu
DELIVER="$(dirname "$HERE")"                            # .../12_osu/4k_classification
REPO="/tmp/calc/ManiaMapAnalyser by Leo_Black"

REPORT_ONLY=0; PASS_ARGS=()
for a in "$@"; do case "$a" in --report-only) REPORT_ONLY=1;; *) PASS_ARGS+=("$a");; esac; done

echo "[wf] engine = $HERE"
echo "[wf] osu    = $OSU_DIR"
[ -f "$OSU_DIR/client.realm" ] || { echo "[wf] ERROR: client.realm not found under $OSU_DIR"; exit 1; }

# realm 健康检查: lazer 开着 / Dropbox 未同步完时 client.realm 是半截快照, 解析必失败 -> 优雅跳过
REALM_OK="$(python3 - "$OSU_DIR/client.realm" <<'PYEOF'
import struct,sys
try:
    d=open(sys.argv[1],'rb').read()
    flags=d[22]; sel=flags&1; top=struct.unpack_from('<Q',d,8 if sel else 0)[0]
    print("OK" if 0<top<len(d) else "BUSY")
except Exception:
    print("BUSY")
PYEOF
)"
if [ "$REALM_OK" != "OK" ]; then
  echo "[wf] REALM_BUSY: client.realm 现在是半截/锁定快照(osu!lazer 开着或 Dropbox 未同步完),本次跳过、未改动任何文件。请关闭 lazer 让其完整同步后再跑。"
  exit 7
fi

# 引导计算器环境 (/tmp 每会话清空)
if [ ! -d "$REPO" ]; then
  mkdir -p /tmp/calc
  if [ -f "$HERE/calc_env.tar.gz" ]; then tar xzf "$HERE/calc_env.tar.gz" -C /tmp/calc && echo "[wf] calc restored from tarball";
  else git clone --depth 1 https://github.com/LeoBlackMT/osumania_map_analyser.git /tmp/calc_src && cp -r "/tmp/calc_src/ManiaMapAnalyser by Leo_Black" /tmp/calc/ && cp "$HERE/node_runner/"*.mjs "/tmp/calc/ManiaMapAnalyser by Leo_Black/" 2>/dev/null; fi
fi
[ -d "$REPO" ] || { echo "[wf] ERROR: calc env unavailable"; exit 1; }

if [ "$REPORT_ONLY" -eq 0 ]; then
  python3 "$HERE/run_4k.py" "${PASS_ARGS[@]}" || { echo "[wf] run_4k.py FAILED"; exit 1; }
fi
python3 "$HERE/make_report.py" || { echo "[wf] make_report.py FAILED"; exit 1; }

mkdir -p "$DELIVER"
[ -f "$HERE/report.pdf" ]    && cp -f "$HERE/report.pdf"    "$DELIVER/4k_report.pdf"
[ -f "$HERE/collection.db" ] && cp -f "$HERE/collection.db" "$DELIVER/4k_collections.db"
echo "[wf] deliverables -> $DELIVER"; ls -la "$DELIVER"
echo "[wf] DONE"
