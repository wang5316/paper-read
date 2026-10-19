# Paper Read · 论文观察站

每天自动抓取 arXiv 上与 **3D 语义分割 / 域适应 / 域泛化 / 开放词汇 / 测试时适配**相关的新论文,
去重、分类,并发布成一个静态站点。

| 入口 | 地址 |
| --- | --- |
| **站点** | https://wang5316.github.io/paper-read/ |
| **RSS 订阅** | https://wang5316.github.io/paper-read/feed.xml |
| **近七天题录(.ris)** | https://wang5316.github.io/paper-read/recent.ris |
| **全库题录(.ris)** | https://wang5316.github.io/paper-read/catalog.ris |

## 它做什么

- **每天北京时间 08:35** 在 GitHub Actions 上醒来,按配置的检索式增量抓取 arXiv
- **去重**:按 arXiv 编号与 DOI 归并,论文出了新版本会标成「新版本」而不是「新论文」
- **分类**:按研究方向(DA/DG/OV/TTA)、方法路线、应用场景打标签,首页可按专题浏览
- **记录抓取状态**:每个检索式单独报告成功/失败。**没有结果不等于抓取成功**
- **产出**:静态站点、RSS、RIS 题录、运行摘要

## 换成你自己的研究方向

只需要改 `config.json` 三块,代码不用动:

| 块 | 作用 |
| --- | --- |
| `arxiv.queries` | 抓什么(arXiv 布尔检索式) |
| `classify` | 怎么分类(领域词表、研究方向、方法) |
| `topics` | 首页专题怎么分组 |

完整部署与改造说明见 [docs/DEPLOY.zh-CN.md](docs/DEPLOY.zh-CN.md),架构与数据契约见
[README.zh-CN.md](README.zh-CN.md)。

## 技术实现

- 后端:Python 标准库(唯一可选依赖是 AI 解读用的 PyMuPDF)
- 前端:`web/` 下三个文件,无框架、无构建、无外部字体请求
- 状态:`data/store.json` 记录抓取游标与论文库,每天随运行提交回仓库
- 站点构建产物不进仓库,由 Actions 直接发布到 Pages

## 许可

代码 MIT,见 [LICENSE](LICENSE)。抓取到的论文题录、摘要版权归原作者,本站只做索引与链接。
