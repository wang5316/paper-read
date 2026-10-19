# NOTICE

本项目的实现思路来自下面的开源项目,并在其基础上重写了配置系统、专题计算与引文链接:

**3D-Semantic-Segmentation-Domain-Adaptation** ("3D Segmentation Observatory")
- 作者:3D Segmentation Observatory contributors
- 仓库:https://github.com/nuyoah0123/3D-Semantic-Segmentation-Domain-Adaptation
- 许可:MIT License, Copyright (c) 2026 3D Segmentation Observatory contributors
  (该仓库的 LICENSE 在 MIT 正文后加了一句「仅覆盖原创代码与文档,不覆盖抓取到的第三方论文内容」,
  因此 GitHub 的许可证识别器把它标为 `NOASSERTION` 而不是 `MIT`;对代码复用的条件没有实质变化。)
- 站点:https://nuyoah0123.github.io/3D-Semantic-Segmentation-Domain-Adaptation/

保留下来的设计(这些是本项目认为最有价值的部分):
- 每个检索式独立游标,只有被证明完整的窗口才前移;
- `identity_keys` 强标识去重 + 身份冲突时不合并;
- 人工覆盖层与抓取链物理隔离,`reviewed_version` 绑定版本号;
- AI 解读使用编号候选句 + `{"snippet": "S12"}` 引用 + 数字落地校验。

改动与修复见 `README.zh-CN.md` 第 9 节。

MIT License 全文见 `LICENSE`。该许可只覆盖代码与文档,不覆盖抓取到的第三方论文题录、摘要、
图表等外部内容 —— 那些内容的版权归各自作者所有。
