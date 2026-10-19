"use strict";

/* 前端只做三件事:读 window.ATLAS、按筛选条件过滤、把一条记录渲染成详情。
   专题归属由生成器在服务端算好(p.topics),这里不再维护第二套匹配逻辑。 */

const D = window.ATLAS || { papers: [], events: [], topics: [], status: {}, site: {} };
const $ = (id) => document.getElementById(id);

const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char])
  );

const validURL = (value) => {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username ? url.href : "";
  } catch {
    return "";
  }
};

const date = (value) => (value ? String(value).slice(0, 10) : "日期未提供");

function time(value) {
  if (!value) return "尚无记录";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: (D.site && D.site.timezone) || "UTC",
    }).format(new Date(value));
  } catch {
    return String(value);
  }
}

const STATES = {
  candidate: "自动发现 · 待审核",
  "imported-unreviewed": "既有题录 · 待审核",
  "review-stale": "版本已变化 · 需重新核对",
  selected: "人工精选",
  "metadata-checked": "题录已核对",
  "abstract-checked": "摘要已核对",
  "full-text-read": "人工全文阅读",
};

const TASKS = Object.assign(
  { other: "其他 / 支撑" },
  (D.classify && D.classify.tasks) || {},
  { "segmentation-unspecified": "分割任务待核对" }
);

const ROLE_LABELS = {
  setting: "任务设置",
  supervision: "监督条件",
  modalities: "模态与数据",
  datasets: "主要数据集",
  evaluation: "评测口径",
};
const SCOPE_LABEL = { "pdf-evidence": "arXiv 全文 PDF", "oa-pdf": "开放获取全文 PDF" };
const OPEN_KIND = { limitation: "作者承认的局限", "future-work": "作者提出的未来方向" };
const EVENT_KIND = { discovered: "新收录", revised: "新版本", "metadata-updated": "题录更新" };

let favorites = new Set();
try {
  favorites = new Set(JSON.parse(localStorage.getItem("observatory-favorites") || "[]"));
} catch {
  favorites = new Set();
}

let shown = 24;
let filtered = [];
const CUTOFF = Date.now() - 7 * 86400000;
const recent = new Set(
  (D.events || []).filter((e) => e.kind === "discovered" && Date.parse(e.at) >= CUTOFF).map((e) => e.paper_id)
);
const revised = new Set(
  (D.events || []).filter((e) => e.kind === "revised" && Date.parse(e.at) >= CUTOFF).map((e) => e.paper_id)
);
const isSelected = (paper) =>
  ["selected", "metadata-checked", "abstract-checked", "full-text-read"].includes(paper.review_status);

/* ------------------------------------------------------------------ 状态区 */

const STATUS_NAMES = {
  "not-run": "等待首次自动发现",
  ok: "最近一轮抓取成功",
  partial: "部分来源需要检查",
  failed: "抓取失败 · 保留已有库",
};

function renderHeader() {
  const status = D.status || {};
  const counts = status.counts || {};
  if (D.site && D.site.name_zh) document.title = D.site.name_zh;

  $("health-title").textContent = STATUS_NAMES[status.status] || status.status || STATUS_NAMES["not-run"];
  if (status.status === "ok" && D.last_full_success && Date.now() - Date.parse(D.last_full_success) > 48 * 3600000) {
    $("health-title").textContent = "抓取记录已过期 · 请检查 Actions";
  }
  $("health-time").textContent = "最近尝试:" + time(status.at);
  $("last-success").textContent = "上次完整成功:" + time(D.last_full_success);
  $("new-count").textContent = (counts.new ?? "—");
  $("rev-count").textContent = (counts.revised ?? "—");
  $("library-count").textContent = D.papers.length;
  $("seed-count").textContent = D.index_count ?? "—";
  $("core-count").textContent = D.papers.filter((p) => (p.classification || {}).bucket === "core").length;
  $("reviewed-count").textContent = D.papers.filter(isSelected).length;
  $("ai-count").textContent = D.papers.filter((p) => p.ai_draft).length;
  $("built").textContent = "页面构建于 " + time(D.built_at);

  if (D.site && D.site.preview) {
    $("preview-note").hidden = false;
    $("preview-note").textContent = "本地预览:展示本地库内容;不代表已完成联网抓取,也不代表已经发布。";
  }
  if (D.site && D.site.name_zh) {
    $("footer-name").textContent = D.site.name_zh + " · Daily discovery, deliberate reading.";
    $("brand-en").textContent = D.site.name ? String(D.site.name).toUpperCase().slice(0, 24) : "LITERATURE";
  }
  const tracks = (D.classify && D.classify.tracks) || [];
  const select = $("track");
  for (const track of tracks) {
    const option = document.createElement("option");
    option.value = track.id;
    option.textContent = track.name || track.id;
    select.appendChild(option);
  }
}

/* ------------------------------------------------------------------ 专题 */

const TOPIC_ITEMS = (D.topics || []).flatMap((group) => group.items || []);
const topicById = (id) => TOPIC_ITEMS.find((topic) => topic.id === id);
const topicCount = (topic) => D.papers.filter((paper) => (paper.topics || []).includes(topic.id)).length;
let activeTopic = "";

function renderTopics() {
  const host = $("topic-groups");
  if (!host || !TOPIC_ITEMS.length) return;
  host.innerHTML = (D.topics || [])
    .map(
      (group) => `<div class="topicgroup">
        <div class="tgrouphead"><h3>${esc(group.group)}</h3><span class="muted">${esc(group.desc || "")}</span></div>
        <div class="topiccards">${(group.items || [])
          .map(
            (topic) => `<button class="topiccard${activeTopic === topic.id ? " active" : ""}" data-topic="${esc(
              topic.id
            )}" type="button"><b>${esc(topic.name)}</b><span>${topicCount(topic)} 篇</span><small>${esc(
              topic.desc || ""
            )}</small></button>`
          )
          .join("")}</div>
      </div>`
    )
    .join("");
}

function updateCrumb() {
  const box = $("topic-crumb");
  if (!box) return;
  const topic = topicById(activeTopic);
  box.hidden = !topic;
  if (topic) {
    box.innerHTML = `<span class="chiptopic">专题 · ${esc(topic.name)}</span><span class="muted">${topicCount(
      topic
    )} 篇文献</span><button id="topic-clear" class="text-button" type="button">查看全部专题 ×</button>`;
  }
}

function setTopic(id, scroll) {
  activeTopic = id && topicById(id) ? id : "";
  if (activeTopic) {
    if (location.hash !== "#topic=" + activeTopic) location.hash = "topic=" + activeTopic;
  } else if (location.hash.indexOf("#topic=") === 0) {
    history.replaceState(null, "", location.pathname + location.search);
  }
  updateCrumb();
  renderTopics();
  shown = 24;
  render();
  if (scroll && activeTopic) {
    const el = $("library");
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

/* ------------------------------------------------------------------ 列表 */

function render() {
  const query = $("search").value.trim().toLocaleLowerCase();
  const track = $("track").value;
  const bucket = $("bucket").value;
  const scope = $("scope").value;

  filtered = D.papers.filter((paper) => {
    const classification = paper.classification || {};
    if (track && !(classification.tracks || []).includes(track)) return false;
    if (bucket && classification.bucket !== bucket) return false;
    if (scope === "new" && !recent.has(paper.id)) return false;
    if (scope === "revised" && !revised.has(paper.id)) return false;
    if (scope === "selected" && !isSelected(paper)) return false;
    if (scope === "ai" && !paper.ai_draft) return false;
    if (scope === "favorites" && !favorites.has(paper.id)) return false;
    if (activeTopic && !(paper.topics || []).includes(activeTopic)) return false;
    if (!query) return true;
    const haystack = [
      paper.title,
      paper.id,
      paper.abstract,
      ...(paper.authors || []),
      ...(classification.methods || []),
      ...((paper.identifiers || {}).arxiv || []),
      ...((paper.identifiers || {}).doi || []),
    ]
      .join(" ")
      .toLocaleLowerCase();
    return haystack.includes(query);
  });

  const sort = $("sort").value;
  filtered.sort((a, b) => {
    if (sort === "title") return String(a.title).localeCompare(String(b.title));
    const left = String(a[sort] || a.published || "");
    const right = String(b[sort] || b.published || "");
    return right.localeCompare(left) || String(a.id).localeCompare(String(b.id));
  });

  $("results").textContent = `找到 ${filtered.length} 篇 · 显示 ${Math.min(shown, filtered.length)} 篇`;
  $("empty").hidden = filtered.length !== 0;
  $("more").hidden = shown >= filtered.length;

  $("papers").innerHTML = filtered
    .slice(0, shown)
    .map((paper) => {
      const classification = paper.classification || {};
      return `<article class="card">
        <div class="cardtop">
          ${(classification.tracks || []).map((t) => `<span class="chip track">${esc(t)}</span>`).join("")}
          ${paper.ai_draft ? '<span class="chip ai">AI 解读</span>' : ""}
          ${paper.abstract && !paper.ai_draft ? '<span class="chip abstract">仅摘要</span>' : ""}
          <span class="chip">${esc(TASKS[classification.task] || "待核对")}</span>
          <span class="muted">${esc(date(paper.published))}</span>
        </div>
        <h3>${esc(paper.title)}</h3>
        <p class="authors">${esc((paper.authors || []).slice(0, 3).join(" · "))}${
        (paper.authors || []).length > 3 ? " et al." : ""
      }</p>
        <div>${(classification.methods || [])
          .slice(0, 3)
          .map((m) => `<span class="chip">${esc(m)}</span> `)
          .join("")}</div>
        <p class="review">${esc(STATES[paper.review_status] || paper.review_status || "待审核")}${
        recent.has(paper.id) ? " · 近七天新收录" : ""
      }${revised.has(paper.id) ? " · 新版本" : ""}</p>
        <div class="cardfoot">
          <button data-open="${esc(paper.id)}">查看题录与依据 ↗</button>
          ${link(paper, "原文 ↗")}
          <button class="star" data-star="${esc(paper.id)}" aria-label="收藏">${favorites.has(paper.id) ? "★" : "☆"}</button>
        </div>
      </article>`;
    })
    .join("");
}

function link(paper, text) {
  const url = validURL(paper.url);
  return url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${text}</a>` : "";
}

/* -------------------------------------------------------------- 详情弹层 */

function pdfPageURL(paper, evidence) {
  const direct = validURL((evidence || {}).source);
  if (direct) return direct;
  const aid = ((paper.identifiers || {}).arxiv || [])[0];
  return aid && evidence && evidence.page
    ? `https://arxiv.org/pdf/${encodeURIComponent(aid)}#page=${encodeURIComponent(evidence.page)}`
    : "";
}

function quoteBlock(paper, evidence) {
  if (!evidence || !evidence.quote) return "";
  const url = pdfPageURL(paper, evidence);
  return `<blockquote class="evidence">${esc(evidence.quote)}${
    url
      ? ` <a href="${esc(url)}" target="_blank" rel="noopener noreferrer">PDF p.${esc(String(evidence.page))} ↗</a>`
      : `<span class="muted"> p.${esc(String(evidence.page ?? "?"))}</span>`
  }</blockquote>`;
}

const evidenceOf = (paper, claim) => (claim.evidence || []).map((item) => quoteBlock(paper, item)).join("");

function claimBlock(paper, claim) {
  if (!claim || !claim.text) return "";
  return `<p>${esc(claim.text)}</p>${evidenceOf(paper, claim)}`;
}

function claimList(paper, items, withKind) {
  return (items || [])
    .map(
      (claim) =>
        `<li>${withKind && OPEN_KIND[claim.kind] ? `<span class="chip kind">${OPEN_KIND[claim.kind]}</span> ` : ""}${esc(
          claim.text
        )}${evidenceOf(paper, claim)}</li>`
    )
    .join("");
}

function aiSection(paper) {
  const draft = paper.ai_draft;
  if (!draft || !draft.summary_zh) return "";
  const roles = draft.role
    ? Object.keys(ROLE_LABELS)
        .filter((key) => draft.role[key])
        .map((key) => `<tr><td>${ROLE_LABELS[key]}</td><td>${esc(draft.role[key])}</td></tr>`)
        .join("")
    : "";
  const open = (draft.open_problems || []).length ? draft.open_problems : draft.limitations || [];
  return `<h3>AI 解读 <span class="chip ai">AI 生成 · 待人工复核</span></h3>
  <p class="disclaimer">由 ${esc(draft.model || "模型")} 依据论文全文生成(${esc(
    SCOPE_LABEL[draft.scope] || draft.scope || "pdf-evidence"
  )});按「问题 → 方法 → 结果 → 未解决 → 启发」五段整理,结论附原文引用与页码。不是人工精读,不能替代原文核对。</p>
  <p><b>中文摘要:</b>${esc(draft.summary_zh)}</p>
  ${draft.problem ? `<h3>① 解决的问题</h3>${claimBlock(paper, draft.problem)}` : ""}
  ${draft.method ? `<h3>② 使用方法</h3>${claimBlock(paper, draft.method)}` : ""}
  ${(draft.findings || []).length ? `<h3>③ 得到的结果</h3><ul class="ai">${claimList(paper, draft.findings)}</ul>` : ""}
  ${
    open.length
      ? `<h3>④ 尚未解决的问题</h3><ul class="ai">${claimList(paper, open, true)}</ul>`
      : `<h3>④ 尚未解决的问题</h3><p class="muted">作者未明确提到局限或未来方向,本条暂缺。</p>`
  }
  ${
    (draft.insights || []).length
      ? `<h3>⑤ 对后续读者的启发 <span class="chip kind">编辑推断</span></h3>
         <p class="disclaimer">以下为基于本文证据的推断,不是作者原文结论,请结合原文核对。</p>
         <ul class="ai">${claimList(paper, draft.insights)}</ul>`
      : `<h3>⑤ 对后续读者的启发</h3><p class="muted">模型没有给出有原文依据的启发,本条暂缺。</p>`
  }
  ${roles ? `<h3>实验设置</h3><table class="roletable"><tbody>${roles}</tbody></table>` : ""}`;
}

function abstractBlock(paper) {
  const source = { arxiv: "arXiv", openalex: "OpenAlex 元数据" }[paper.abstract_source] || paper.abstract_source || "";
  if (!paper.abstract) {
    return `<h3>原始摘要</h3><p class="muted">暂无摘要:该题录尚未匹配到公开摘要来源。</p>`;
  }
  const bits = [];
  if (source) bits.push("来源:" + esc(source));
  if (paper.oa_pdf) {
    bits.push(`<a href="${esc(paper.oa_pdf)}" target="_blank" rel="noopener noreferrer">开放获取 PDF ↗</a>`);
  }
  if (!paper.ai_draft) bits.push("未获取全文 · 仅提供摘要,本站不生成 AI 解读");
  return `<h3>原始摘要${paper.ai_draft ? "" : ' <span class="chip abstract">仅摘要</span>'}</h3>
    <p class="absmeta">${bits.join(" · ")}</p><p>${esc(paper.abstract)}</p>`;
}

const REVIEW_ACTIONS = [
  ["metadata-checked", "题录已核对"],
  ["abstract-checked", "摘要已核对"],
  ["full-text-read", "已读全文"],
  ["selected", "人工精选"],
  ["rejected", "驳回/隐藏"],
];
const REVIEW_BUCKETS = [
  ["core", "核心方向"],
  ["related", "相关方法"],
  ["excluded", "移出列表"],
];

function reviewBlock(paper) {
  const repo = (D.site && D.site.repo) || "";
  if (!repo) return "";
  const makeLink = (label, extra, title) => {
    const body = `<!-- observatory-review -->\npaper_id: ${paper.id}\n${extra}\n\n<!-- 上面两行请勿修改;也可以在下一行补充公开备注(会显示在网站上) -->\n`;
    const url = `https://github.com/${repo}/issues/new?labels=review&title=${encodeURIComponent(
      title
    )}&body=${encodeURIComponent(body)}`;
    return `<a class="reviewbtn" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  };
  const title = (suffix) => `[审核] ${suffix} · ${paper.title.slice(0, 60)}`;
  return `<h3>人工审核</h3>
    <p class="muted">点击按钮会在 GitHub 打开预填好的审核 Issue,提交后由机器人写回仓库并重新部署(仅仓库所有者的提交生效)。当前状态:${esc(
      STATES[paper.review_status] || paper.review_status
    )}。</p>
    <div class="reviewrow">${REVIEW_ACTIONS.map(([value, label]) =>
      makeLink(label, `state: ${value}`, title(label))
    ).join("")}</div>
    <div class="reviewrow">${REVIEW_BUCKETS.map(([value, label]) =>
      makeLink("归类:" + label, `bucket: ${value}`, title(label))
    ).join("")}${makeLink("加公开备注", "note: ", "公开备注")}</div>
    <p class="muted">命令行等价操作:<code>python scripts/review.py '${esc(
      paper.id
    )}' --state metadata-checked</code>,改完 commit &amp; push 即可。</p>`;
}

function showPaper(id) {
  const paper = D.papers.find((item) => item.id === id);
  if (!paper) return;
  const classification = paper.classification || {};
  $("detail-body").innerHTML = `
    <p class="eyebrow">${esc(paper.id)} / PAPER RECORD</p>
    <h2>${esc(paper.title)}</h2>
    <p>${esc((paper.authors || []).join(" · "))}</p>
    <div class="detailmeta">
      <b>研究方向:</b>${esc((classification.tracks || []).join(" / ") || "待确认")}
      <b>任务:</b>${esc(TASKS[classification.task] || "待确认")}<br>
      <b>题录日期:</b>${esc(paper.published || "未提供")}
      <b>版本:</b>${esc(paper.version || "待核对")}<br>
      <b>本站首次发现:</b>${esc(paper.first_seen ? time(paper.first_seen) : "既有导入题录")}<br>
      <b>阅读状态:</b>${esc(STATES[paper.review_status] || paper.review_status)}<br>
      ${link(paper, "打开论文来源 ↗")}
    </div>
    <h3>分类依据与边界</h3>
    <p>${esc(classification.note || "题录级分类;尚未核对全文。")}</p>
    <p class="muted">来源范围:${esc(classification.basis || "title-only")}。数据集、类别映射、训练与推理模态、目标域监督及模型选择协议仍需原文核对。</p>
    ${abstractBlock(paper)}
    ${aiSection(paper)}
    ${paper.public_note ? `<h3>人工公开笔记</h3><p>${esc(paper.public_note)}</p>` : ""}
    ${reviewBlock(paper)}
    <p class="disclaimer">研究方向可以交叉。关键词命中不证明实验设置;不同数据集、类别空间与监督条件下的指标不能直接排名。</p>
    <button class="ghost" id="single-ris">导出这篇题录 RIS ↓</button>`;
  $("single-ris").onclick = () => downloadRIS([paper], paper.id.replace(/[^a-zA-Z0-9-]/g, "_") + ".ris");
  if (!$("detail").open) $("detail").showModal();
}

/* ---------------------------------------------------------------- 导出 */

function downloadRIS(papers, name) {
  const clean = (value) => String(value ?? "").replace(/[\r\n]+/g, " ");
  const ris = papers
    .map((paper) =>
      [
        "TY  - JOUR",
        "TI  - " + clean(paper.title),
        ...(paper.authors || []).map((author) => "AU  - " + clean(author)),
        ...(paper.published ? ["PY  - " + clean(paper.published.slice(0, 4))] : []),
        ...((paper.identifiers || {}).doi || []).slice(0, 1).map((doi) => "DO  - " + clean(doi)),
        ...(validURL(paper.url) ? ["UR  - " + validURL(paper.url)] : []),
        "ER  - ",
        "",
      ].join("\n")
    )
    .join("\n");
  const url = URL.createObjectURL(new Blob([ris], { type: "application/x-research-info-systems;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------------------------------------------------------------- 事件绑定 */

$("papers").addEventListener("click", (event) => {
  const open = event.target.closest("[data-open]");
  if (open) showPaper(open.dataset.open);
  const star = event.target.closest("[data-star]");
  if (star) {
    const id = star.dataset.star;
    if (favorites.has(id)) favorites.delete(id);
    else favorites.add(id);
    try {
      localStorage.setItem("observatory-favorites", JSON.stringify([...favorites]));
    } catch {
      /* 隐私模式下忽略 */
    }
    render();
  }
});

for (const id of ["search", "track", "bucket", "scope", "sort"]) {
  $(id).addEventListener(id === "search" ? "input" : "change", () => {
    shown = 24;
    render();
  });
}

$("reset").onclick = () => {
  $("search").value = "";
  $("track").value = "";
  $("bucket").value = "";
  $("scope").value = "all";
  $("sort").value = "first_seen";
  shown = 24;
  render();
};
$("more").onclick = () => {
  shown += 24;
  render();
};
$("export").onclick = () => downloadRIS(filtered, "observatory-selection.ris");
$("close").onclick = () => $("detail").close();
$("detail").addEventListener("click", (event) => {
  if (event.target === $("detail")) $("detail").close();
});

$("sources").innerHTML =
  (D.status.sources || [])
    .map(
      (source) => `<div class="source"><b>${esc(source.source)}</b><span class="${
        source.status === "ok" ? "good" : "bad"
      }">${source.status === "ok" ? "成功" : "失败"}${
        source.backlog_days ? " · 仍有日期待回查" : ""
      }</span></div>${source.error ? `<p class="muted">${esc(source.error)}</p>` : ""}`
    )
    .join("") ||
  '<p class="muted">还没有抓取记录。跑一次 GitHub Actions 或 python scripts/update.py 后,这里会显示每个查询的状态。</p>';

$("events").innerHTML =
  (D.events || [])
    .slice(-8)
    .reverse()
    .map(
      (event) => `<div class="event"><span class="chip">${esc(EVENT_KIND[event.kind] || event.kind)}</span> ${esc(
        event.title
      )}<small>${esc(time(event.at))}</small></div>`
    )
    .join("") ||
  '<p class="muted">没有已发布的发现或版本事件。既有文献导入不会被伪装成今天的新论文。</p>';

/* ------------------------------------------------------------------ 启动 */

const hashMatch = /^#topic=([\w-]+)$/.exec(location.hash);
if (hashMatch && topicById(hashMatch[1])) activeTopic = hashMatch[1];

renderHeader();
renderTopics();
updateCrumb();
render();

$("topics").addEventListener("click", (event) => {
  const button = event.target.closest("[data-topic]");
  if (button) setTopic(button.dataset.topic, true);
});
$("topic-crumb").addEventListener("click", (event) => {
  if (event.target.closest("#topic-clear")) setTopic("", false);
});
window.addEventListener("hashchange", () => {
  const match = /^#topic=([\w-]+)$/.exec(location.hash);
  setTopic(match ? match[1] : "", false);
});

const initial = new URLSearchParams(location.search).get("paper");
if (initial) showPaper(initial);
