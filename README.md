# 4K 难度分级 — 输出说明

本目录由「osu!mania 4K 复合难度分级工作流」自动生成 / 更新。**已在本机(Windows)本地化**:不再依赖沙箱,直接读硬盘上的真实 `client.realm`,可手动或每晚自动运行。

## 文件
- **4k_collections.db** — 可导入 osu!lazer 的收藏夹文件。按 **BSTv2** 难度分桶(`[4k]0` … `[4k]XVIII+` … `[4k]Z`,每 0.5 一桶,见下方公式),并**原样保留所有手工收藏夹**(即名字不以 `[4k]` 开头的,如当前的 `13SKIP`),导入时不会丢。
- **4k_report.pdf** — 中文报告:难度分布 + 得分统计(每次运行覆盖更新,只保留最新一份)。

## 把 collection.db 导入 lazer(CollectionManager)
> lazer 没有原生 collection.db 导入,需用 CollectionManager(Piotrekol)。
1. **关闭 osu!lazer。**
2. 备份上一级的 `client.realm`(保险起见)。
3. 打开 CollectionManager → **Load** → 选本目录的 `4k_collections.db`。
4. **File → Save → Default osu! collection**(覆盖写回 lazer)。
5. 打开 lazer 确认收藏夹。

> 仅当检测到**新增 4K 谱**时才会重新生成 `4k_collections.db`(`db` 内容变了才需要重新导入);没有新增就只刷新报告,不动收藏夹库。报告每次都会重出(成绩可能更新)。

## 每晚自动运行(已设置)
- **Windows 计划任务** `osu4k-nightly-classify`,每天 **23:30** 自动跑一次(增量)。
- 跑的时候 **osu!lazer 必须关着**:若 lazer 在运行或 `client.realm` 半截/同步未完,任务会**优雅跳过**(不改任何文件),等下次。
- 运行日志在 `%LOCALAPPDATA%\osu4k\logs\run_*.log`(非 Dropbox,避免同步锁文件)。
- 管理任务:任务计划程序里搜 `osu4k-nightly-classify`,可改时间/停用/手动运行。

## 手动重跑
加了新曲子想立刻重算时(先关 lazer),任选其一:

- **最简单:双击本目录的 `osu4k.cmd`**。它会自动跑增量、刷新 db/报告,跑完停在窗口让你看结果。也可以在终端里直接敲 `osu4k`(在本目录下),或把它"发送到 → 桌面快捷方式"。
- 或直接调脚本(可带开关):
  ```powershell
  powershell -ExecutionPolicy Bypass -File "<osu根>\4k_classification\engine\run_workflow.ps1" -ResetBaseline
  ```
  常用开关:`-ReportOnly`(只重出报告)、`-Force`(强制重建 db)、`-ResetBaseline`(让"本次新增"只统计这次真正新增的谱)。`osu4k.cmd` 已默认带 `-ResetBaseline`,额外开关会原样透传(如 `osu4k -ReportOnly`)。
- 或让 Claude「重新跑一遍 4K 工作流」。

> osu!lazer 开着时双击 `osu4k.cmd` 会显示 `[SKIP]` 并什么都不改 —— 关掉 lazer 再跑即可。

引擎、计算器(`%USERPROFILE%\osu4k_calc`)和难度缓存(`engine\cache\results.json`,含 2400 张 4K 结果)都已常驻,**只会计算没算过的新谱**,零漂移(新旧谱用同一套公式/计算器)。

## 难度公式：BSTv2

当前定级系统 **BSTv2** = 旧复合分 **BST**(三计算器)与社区模型 **MM**(Map Minus，本地移植自 yumu 的 SkillMania6，已逐位复现其 `/mm` 输出)的融合。完整公式如下。

### 1. BST —— 三计算器复合分

$$\text{base} = \frac{\text{MSD} + 4\,\text{ISR} + 4\,\text{RSR}}{9}$$

$$\text{BST} = 1.30 \cdot \text{base} \cdot \left(1 + 0.18\,\text{lnr} + 0.12\,\text{hb}\right), \qquad \text{hb} = 4\,\text{lnr}\,(1-\text{lnr})$$

其中 MSD = Etterna MinaCalc Overall，ISR = Interlude，RSR = sunny rework，lnr = 长条音符占比。

### 2. MM —— Map Minus overall

从 .osu 手型逐对算出 13 个 pattern 分量，归并为 6 个命名技能（RC 米 / ST 耐力 / SP 速度 / LN 面 / CO 协调 / PR 彩率），再聚合：

$$\text{MM} = 0.6\,s_{(2)} + 0.4\,s_{(3)} + 0.2\,s_{(4)}$$

其中 $s_{(k)}$ 为 6 个技能值降序排列后的第 $k$ 大（刻意跳过最高，防单一技能刷分）。

### 3. BSTv2 —— 标准化融合 + 锚定

$$z(x) = \frac{x - \mu_x}{\sigma_x}$$

$$\text{BSTv2} = A + B \left( 0.4\,z(\text{BST}) + 0.6\,z(\text{MM}) \right)$$

冻结参数（由全库一次标定，单图分值不随库增减漂移）：

$$\mu_{\text{BST}} = 7.319,\quad \sigma_{\text{BST}} = 3.589,\quad \mu_{\text{MM}} = 3.585,\quad \sigma_{\text{MM}} = 1.776,\quad A = 6.690,\quad B = 3.709$$

$A, B$ 由 REFORM 全阶梯最小二乘锚定，使 REFORM 10 段 $\approx 13$。

### 4. 分桶与命名

$$\text{bucket} = \frac{\lfloor\, 2 \cdot \text{BSTv2} \,\rfloor}{2} \quad (\text{每 } 0.5 \text{ 一桶})$$

收藏夹名 = `[4k]` + 桶号：整数 $0$–$9$ 用阿拉伯数字、$10$–$18$ 用罗马 `X`–`XVIII`、$\ge 19$ 用 `Z`（避免 `XIX` 破坏字典序）；`.5` 半桶加后缀 `+`。

例：`[4k]0`、`[4k]0+`、…、`[4k]9+`、`[4k]X`、`[4k]X+`、…、`[4k]XVIII+`、`[4k]Z`。

## 本机环境(已装好,供排错)
- Python 3.12 + matplotlib/fonttools/numpy(`%LOCALAPPDATA%\Programs\Python\Python312`)
- Node.js(`C:\Program Files\nodejs`)— 跑 MSD/ISR/RSR 三个计算器
- MiKTeX + XeLaTeX(`%LOCALAPPDATA%\Programs\MiKTeX`)— 编译中文 PDF;中文用系统字体 SimSun(正文)+ DengXian(标题/图表),无需 Noto
