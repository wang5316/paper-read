# 文献观察站 · Literature Observatory

一个「活的」论文观察站:每天抓取指定研究方向的 arXiv 新论文,自动去重与分类,
生成静态网站 + RSS + RIS,并把**抓取是否真的成功**如实记录下来。

它不是论文推荐系统 —— 自动分类只是检索线索,人工审核与 AI 解读都带边界声明。

---

## 1. 三条核心设计

**① 游标与「成功」强绑定**
每个检索式各自持有一个 `completed_until` 游标,只有整窗请求**完整成功**(含分页校验)才前移。
拿不到 `totalResults`、中间页为空、跨页重复 ID、翻页无进展 —— 统统算失败,而不是「0 条结果」。
所以 `matched=0 && status=ok` 才能说「这段时间真的没有新论文」。

**② 身份靠强标识,标题只报警不合并**
`identity_keys()` 产出 `arxiv:<id>`(去版本号)与 `doi:<doi>`(含 `10.48550/arxiv.*` 反推)两类键。
命中多个记录 → `identity-conflict` 事件,整条丢弃(宁可留一个,也不错并两条)。
标题归一化后相同只写一条 `warnings`,不合并。

**③ 人工层物理隔离**
`data/editorial.json` 只由 `apply_review()` 写(命令行与 Issue 工作流共用同一入口),抓取链只读它。
`reviewed_version` 把「已核对」绑定到具体版本号,论文出了新版自动退化为 `review-stale`。

## 2. AI 解读为什么不会编造引用

模型看到的不是 PDF 正文,而是切成编号候选句的列表:

```
S1 [p.1] Unsupervised domain adaptation for 3D semantic segmentation ...
S2 [p.1] We propose a self-training framework with pseudo-label class balancing ...
```

每条结论必须用 `{"snippet": "S12"}` 引用。存库时 `quote` 直接取自被引句 —— 引用文字**在物理上**不可能被编造。
再加一层机械校验:结论里的每个数字都必须出现在被引句里,否则先在全部候选句里补挂证据,
补不上就丢弃该条(`problem`/`method` 挂了则整篇拒收)。

没有 `LLM_API_KEY` 时整层跳过(`status=not-configured`),站点照常构建。

## 3. 快速开始

**看版式(离线,无需网络与密钥):**

```bash
python scripts/demo.py          # 演示数据写在 .demo/,不会污染真正的 data/
python -m http.server -d dist 8000   # 打开 http://127.0.0.1:8000
```

**真抓一轮:**

```bash
pip install -r requirements.txt
python scripts/update.py          # 抓 arXiv,写 data/store.json
python scripts/enrich.py          # 可选:生成 AI 解读(需要 LLM_API_KEY)
python scripts/build.py           # 生成 dist/
```

**只回补某段时间:** `python scripts/update.py --days 90`(仍按 30 天一个窗口逐块推进,`backlog_days` 会如实写进状态)。

**本地起个静态服务:** `python -m http.server -d dist 8000`

**跑测试:** `python -m unittest discover -s tests -v`(30 个,全部离线,不需要网络与密钥)

**前端功能检查:** `python scripts/demo.py && node tests/frontend_check.js dist`
(在 Node 里用一个极简 DOM 桩真跑 `app.js`:渲染卡片、打开详情、搜索、筛选、空库状态)

## 4. 目录结构

```
config.json              唯一需要改的配置:检索式、分类词表、专题、上限
atlas/
  common.py              路径 / 时间 / 原子写 / 标识符归一 / 配置校验
  arxiv_source.py        arXiv Atom 客户端 + 分页守卫 + 版本巡检
  triage.py              题录级自动分类(词表来自 config.classify)
  pipeline.py            合并状态机、事件、逐查询游标、整轮报告
  editorial.py           人工覆盖层(唯一写 editorial.json 的入口)
  pdf_text.py            PDF 下载 / 取页 / 选页
  enrich.py              编号候选句 + 机械校验的 AI 解读
  site.py                白名单出站 + 静态产物
scripts/                 update / enrich / build / review / status / demo
web/                     index.html + style.css + app.js(无构建、无依赖)
data/
  store.json             抓取状态(records / queries / events / runs)
  seed.json              策展题录(既有文献,参与去重)
  editorial.json         人工覆盖(精选、归档、公开备注)
.github/workflows/       daily(抓取+部署) / review(Issue 审核) / tests
dist/                    构建产物(gitignore)
```

## 5. 换成你自己的研究方向

只改 `config.json`,代码不用动。五个块:

**① `arxiv.queries` —— 抓什么**
arXiv API 的布尔检索式,一条一个 `id`。建议写成三段:`<领域词> AND <任务词> AND <设定词>`,例如

```
(all:"3D" OR all:"point cloud" OR all:LiDAR) AND (all:segmentation) AND (all:"domain adaptation")
```

改动某个 `query` 字符串会让该查询的 `query_hash` 变化 → 自动回退到 `initial_lookback_days` 重建基线。

**② `classify.scope` —— 什么算「你的领域」**
一个必中的词表;命中即为范围内,否则 `bucket=excluded`,不会出现在站点上。

**③ `classify.task` / `classify.tracks` / `classify.methods` —— 怎么分类**
`task.primary` 决定「核心方向」的门槛(`scope ∩ primary ∩ tracks`),`tracks` 是研究方向多标签。

**④ `topics` —— 首页的专题卡片**
`match` 只能引用 `classify.tracks` / `classify.methods` 里真实存在的 id(配置校验会拦住写错的),
外加 `keywords`(标题/摘要/期刊/方法名的子串)、`venue_named`、`abstract_only` 三种软匹配。

**⑤ 文案** —— `name` / `name_zh` / `site_url` / `display_timezone` / `review.repo`。

> 上游原项目在这里有个坑:它的专题词表用了分类器从不产出的方法名,导致 3 个专题永远是空的。
> 本项目把词表统一到 `classify`,并在 `load_config()` 里直接报错,写错就启动失败。

## 6. 数据契约

`dist/data.js` 就是 `window.ATLAS = {...}`,前端只读它:

```jsonc
{
  "site":   { "name", "name_zh", "url", "timezone", "repo", "preview" },
  "classify": { "tracks": [{ "id", "name" }], "tasks": { "semantic": "语义分割" } },
  "status": { "status": "ok|partial|failed|not-run", "at", "counts", "sources": [], "note" },
  "last_full_success": "…Z",
  "index_count": 2,
  "topics": [{ "group", "desc", "items": [{ "id", "name", "desc", "match" }] }],
  "papers": [{
    "id": "arxiv:2610.00855",
    "title", "authors", "published", "updated", "version", "venue",
    "identifiers": { "arxiv": [], "doi": [] },
    "first_seen", "review_status", "imported",
    "classification": { "bucket": "core|related|excluded", "tracks", "task", "methods", "note", "basis", "certainty" },
    "abstract", "abstract_source", "oa_pdf", "url",
    "ai_draft": {
      "summary_zh",
      "problem": { "text", "evidence": [{ "page", "quote", "snippet", "source" }] },
      "method": { "text", "evidence": [] },
      "findings": [{ "text", "evidence": [] }],
      "open_problems": [{ "text", "kind": "limitation|future-work", "evidence": [] }],
      "insights": [{ "text", "evidence": [] }],
      "role": { "setting", "supervision", "modalities", "datasets", "evaluation" },
      "model", "prompt_version", "scope", "generated_at", "source_hash"
    },
    "public_note": "人工公开笔记",
    "topics": ["da", "selftrain"]
  }],
  "events": [{ "kind": "discovered|revised|metadata-updated", "paper_id", "title", "at" }]
}
```

出站走白名单(`site.PUBLIC_FIELDS`),`warnings` / `versions` / `last_seen` 等内部字段永远不会出现在网页上。
`data.js` 会把 `<` 与 U+2028/2029 转义,防止题名里的脚本注入。

## 7. 人工审核

**命令行:**

```bash
python scripts/review.py 'arxiv:2610.00855' --state metadata-checked
python scripts/review.py 'arxiv:2610.00855' --bucket core --tracks DA OV --note "重点跟进"
```

**网页:** 详情页的按钮会打开一条预填好的 GitHub Issue(`<!-- observatory-review -->` + `key: value` 行),
`review.yml` 收到后写回 `data/editorial.json`、重建、部署,并评论 + 关闭 Issue。
只有仓库所有者开的 Issue 会被处理。

合法值:`candidate` / `imported-unreviewed` / `selected` / `metadata-checked` / `abstract-checked` /
`full-text-read` / `rejected`;`bucket` 为 `core` / `related` / `excluded`。

## 8. 部署到 GitHub Pages

1. 建仓库并推上代码(把 `config.json` 里的 `site_url`、`review.repo` 改成你的)。
2. Settings → Pages → Source 选 **GitHub Actions**(不要选 branch)。
3. (可选)Settings → Secrets 加 `LLM_API_KEY`;Variables 加 `LLM_BASE_URL`、`LLM_MODEL`。
4. Actions 里手动跑一次 `daily`,勾上 `refresh=true`。

之后每天 UTC 00:35 自动跑;`partial`(部分来源失败)不阻断发布,只有整轮 `failed` 才会标红。

**成本:** 抓取与建站零成本。AI 解读是唯一的钱:`config.llm.max_per_run`(每轮 8 篇)
与 `max_per_utc_day`(每天 24 篇)双闸门,并在发请求**之前**先记账落盘 —— 超时或崩溃也不会少算。
`source_hash = digest([title, abstract, version])` 保证同一版本只生成一次,论文改版才会重做。

## 9. 与上游项目的对照

本站点是对 [`Nuyoah0123/3D-Semantic-Segmentation-Domain-Adaptation`](https://github.com/nuyoah0123/3D-Semantic-Segmentation-Domain-Adaptation)
(MIT)实现思路的重写,保留了它最好的几个设计(游标/身份/人工隔离/编号候选句),并修掉了几处真实缺陷:

| 上游问题 | 本项目 |
|---|---|
| 专题词表与分类器输出词表不一致,3 个专题永远为空 | 词表统一到 `classify`,写错直接 `ConfigError` |
| 专题匹配逻辑前后端各写一份 | 只在服务端算一次,写进 `p.topics` |
| 引文页码链接只认 arXiv 号,`oa-pdf` 记录点不动 | 优先用 `evidence.source`,再回退到 arXiv 拼接 |
| `store.json` 单文件膨胀(上游已 4 MB) | 仍单文件,但事件按 5000、报告按 90 轮截尾;需要时可拆 `events.jsonl` |
| 无配置化能力(方向写死在 triage.py) | 全部词表在 `config.json`,换方向只改配置 |

## 10. 已知边界

- **只管 arXiv**。期刊/会议正式版需要额外接 OpenAlex/Crossref(上游有,这里留作扩展点)。
- **没有 PDF 就无法生成 AI 解读**,该条只显示摘要,并明确标注「仅摘要」。
- **自动分类是关键词布尔判断**,不理解语义。命中背景描述很正常,页面上一律带免责声明。
- **不做指标排名**。不同数据集与监督条件下的 mIoU 不能直接比较,这是刻意不做的功能。
- **`revisions` 巡检严格**:请求的编号必须全部返回,撤稿条目会让那一轮 `partial`,需要人工清理。

## 许可

代码 MIT。上游实现的署名见 `NOTICE.md`。
抓取到的论文题录与摘要版权归原作者,本站只做索引与链接。
