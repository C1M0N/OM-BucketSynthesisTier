# osu!mania 4K 复合难度分级 —— 工作流完整规格

> 交给 Claude Code 的实现规格。目标:把 osu!lazer 里所有 4K(4 键)谱面按一套**复合难度**重新定级,分到 0.5 宽的难度桶(`4k-00.0 … 4k-19.0`),产出**可导入 lazer 的 `collection.db`** 和一份**中文 PDF 报告**;支持**增量**(只算没算过的新谱),可**每晚自动**或按需运行。

---

## 0. 给 Claude Code 的话:为什么本地跑更合适

这套流程之前在一个**沙箱**里实现(通过 Dropbox 镜像读用户的 osu 文件夹),踩到的几乎所有坑都来自环境,而不是算法:

1. **同步延迟**:沙箱看到的 `client.realm` 经常是 Dropbox 还没传完的**半截/陈旧**副本(`top_ref` 指到文件末尾之外 → 无法解析)。**你(Claude Code)在本地直接读用户硬盘上的真实 `client.realm`,没有这个问题**——前提是读取时 osu!lazer 已关闭(见 §3.3)。
2. **45 秒执行上限**:沙箱单次命令最多 45s,算几百张谱要反复分批断点续算。**你没有这个限制**,一次跑完即可。
3. **环境易失**:沙箱 `/tmp` 每次会话清空,计算器仓库要反复重建。**你的环境持久**,装一次即可。

所以这份文档既给"从零实现"的完整规格,也指明**现成可复用的代码**(在 `4k_classification/engine/`,见 §12)。**最快路径:先读 `engine/` 里的现有实现复用之**;§1–§11 是它背后的规格,§15 是必看的经验/坑。

---

## 1. 目标与产物

**输入**:用户的 osu!lazer 数据目录(本仓库根 = osu 根目录),含 `client.realm` 和 `files/` blob 存储。

**每次运行产出**:
- `4k_collections.db` —— osu!stable 格式的收藏夹文件,把每张 4K 谱按复合难度桶分组(`4k-XX.X`),并**原样保留**名为 `Y.S.Z.D.` 的手工收藏夹。用户用 CollectionManager 覆盖导入 lazer(见 §9)。
- `4k_report.pdf` —— 中文报告:难度分布 + 得分统计(见 §10)。
- 更新后的**难度缓存**(`results.json`,见 §11)。

**核心行为**:增量。已经算过的谱直接用缓存;只对新谱跑计算。**仅当本次出现新 4K 谱时**才重新生成 `collection.db`。

---

## 2. 运行环境与依赖

- **Python 3**(标准库即可:`struct/json/hashlib/subprocess/math/datetime`)。
- **Node.js**(≥18;实测 v22 OK)—— 跑三个难度计算器。
- **难度计算器仓库**:`https://github.com/LeoBlackMT/osumania_map_analyser`(克隆一次即可;也可解包 `engine/calc_env.tar.gz`)。其中关键文件在子目录 `ManiaMapAnalyser by Leo_Black/js/`:
  - `ett/versions/minaclac-72.3.js` + `minaclac-72.3.wasm` —— MSD(Etterna MinaCalc)。
  - `interlude/index.js` → `calculateInterludeStar` —— ISR。
  - `rework/sunnyAlgorithm.js` → `calculate` —— RSR。
  - `parser/osuFileParser.js` —— .osu 解析(给 MinaCalc 喂音符)。
- **报告**:`matplotlib`(画图)+ **XeLaTeX**(`xelatex`/`latexmk`)编译 PDF。中文字体用 **Noto Sans/Serif CJK SC**(见 §10 字体注意)。
- **导入工具**(用户侧手动):CollectionManager(Piotrekol)——lazer 没有原生 collection.db 导入。

---

## 3. 数据源

### 3.1 `client.realm`(Realm 数据库,手写二进制解析)

lazer 用 Realm 存元数据。**没有可用的现成读库**(realm-js 无 linux-node 预编译;.NET Realm SDK 打开子集模型返回空)——所以是**手写二进制解析**。现成实现见 `engine/realm_engine.py`,**强烈建议直接复用**。格式要点:

- 整文件读入字节 `d`。文件头魔数 `T-DB` 在偏移 16。
- **顶层 ref**:`flags = d[22]`;`sel = flags & 1`;`top_ref = <u64 LE @ (8 if sel else 0)>`。(两个 top-ref 槽位做原子切换。)
- **数组头(8 字节)@off**:`f = d[off+4]`;
  - `is_inner = f & 0x80`(B+树内部节点)
  - `has_refs = f & 0x40`(元素是 ref)
  - `wtype = (f >> 3) & 3`(0=Bits, 1=Multiply/字节, 2=Ignore)
  - `width_bits = (1 << (f & 7)) >> 1`(每元素位宽)
  - `size`(元素个数)= 24-bit **大端** = `(d[off+5]<<16)|(d[off+6]<<8)|d[off+7]`
  - 数据从 `off+8` 开始。
- **ref vs tagged-int**:偶数 = 字节偏移(ref);奇数 = 立即整数,值 = `x >> 1`。
- **顶层数组** `tv = read_refs(top_ref)`:`tv[0]` = 表名字符串数组,`tv[1]` = 各表根 ref 数组(`table_refs`)。按表名(如 `class_Beatmap` / `class_Score` / `class_BeatmapCollection`)在 `tv[0]` 里找下标 → 取 `table_refs[i]` 为该表根。
- **表根数组 T**:`T[0]` = spec(`[列类型数组, 列名数组, …]`),`T[2]` = 簇(cluster)树根。
- **簇树**:根可能是 B+树内部节点(`is_inner`),其 `children[3:]` 是各叶簇,最后一个元素是 tagged 的总行数。**每个叶簇**是一个数组,元素 k = 第 k 列的数组。**注意**:叶簇头部有一个 B+树 key 列,所以 spec 里声明序号 `i` 的列,实际在叶簇的 `i+1` 位置(`KEY_OFFSET = 1`)。
- **字符串列**:
  - Multiply 宽度叶(`wtype==1`):定宽槽,每槽最后一字节 = `(width-1-len)`,字符串 = `slot[:len]`。
  - 长字符串:`has_refs` 叶 `[offsets数组, blob数组, …]`,按 offsets 切 blob。
  - 超长/逐行大 blob:一个 ref 数组,每个 ref 指向一段 blob。
- **f64 列**(`Accuracy` / `StarRating` / `PP`):宽度标志可能**低报**,直接按 8 字节 double 读(stride 8)。
- **Timestamp 列**(`Date`):是一个 `size==2` 的 `has_refs` 元素 `[秒_int数组, 纳秒_int数组]`,秒在 unix 范围(~1.7e9)。秒数组可能有一个前导哨兵,取末 `rown` 个对齐行。

**用到的表/列**:
- `class_Beatmap`:`MD5Hash`(.osu 内容的 **MD5**,32 hex —— 这就是 collection.db / lazer 用的键)、`Hash`(.osu 的 **SHA-256**,64 hex = blob 文件名)、`StarRating`(lazer 官方星级,f64)。
- `class_Score`:`BeatmapHash`(= 谱面 **SHA-256**,**不是 MD5**)、`Accuracy`(f64,0–1)、`Date`(Timestamp)、`PP`(f64,**本地/未上传成绩基本是空/NaN** → 见 §15.5)、`MaxCombo`、`Mods`、`Rank`(成绩等级,非全球排名)等。
- `class_BeatmapCollection`:`Name`、`BeatmapMD5Hashes`(md5 字符串列表)。

> **小技巧(已验证)**:要快速建 `md5 → sha256` 映射(给新谱定位 .osu 文件),**别去哈希 17k 个 blob 文件**(慢)。直接从 `class_Beatmap` 的 `MD5Hash` + `Hash` 两列读出来,~0.1s 搞定。

### 3.2 `files/` blob 存储

`.osu` 谱面文本按 SHA-256 存:`files/<sha[0]>/<sha[0:2]>/<完整64位sha256>`(全小写)。文件内容以字节 `osu file format ` 开头。**谱面 MD5 = `md5(完整内容)`**;**SHA-256 = 文件名**。

### 3.3 realm 卡住/残缺的处理 + **files-only 回退**(重要)

osu!lazer **开着**(存在 `client.realm.lock`)或文件正在写/同步未完时,`client.realm` 可能是**残缺**的:`top_ref` 指到文件大小之外 → 无法解析。

- **本地(你)首选**:运行前**确保 osu!lazer 已完全关闭**,这样 realm 是干净一致的。开跑前做一次**健康检查**:`0 < top_ref < filesize`,否则报"realm busy"并停(别猜、别写)。
- **回退路径(realm 读不了但仍要定级)**:谱面 `.osu` 文件本身就在 `files/` 里。可以**完全绕过 realm**:扫 `files/` 下所有 `.osu`(按魔数识别)→ 算 `md5` → 与缓存比对找新谱 → 计算 → 分桶 → 重建 `collection.db`。这样即使 realm 不可用,也能把"当前曲库"分好桶。**缺点**:拿不到 realm 里的成绩(得分报告无法刷新)和官方 `StarRating`(报告里的"平均星数"会缺;但**分桶不依赖它**)。
- **铁律**:`client.realm` 及任何游戏文件**只读**。解析前可先 `copy` 到临时目录再读。**绝不写入/修改游戏文件。**

---

## 4. 三个难度计算器(来源、调用、核心公式)

每张谱、`rate=1.0`,输入 = `.osu` 文本。**现成调用封装见 `engine/node_runner/`**(`msdRunner.mjs`、`batch_4k.mjs`)和 `engine/run_4k.py` 的 `run_calculators(items)`。

**调用方式**(node,ESM;把 runner 放进仓库目录使相对 import 生效):
```js
import { msd } from "./msdRunner.mjs";                            // (await msd(osu,1.0)).Overall
import { calculateInterludeStar } from "./js/interlude/index.js"; // await calculateInterludeStar(osu,1.0,null)
import { calculate } from "./js/rework/sunnyAlgorithm.js";        // r=calculate(osu,1.0,null,null); rsr = Array.isArray(r)?r[0]:(r.star ?? r)
```
MinaCalc 是 WASM:node 里要把 `.wasm` 当 `wasmBinary` 直接喂,并绕过浏览器的 `locateFile`(`msdRunner.mjs` 已处理)。MinaCalc 输出 8 个 skillset,取下标 0 的 **Overall** 作为 `MSD`。

> 这三者都是**完整算法**(各几百行),下面是**核心定义式**(系数从源码逐字抄出;部分中间构造略写)。完整实现以源码为准。

**MSD(Etterna MinaCalc,概念式)**:逐 row 算 pattern 难度,对 8 个键型分别反解出"在目标准确率 g 下的难度",再软聚合:
```
SSR_s = { D : acc_s(D) = g },  g = 0.93
MSD   = Overall(SSR_1..SSR_8)   # 8 个 skillset 分的软聚合(取下标0)
```

**ISR(Interlude / YAVSRG)**:
```
d_i = ( (6·S_L^0.5)^3 + (6·S_R^0.5)^3 + J^3 )^(1/3)        # 单音难度;S_L/S_R=同手左右流速, J=jack 分量
x_k ← b − (b − x_k·δ')·e^(r·Δ̂),  b=0.01626·d_i²,  r=ln(0.5)/1575,  Δ̂=min(Δ,200)   # burst strain 递推(半衰期1575ms)
ISR = 0.4056·( Σ_i w_i·x_(i) / Σ_i w_i )^0.6,   w_i = 0.002 + max(0,(i+2500−N)/2500)^4   # 排序后加权幂平均
```

**RSR(sunny / rework)**:
```
L_i = 0.4·( A_i^(3/K_s)·min(J̄_i, 8+0.85·J̄_i) )^1.5
R_i = 0.6·( A_i^(2/3)·(0.8·P̄_i + 35·R̄_i/(C_i+8)) )^1.5
S_i = (L_i+R_i)^(2/3),   T_i = A_i^(3/K_s)·X̄_i / (X̄_i+S_i+1),   D_i = 2.7·S_i^0.5·T_i^1.5 + 0.27·S_i
SR  = 0.25·0.88·D93 + 0.2·0.94·D83 + 0.55·( Σ_i w_i·D_i^5 / Σ_i w_i )^(1/5),   w_i = C_i·Δt_i
RSR = 0.975·R( SR·N/(N+60) ),   R(s)= s (s≤9) 否则 9+(s−9)/1.2
```
(`A,J,P,R,X` 为按列预构造的五大 strain 分量;`C`=同时按键数,`K_s`=列键数,`D93/D83`=百分位项。)

---

## 5. 复合难度公式(用户定义,**核心**)

```
base   = (MSD + 4·ISR + 4·RSR) / 9
lnr    = 长条音符数 / 总物量          # hold notes / total notes
hb     = 4·lnr·(1 − lnr)             # 长条混合奖励, lnr=0.5 时最大
scaled = 1.30 · base · (1 + 0.18·lnr + 0.12·hb)
bucket = floor(scaled · 2) / 2        # 0.5 宽
桶名   = "4k-%04.1f" % bucket         # 4k-00.5 … 4k-19.0(零填充)
```
- `1.30` 缩放 + LN/HB 加权 = 让 LN/HB 谱**略微高估**(用户要求)。
- **锚定**:`REFORM 10` 段谱应得 `scaled ≈ 13`(用作公式正确性的 sanity check)。
- **相邻段位**靠数值差自然落入不同桶(无需特殊处理)。

---

## 6. 谱面类型分类器(从 `.osu` 物件)

按长条占比 `lnr` 与节奏特征分 5 类(现成实现见 `engine/osu_meta.py`):
```
lnr ≥ 0.5            → "LN"
0.2 ≤ lnr < 0.5      → "HB"
否则(rice, lnr<0.2):
    vibro_frac(同列相邻间隔 < 80ms 的占比) ≥ 0.08   → "Vibro"
    否则 chord_frac(≥2 同时按的行占比) ≥ 0.35 且 jack_frac(同列重复占比) ≥ 0.12 → "MIX"
    否则                                            → "RC"
```
(阈值为经验值;如需可微调。报告里按这 5 类着色/堆叠。)

---

## 7. 过滤规则

- **4K 判定**:`.osu` 头里 `Mode: 3`(mania)**且** `CircleSize == 4`。非 4K → 缓存标 `{"status":"skip"}`,不参与分桶。
- **成绩过滤**:采集成绩时,**准确率 < 0.80 的视为无效**,丢弃。得分报告(散点/进步曲线/各类型/每桶平均准确率)只基于有效成绩。

---

## 8. 分桶与命名

见 §5:`bucket = floor(scaled*2)/2`,名 `4k-%04.1f`。`collection.db` 里每个非空桶 = 一个收藏夹,按桶升序排列。

---

## 9. `collection.db` 格式(导入 lazer)

osu!**stable** 收藏夹二进制格式(lazer 经 CollectionManager 导入):
```
int32   version            = 20240101
int32   numCollections
每个收藏夹:
    string  name
    int32   numBeatmaps
    numBeatmaps × string  beatmapMD5
```
`string` = 字节 `0x0B` + ULEB128 长度 + UTF-8 内容(空串则只一个 `0x00`,但这里都有内容)。所有整数小端。
- 收藏夹 = 所有非空 `4k-XX.X` 桶(升序)+ **保留 `Y.S.Z.D.`**(用其原始 12 个 md5;从 realm 的 collection 表读,或沿用上一份 db 里的)。
- 键用**谱面 MD5**(= `md5(.osu内容)` = realm `class_Beatmap.MD5Hash`)。
- 现成 writer 见 `engine/run_4k.py`(搜 `write_collection_db` / "collection.db writer")。

**用户导入步骤**(写进交付目录的 README):关闭 lazer → 备份 `client.realm` → CollectionManager `Load` 选 `4k_collections.db` → `File → Save → Default osu! collection` → 重开 lazer 确认。

---

## 10. 报告(PDF)规格

matplotlib 出图 → XeLaTeX 编译单一中文 PDF(现成实现见 `engine/make_report.py`)。数据来自 `report_data.json`(见 §11)。

**第 0 节 概览**:本次新增 4K 谱数、4K 总数、谱面总数、是否重生成 db;一张"桶变化"表(相对当前库中收藏夹的各桶谱数变化,**按桶难度升序**,只列有变化的)。

**第 1 节 难度分布**:
- **堆叠柱状图**:x = 各难度桶,y = 谱数,按类型(RC/LN/HB/MIX/Vibro)堆叠。叠加**两条均值线**(用次坐标轴):每桶**平均星数 avgSr**、每桶**平均准确率 avgAcc**(由有效成绩算)。
- 全库类型构成表 + 横向条形(各类型谱数与占比)。
- **逐桶明细表**(longtable):桶 | 谱数 | 平均星 avgSr | 平均复合 avgScaled | RC | LN | HB | MIX | Vibro。
- 建议类型配色:RC `#378ADD`、LN `#7F77DD`、HB `#1D9E75`、MIX `#EF9F27`、Vibro `#E24B4A`。

**第 2 节 得分报告**:
- **散点**:x = 复合难度 `comp`(= 该谱 `scaled`),y = **误差率 `100−acc`**,**对数轴且反转**(小误差在上 = "越好越高",刻度如 0.5/1/2/5/10/20%)。点按**游玩先后均分 6 段**着色(红→绿:`#e24b4a`→`#1d9e75`);**RC 用 ●,非 RC 用 ×**。加一条 `log2(误差)` 对 `comp` 的线性拟合线,标注"难度每 +X 误差翻倍"(实测约 +1.55)。
- **进步曲线**:把有效成绩按游玩先后均分 6 段,画每段平均准确率(折线+标注)。
- **各类型平均准确率**柱状(按类型配色,标注 n 与 acc%)。
- 脚注附复合公式(§5)。

**CJK 字体注意**:
- LaTeX:环境若**没有 `ctex`/`xeCJK`**,改用 `fontspec` + `\setmainfont{Noto Serif CJK SC}[Script=CJK]` + XeTeX 断行原语(`\XeTeXlinebreaklocale "zh"`)。
- matplotlib **无法按字体族名直接注册 `.ttc`**:用 `fontTools` 从 `NotoSansCJK-Regular.ttc` 抽出 "Noto Sans CJK SC" 子字体存成 `.otf`,再 `addfont`;`rcParams['axes.unicode_minus']=False`。
- 在 outputs 受限挂载上 LaTeX 的 aux 文件会出问题 → **在本地临时目录编译**,再把成品 PDF 拷出。(你在本地无此限制。)

---

## 11. 缓存与增量

- **缓存** `results.json`:`md5 → {status, sr, msd, isr, rsr, scaled, bucket, lnr, type, title, version}`。`status` ∈ `ok`(4K 已算)/ `skip`(非 4K)。`sr` 在 realm 不可用时可为 `null`(不影响分桶)。
- **增量**:遍历当前曲库的每个谱面 `md5`;已在缓存(`ok`/`skip`)的跳过;新 `md5` 才计算。
- **`report_data.json`**(给报告用)结构:
```
{ generatedAt,
  summary:{ newMaps, total4k, totalBeatmaps, dbRegenerated, bucketChanges:[{name,delta}] },
  buckets:[ {bucket, name, count, avgSr, avgScaled, avgAcc, types:{RC,LN,HB,MIX,Vibro}} ],
  scores:[ {comp, acc, date, typ} ],     # 仅 acc≥0.80;date=unix秒
  typePerf:[ {typ, count, avgAcc} ],
  progress:[ {idx, label, avgAcc, count} ]   # 6 个等量游玩先后段
}
```

---

## 12. 现有实现文件清单(在 `4k_classification/engine/`,可直接复用)

> **最快路径:先读这些文件**。它们是上一个环境里调试可用的版本;在本地跑前主要要做的是**把硬编码/路径改成直接读本目录的 realm**,并去掉 45s 分批的权宜逻辑(本地可一次跑完)。

- `realm_engine.py` —— 手写 realm 解析器。类 `Realm`:`table_spec(name)`、`table_leaves(name)`、`leaf_col(lc,name,col)`、`read_refs/read_ints/read_ints_signed/read_f64_at`、`decode_string_column`、`beatmaps_md5()`、`scores()`、`collections()`;以及 `build_blob_index(files_root,...)`(md5→sha)。
- `osu_meta.py` —— `.osu` 解析:4K 判定、`lnr`、类型分类器(§6)。
- `run_4k.py` —— 编排:`ensure_blob_index()`(已优化为从 realm `Hash` 列秒建)、解析、增量计算 `run_calculators(items)`(调 node)、复合/分桶、写 `collection.db`、产 `report_data.json`。CLI:`--max-new=N`、`--phase=compute|finalize|all`、`--realm=PATH`(指定 realm 文件,可指向备份/快照)。
- `make_report.py` —— 出图 + XeLaTeX → `report.pdf`。
- `node_runner/msdRunner.mjs`、`node_runner/batch_4k.mjs` —— node 计算器封装(MSD/ISR/RSR 批量)。
- `calc_env.tar.gz` —— 打包好的计算器仓库(MinaCalc WASM + Interlude + sunny)。解包:`tar xzf calc_env.tar.gz -C <某处>`,得到 `ManiaMapAnalyser by Leo_Black/`。
- `cache/results.json` —— 当前难度缓存(已含约 2392 张 4K 的结果,可继续增量)。
- `run_workflow.sh` —— 一键:引导计算器 → realm 健康检查(`REALM_BUSY` 优雅跳过)→ `run_4k.py` → `make_report.py` → 把 `4k_report.pdf`/`4k_collections.db` 拷到上级交付目录。

> 注:`run_4k.py`/`run_workflow.sh` 里的路径是按"脚本所在 = `<osu根>/4k_classification/engine`"自定位的(`OSU_DIR = 脚本目录的祖父目录`)。本地放在同样位置即可直接用。

---

## 13. 端到端流程(Claude Code 怎么跑)

1. **确保 osu!lazer 已关闭**(realm 干净)。
2. 准备计算器:`tar xzf engine/calc_env.tar.gz -C /tmp/calc`(或 `git clone` 那个仓库),把 `engine/node_runner/*.mjs` 放进 `ManiaMapAnalyser by Leo_Black/` 目录里。
3. **解析 realm**(只读;可先 copy 一份再读):读 `class_Beatmap` 得全部谱面 `{md5, sha256, sr}`;读 `class_BeatmapCollection` 取 `Y.S.Z.D.`;读 `class_Score` 取成绩 `{sha256, acc, date}`(过滤 acc<0.80)。
   - realm 若不可解析 → 走 §3.3 **files-only 回退**(扫 `files/` 的 `.osu` 当谱面列表)。
4. **增量计算**:对不在缓存的谱面:取 `.osu`(由 sha256 定位 blob)→ 4K 判定 → 跑 MSD/ISR/RSR → 复合 `scaled`、分桶 `bucket`、类型 `type`、`lnr` → 写缓存。非 4K 标 `skip`。
5. **仅当有新 4K 谱**:重建 `collection.db`(§9)。
6. 产 `report_data.json` → `make_report.py` 出 `4k_report.pdf`。
7. 交付:把 `4k_collections.db`、`4k_report.pdf` 放到 `4k_classification/`。
8. 打印摘要:新增数 / 4K 总数 / 桶变化;若重生成了 db,提醒用户用 CollectionManager 重新导入。

---

## 14. 调度(可选)

希望每天定时跑(用户之前要的是**每晚 23:30**)。本地用系统计划任务(Windows 计划任务 / cron)定时调 `run_workflow.sh` 即可。**注意**:跑的时候 osu!lazer 要关着,否则 realm 是锁定/半截状态——脚本会 `REALM_BUSY` 跳过。建议挑用户不玩的时段(或在任务里先确保 lazer 进程已退出)。

---

## 15. 关键坑 / 经验清单(务必看)

1. **同步/读取**:本地直接读真实 `client.realm`,避免镜像/网盘的半截同步。读前确认 lazer 关闭。
2. **realm 残缺判定**:`top_ref` 必须落在 `[0, filesize)` 内,否则文件不完整 → 别解析、别猜;要么等干净副本,要么走 files-only 回退。`collectionBackups/client_<md5>.realm` 是 CollectionManager 留的**完整备份**,必要时可作快照来源(配合 `run_4k.py --realm=`)。
3. **只读游戏文件**:`client.realm` 及 `files/` 一律只读;要改先 copy。绝不写游戏目录。
4. **计算器一致性**:复合/分桶/分类逻辑要和已有 2392 张**完全一致**(零漂移),否则新旧谱会错桶、和已导入的收藏夹对不上。复用 `run_4k.py`/`osu_meta.py` 的现成函数最稳。
5. **PP/全球排名不可得**:realm 里本地/未上传成绩的 `PP` 基本是空/NaN;历史全球排名本地无法还原。所以报告**不做 pp / 排名轴**(之前评估后放弃)。
6. **CJK PDF**:见 §10 字体注意(`fontspec`+Noto;matplotlib 用抽出的 `.otf`)。
7. **blob 索引**:`md5→sha` 直接从 realm `class_Beatmap` 的 `MD5Hash`+`Hash` 两列建(秒级),别去哈希上万个文件。
8. **键的区分**:成绩表 `BeatmapHash` 是 **SHA-256**;collection.db / 谱面键是 **MD5**。两者经 `class_Beatmap` 的 `Hash`↔`MD5Hash` 互转。
9. **增量正确性**:缓存按 `md5`;当前曲库的谱面集合 = 扫到的 `.osu`(回退)或 realm 谱面表。删掉的谱在 collection.db 里残留无害(lazer 忽略不存在的 md5)。

---

## 16. 验证清单(交付前自检)

- [ ] `scaled` 公式自检:`REFORM 10` 段 ≈ 13。
- [ ] 计算器复现:对若干**已在缓存**的谱重算 MSD/ISR/RSR,与缓存值吻合(零漂移)。
- [ ] `collection.db` 往返解析:收藏夹数 / 成员数对得上;无尾随字节;`Y.S.Z.D.` 12 个 md5 原样保留。
- [ ] 4K 数:缓存 `ok` 数 = 桶内总数;无"4K 但未分桶"残留。
- [ ] PDF:5 段内容齐全、中文无豆腐块、四张图符合 §10。
- [ ] `client.realm` 运行前后 md5 不变(只读)。

---

*附:复合公式与三个计算器的核心式见 §4–§5。现成代码见 `4k_classification/engine/`。如需我(原环境)导出某个文件的逐字内容,可单独索取。*
