"""文献观察站 · 抓取/去重/富化/出站流水线。

模块分工:
  common        路径、时间、原子 JSON、标识符归一、配置校验
  arxiv_source  arXiv Atom 客户端与分页守卫
  triage        题录级自动分类(词表来自 config.classify)
  pipeline      合并状态机、事件、逐查询游标、整轮报告
  editorial     人工覆盖层(唯一写 editorial.json 的入口)
  pdf_text      PDF 下载与取页
  enrich        可选的 AI 解读(编号候选句 + 机械校验)
  site          白名单出站与静态产物
"""

__version__ = "1.0.0"
