// batch_4k.mjs — runs the 3 calculators (MSD/isr/rsr) on a batch of .osu files.
// Lives inside the repo dir so relative imports (./js/..., ./msdRunner.mjs) resolve.
//
// Input  (argv[2]): JSON file = [{ md5, path }]  (path = absolute path to .osu text)
// Output (argv[3]): JSON file = [{ md5, ok, mode, cs, msd, isr, rsr, lnRatio, columnCount,
//                                  holds, total, err }]
// rate fixed at 1.0 per spec.
import { calculate as reworkCalc } from "./js/rework/sunnyAlgorithm.js";
import { calculateInterludeStar } from "./js/interlude/index.js";
import { msd } from "./msdRunner.mjs";
import { readFileSync, writeFileSync } from "node:fs";

const inPath = process.argv[2];
const outPath = process.argv[3];
const items = JSON.parse(readFileSync(inPath, "utf8"));

// Lightweight .osu header read for 4K filter + LN ratio (independent of calculators).
function quickMeta(osu) {
  let mode = null, cs = null;
  const lines = osu.split(/\r?\n/);
  let section = "";
  let holds = 0, total = 0, inHit = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (line.startsWith("[") && line.endsWith("]")) { section = line.slice(1, -1); inHit = (section === "HitObjects"); continue; }
    if (section === "General") {
      const m = line.match(/^Mode\s*:\s*(\d+)/); if (m) mode = parseInt(m[1], 10);
    } else if (section === "Difficulty") {
      const m = line.match(/^CircleSize\s*:\s*([\d.]+)/); if (m) cs = parseFloat(m[1]);
    } else if (inHit) {
      if (!line) continue;
      const parts = line.split(",");
      if (parts.length < 5) continue;
      total += 1;
      const type = parseInt(parts[3], 10);
      // mania hold note: type bit 7 (128) set
      if (type & 128) holds += 1;
    }
  }
  return { mode, cs, holds, total };
}

const out = [];
let n = 0;
for (const it of items) {
  n += 1;
  const rec = { md5: it.md5, ok: false, mode: null, cs: null, msd: null, isr: null, rsr: null,
                lnRatio: null, columnCount: null, holds: 0, total: 0, err: null };
  let osu;
  try { osu = readFileSync(it.path, "utf8"); }
  catch (e) { rec.err = "read:" + e.message; out.push(rec); continue; }
  const meta = quickMeta(osu);
  rec.mode = meta.mode; rec.cs = meta.cs; rec.holds = meta.holds; rec.total = meta.total;
  // 4K filter: Mode 3 AND CircleSize == 4
  if (meta.mode !== 3 || Math.round(meta.cs) !== 4) { rec.ok = true; rec.skip = true; out.push(rec); continue; }
  try { const r = reworkCalc(osu, 1.0, null, null); rec.rsr = Array.isArray(r) ? r[0] : (r?.star ?? r);
        if (Array.isArray(r)) { rec.lnRatio = r[1]; rec.columnCount = r[2]; } } catch (e) { rec.err = "rsr:" + e.message; }
  try { rec.isr = await calculateInterludeStar(osu, 1.0, null); } catch (e) { rec.err = (rec.err||"") + " isr:" + e.message; }
  try { rec.msd = (await msd(osu, 1.0)).Overall; } catch (e) { rec.err = (rec.err||"") + " msd:" + e.message; }
  rec.ok = (rec.msd != null && rec.isr != null && rec.rsr != null);
  out.push(rec);
  if (n % 25 === 0) { writeFileSync(outPath, JSON.stringify(out)); process.stderr.write(`  [${n}/${items.length}]\n`); }
}
writeFileSync(outPath, JSON.stringify(out));
process.stderr.write(`done ${out.length}\n`);
