# 4K 难度分级 — 输出说明

本目录由「osu!mania 4K 复合难度分级工作流」自动生成 / 更新。**已在本机(Windows)本地化**:不再依赖沙箱,直接读硬盘上的真实 `client.realm`,可手动或每晚自动运行。

## 文件
- **4k_collections.db** — 可导入 osu!lazer 的收藏夹文件。按复合难度分桶(`4k-00.0` … `4k-19.0`,每 0.5 一桶),并**原样保留所有手工收藏夹**(即名字不以 `4k-` 开头的,如当前的 `10SKIP`),导入时不会丢。
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

## 复合难度公式(参考)
`scaled = 1.30 · base · (1 + 0.18·lnr + 0.12·hb)`,其中 `base = (MSD + 4·ISR + 4·RSR) / 9`,`lnr` = 长条音符占比,`hb = 4·lnr·(1−lnr)`。Reform 10 段锚定 ≈ 13。

## 本机环境(已装好,供排错)
- Python 3.12 + matplotlib/fonttools/numpy(`%LOCALAPPDATA%\Programs\Python\Python312`)
- Node.js(`C:\Program Files\nodejs`)— 跑 MSD/ISR/RSR 三个计算器
- MiKTeX + XeLaTeX(`%LOCALAPPDATA%\Programs\MiKTeX`)— 编译中文 PDF;中文用系统字体 SimSun(正文)+ DengXian(标题/图表),无需 Noto
