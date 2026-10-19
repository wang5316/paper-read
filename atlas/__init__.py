"""文献观察站 · 抓取/去重/富化/出站流水线。

模块分工:
  common        路径、时间、原子 JSON、标识符归一、配置校验
  arxiv_source  arXiv Atom 客户端与分页守卫
  dblp_source   DBLP 会议论文 SPARQL 客户端与计数/翻页守卫
  openalex_source OpenAlex 期刊文章客户端、游标分页与摘要重建
  triage        题录级自动分类(词表来自 config.classify)
  pipeline      多来源合并状态机、事件、逐来源游标、整轮报告
  editorial     人工覆盖层(唯一写 editorial.json 的入口)
  pdf_text      PDF 下载与取页
  enrich        可选的 AI 解读(编号候选句 + 机械校验)
  site          白名单出站与静态产物
"""

__version__ = "1.0.0"
