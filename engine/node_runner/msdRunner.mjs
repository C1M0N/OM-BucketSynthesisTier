// Self-contained MinaCalc (MSD) runner for node, bypassing browser-only locateFile.
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { readFileSync as _rfs } from "node:fs";
globalThis.require = createRequire(import.meta.url);
globalThis.__dirname = path.dirname(fileURLToPath(import.meta.url));

import createMinaCalc723 from "./js/ett/versions/minaclac-72.3.js";
import { OsuFileParser } from "./js/parser/osuFileParser.js";

const VERS_DIR = path.join(globalThis.__dirname, "js", "ett", "versions");
const ORDER = ["Overall","Stream","Jumpstream","Handstream","Stamina","JackSpeed","Chordjack","Technical"];

let modPromise = null;
function getModule() {
  if (!modPromise) {
    const wasmBinary = _rfs(path.join(VERS_DIR, "minaclac-72.3.wasm"));
    modPromise = createMinaCalc723({ wasmBinary, locateFile: (p) => path.join(VERS_DIR, p) });
  }
  return modPromise;
}

function buildRows(chart) {
  const byTime = new Map();
  const cols = chart.columns, starts = chart.noteStarts;
  const len = Math.min(cols.length, starts.length);
  for (let i = 0; i < len; i++) {
    const c = Number(cols[i]), s = Math.trunc(Number(starts[i]));
    if (!Number.isFinite(c) || !Number.isFinite(s) || c < 0 || c > 31) continue;
    byTime.set(s, (byTime.get(s) || 0) | (1 << c));
  }
  const times = [...byTime.keys()].sort((a,b)=>a-b);
  const masks = new Uint32Array(times.length), secs = new Float32Array(times.length);
  for (let i=0;i<times.length;i++){ masks[i]=byTime.get(times[i])>>>0; secs[i]=times[i]/1000; }
  return { masks, secs };
}

export async function msd(osuText, musicRate = 1.0, scoreGoal = 0.93) {
  const chart = new OsuFileParser(osuText); chart.process();
  if (chart.status !== "OK") throw new Error("parse "+chart.status);
  const kc = chart.columnCount;
  const { masks, secs } = buildRows(chart);
  if (masks.length <= 1) return Object.fromEntries(ORDER.map(n=>[n,0]));
  const m = await getModule();
  const mB=masks.length*4, tB=secs.length*4, oB=8*4;
  const pM=m._malloc(mB), pT=m._malloc(tB), pO=m._malloc(oB);
  try {
    m.HEAPU32.set(masks, pM>>>2); m.HEAPF32.set(secs, pT>>>2);
    const ok = m._minacalc_compute(kc, musicRate, scoreGoal, pM, pT, masks.length, pO);
    if (!ok) throw new Error("minacalc_compute failed");
    const out = m.HEAPF32.slice(pO>>>2, (pO>>>2)+8);
    return Object.fromEntries(ORDER.map((n,i)=>[n, Number(out[i])||0])); 
  } finally { m._free(pM); m._free(pT); m._free(pO); }
}
