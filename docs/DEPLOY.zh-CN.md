# 部署到 GitHub Pages(每日云端自动更新)

目标:代码推到 GitHub 后,**每天定时在云端抓一次 arXiv、重建站点、自动发布**,你的电脑不用开机。

整条链路只有一个 workflow:

```
daily.yml  →  update(抓取) → enrich(可选 AI) → build(建站) → commit 状态 → deploy(Pages)
```

---

## 0. 前置条件

| 项 | 说明 |
|---|---|
| GitHub 账号 | 免费账号即可 |
| 仓库可见性 | **必须 public**。GitHub Pages 在 private 仓库上要付费计划(Pro/Team)才能发布 |
| 仓库名 | 建议 `paper-radar` 这类;若你喜欢 `https://<用户名>.github.io/` 这种根地址,仓库名就叫 `<用户名>.github.io` |
| 本地 | 装了 git;Python 只在你想本地预览时才需要 |

---

## 1. 先改三处占位符

打开 `config.json`,把 `YOUR-NAME` 换成你的用户名(第 4、6、193 行):

```jsonc
"site_url":   "https://YOUR-NAME.github.io/observatory",     // 第 4 行:改成你的 Pages 地址
"user_agent": "observatory/1.0 (+https://github.com/YOUR-NAME/observatory)",  // 第 6 行:arXiv 要求 UA 带联系方式
"review": { "repo": "YOUR-NAME/observatory", ... }            // 第 193 行:不用网页审核就改成 ""
```

- `site_url` 只影响 RSS 里的链接和页面元信息,填错站点也能跑,但订阅链接会错。
- `user_agent` 建议带上仓库地址或邮箱,arXiv 对匿名 UA 更容易限流。
- `review.repo` 设成空字符串 `""`,详情页的「人工审核」按钮会**整块自动消失**(前端有这个判断),这是最省事的功能开关。
- 仓库名如果不用 `observatory`,这三处的路径也要跟着改。

---

## 2. 推到 GitHub

**方式一:网页上传(最省事,不装 git)**

1. 新建仓库(public,不要勾选 README/LICENSE,避免冲突)。
2. 在仓库页 `Add file → Upload files`,把 `observatory/` 里的内容**拖进去**(注意:要上传目录内容,不是整个 `observatory` 文件夹;`.github/` 也要带上,否则定时任务不会生效)。
3. 提交。

> `.github/workflows/` 是隐藏目录,网页上传有时候会漏。上传后在仓库首页确认能看到 `.github/workflows/daily.yml`,看不到就用方式二。

**方式二:命令行(推荐,不漏文件)**

```bash
cd observatory
git init -b main
git add .
git commit -m "feat: literature observatory"
git remote add origin https://github.com/YOUR-NAME/observatory.git
git push -u origin main
```

`dist/`、`.demo/`、`.cache/` 已经在 `.gitignore` 里,不会误传。

---

## 3. 开启 Pages(唯一一个必做设置)

```
仓库页 → Settings → Pages → Build and deployment → Source
```

把 Source 选成 **GitHub Actions**(不要选 "Deploy from a branch")。

> 这一步不做的话,页面不会出现。workflow 里对 Pages 配置做了容错:没开启时只跳过部署、不报错,
> 所以你会看到「运行成功但没有网页」——记得回来检查这里。

---

## 4. 第一次手动跑一次

```
仓库页 → Actions → 左侧选 daily → 右侧 Run workflow
```

参数:

| 参数 | 填什么 | 说明 |
|---|---|---|
| `refresh` | ✅ 勾上 | 关掉就只重建站点、不联网抓取 |
| `days` | 留空 | 留空=按游标增量;想一次回补就填 `90`(仍按 30 天一个窗口逐块推进) |
| `enrich_limit` | 留空 | 本轮最多生成几篇 AI 解读;留空=用 `config.json` 里的上限 |

跑完大约 2–5 分钟(不配 AI 时通常 1 分钟内)。成功后打开 `https://YOUR-NAME.github.io/observatory/`。

首次运行会抓最近 30 天(`arxiv.initial_lookback_days`),之后每天只抓增量。

---

## 5. 之后每天自动更新

`daily.yml` 里已经定好了:

```yaml
on:
  schedule:
    - cron: "35 0 * * *"     # UTC 00:35 = 北京时间 08:35
```

改时间就改这一行(**cron 是 UTC**)。例如想北京时间 07:00 跑:`cron: "0 23 * * *"`(前一天 23:00 UTC)。

注意两点:

1. **GitHub 的定时任务不保证准点**,高峰期可能延迟几分钟到几十分钟;仓库连续 60 天没有任何提交活动时,
   GitHub 会自动停用 schedule,你手动跑一次或提交一次就能恢复。
2. 每次运行会把 `data/store.json` 提交回仓库(这是「上周到底抓成功没有」能查的原因)。
   这个提交会再触发一次 push 触发器的运行,但那一次**只重建站点、不抓取**、也没有 diff 可提交,所以不会死循环 —— 你会在 Actions 里看到成对的两条记录,属正常现象。

---

## 6. 可选:开启 AI 解读

不配也能用,只是站在「仅摘要」状态。要开就加一个 Secret:

```
Settings → Secrets and variables → Actions → Secrets → New repository secret
    Name:  LLM_API_KEY
    Value: sk-...
```

再用 Variables 指定模型(不填则用 `config.json` 的默认值):

```
Settings → Secrets and variables → Actions → Variables
    LLM_BASE_URL = https://api.deepseek.com/v1      # 任何 OpenAI 兼容端点
    LLM_MODEL    = deepseek-chat
```

成本闸门在 `config.json` 的 `llm` 段:`max_per_run`(每轮最多几篇)、`max_per_utc_day`(每天上限),
发请求前先记账,同一版本只生成一次。**想彻底关掉**:把 `llm.enabled` 改成 `false`。

---

## 7. 跑完怎么验证是真的成功了

按顺序看这四处,能定位 90% 的问题:

1. **Actions 列表**:最后一次 daily 是绿色。
2. **Step Summary**(运行详情页底部):有「本轮抓取状态」表格,每个来源标 `ok` / `failed`。
   - `failed` + `HTTP 429` → 被 arXiv 限流,把 `arxiv.request_interval_seconds` 调大到 6–10。
   - `failed` + `network` → 运行环境网络问题,重跑即可。
3. **仓库里的 `data/store.json`**:有 `last_full_success` 时间戳,`events` 数组里有 `discovered` 条目。
4. **站点首页右上角状态卡**:显示「最近一轮抓取成功」+ 新发现的篇数。
   - 显示「抓取失败 · 保留已有库」说明抓取挂了,但站点仍用旧数据构建(这是故意设计的)。
   - 显示「抓取记录已过期」说明超过 48 小时没有完整成功,去 Actions 看原因。

---

## 8. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 运行成功但网页 404 | Pages 没开,或 Source 不是 GitHub Actions | 回第 3 步 |
| 网页是旧内容 | 部署 job 被跳过 | 看 `build` job 的 `pages_ready` 输出是否为 false |
| 报 `remote: Permission denied` | 仓库有分支保护,或 workflow 权限被收紧 | Settings → Actions → General → Workflow permissions 选 "Read and write permissions" |
| 报 `! [rejected]`(push 冲突) | 你本地同时改了 `data/store.json` | workflow 已经会 `git pull --rebase --autostash`;实在冲突就本地 `git pull --rebase` 后重跑 |
| 每天都是 0 篇 | 检索式太窄,或 arXiv 分类不对 | 用 `days=30` 手动跑一次回补;检查 `arxiv.queries` 的布尔式 |
| 首页一直「正在读取状态」 | `data.js` 没生成(构建失败) | 看 build 步骤日志,通常是 `web/` 三个文件缺失 |

---

## 9. 本地改完再推

```bash
cd observatory
python scripts/demo.py                      # 离线演示,写 .demo/,不碰 data/(看一眼版式)
python -m http.server -d dist 8000          # 打开 http://127.0.0.1:8000

python -m unittest discover -s tests -v     # 30 个离线测试
node tests/frontend_check.js dist           # 前端功能检查(需要 node)
```

改完 `git add -A && git commit && git push`,push 触发的那次运行会重建并部署(不抓取)。

---

## 10. 附:以后想「减功能」时,开关在哪

你说想按自己的想法简化功能 —— 下面这张表是各个功能对应的关法,**大部分不用改代码**:

| 想关掉 | 怎么关 | 影响 |
|---|---|---|
| AI 解读 | `config.llm.enabled = false`,或不配 `LLM_API_KEY` | 详情页只显示摘要,标注「仅摘要」 |
| 网页人工审核 | `config.review.repo = ""` | 详情页审核按钮整块消失 |
| Issue 审核工作流 | 删 `.github/workflows/review.yml` | 不再响应 Issue(配合上一行) |
| 独立测试 workflow | 删 `.github/workflows/tests.yml` | 测试只在 daily 第一步跑(仍然有防护) |
| 版本巡检 | `config.arxiv.revision_batch_size = 0` | 不再复查已有论文是否出了新版 |
| 首页专题卡片 | 删掉 `config.topics` 里的条目 | 一篇论文可属多个专题的分组视图消失 |
| 研究方向标签(DA/DG/…) | 精简 `config.classify.tracks` | 注意:同步修改 `topics` 里引用的 id,否则启动即报错 |
| 既有题录(seed) | 保持 `data/seed.json` 为 `[]` | 无影响 |
| 收藏 / 搜索 / 排序 | 需要改 `web/app.js` | 纯前端,前端逻辑都在一个文件里 |
| RSS / RIS 导出 | 删首页 nav 里两个链接 + `atlas/site.py` 对应输出 | 少两个产物文件 |
