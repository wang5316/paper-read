/* 前端功能检查:在 Node 里用一个极简 DOM 桩跑 dist/app.js,
   验证「读 data.js -> 过滤 -> 渲染卡片 -> 打开详情 -> 搜索」这条链真的能跑通,
   而不是只看语法。用法:node tests/frontend_check.js dist */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const dist = process.argv[2] || path.join(__dirname, "..", "dist");

function makeElement(id) {
  return {
    id,
    _html: "",
    _text: "",
    value: "",
    hidden: false,
    open: false,
    handlers: {},
    children: [],
    get innerHTML() {
      return this._html;
    },
    set innerHTML(value) {
      this._html = String(value);
    },
    get textContent() {
      return this._text;
    },
    set textContent(value) {
      this._text = String(value);
    },
    addEventListener(type, handler) {
      this.handlers[type] = handler;
    },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
    click() {
      if (typeof this.onclick === "function") this.onclick();
    },
    remove() {},
    closest() {
      return null;
    },
    scrollIntoView() {},
  };
}

const ids = [
  "health-title", "health-time", "new-count", "rev-count", "last-success", "preview-note",
  "library-count", "seed-count", "core-count", "reviewed-count", "ai-count", "built",
  "brand-en", "footer-name", "topic-groups", "topics", "library", "topic-crumb",
  "search", "track", "bucket", "scope", "sort", "reset", "results", "export",
  "papers", "empty", "more", "sources", "events", "detail", "detail-body", "close",
];
const store = {};
for (const id of ids) store[id] = makeElement(id);

const document = {
  title: "",
  getElementById(id) {
    if (!store[id]) store[id] = makeElement(id);
    return store[id];
  },
  createElement() {
    return makeElement("created");
  },
  body: { appendChild() {} },
};

const location = { hash: "", search: "", pathname: "/index.html" };
const sandbox = {
  document,
  location,
  history: { replaceState() {} },
  localStorage: { getItem: () => null, setItem() {} },
  URLSearchParams,
  Intl,
  Date,
  URL,
  JSON,
  console,
  setTimeout,
  Blob: class Blob {},
  encodeURIComponent,
  decodeURIComponent,
};
sandbox.window = { ATLAS: undefined, addEventListener() {}, document, location };

const context = vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(dist, "data.js"), "utf8"), context, { filename: "data.js" });
const payload = sandbox.window.ATLAS;
vm.runInContext(fs.readFileSync(path.join(dist, "app.js"), "utf8"), context, { filename: "app.js" });

const failures = [];
const expect = (label, condition, detail) => {
  const line = `  ${condition ? "ok  " : "FAIL"} ${label}${!condition && detail ? " -> " + detail : ""}`;
  console.log(line);
  if (!condition) failures.push(label);
};

console.log(`data.js: ${payload.papers.length} papers, status=${payload.status.status}`);

if (payload.papers.length === 0) {
  // 空库:必须是「等待首次自动发现」而不是假装成功
  expect("空库状态文案", store["health-title"].textContent === "等待首次自动发现", store["health-title"].textContent);
  expect("空库计数为 0", store["library-count"].textContent === "0", store["library-count"].textContent);
  expect("空态提示可见", store["empty"].hidden === false);
  expect("结果条为 0", /找到 0 篇/.test(store["results"].textContent), store["results"].textContent);
  expect("来源区给出引导", /还没有抓取记录/.test(store["sources"].innerHTML));
} else {
  expect("抓取状态文案", store["health-title"].textContent === "最近一轮抓取成功", store["health-title"].textContent);
  expect("库内文献数", store["library-count"].textContent === String(payload.papers.length), store["library-count"].textContent);
  expect("核心方向计数 > 0", Number(store["core-count"].textContent) > 0, store["core-count"].textContent);
  expect("卡片渲染出题名", store["papers"].innerHTML.includes(payload.papers[0].title));
  expect("卡片渲染出研究方向标签", /chip track/.test(store["papers"].innerHTML));
  expect("结果条", /找到 \d+ 篇/.test(store["results"].textContent), store["results"].textContent);
  expect("专题分组", store["topic-groups"].innerHTML.includes("研究设定"));
  expect("来源状态", store["sources"].innerHTML.includes(payload.status.sources[0].source));
  expect("事件区", /新收录/.test(store["events"].innerHTML));
  expect("方向下拉已填充", store["track"].children.length >= 4, "children=" + store["track"].children.length);

  const withAI = payload.papers.find((paper) => paper.ai_draft);
  if (withAI) {
    store["papers"].handlers.click({
      target: {
        closest: (selector) => (selector === "[data-open]" ? { dataset: { open: withAI.id } } : null),
      },
    });
    const detail = store["detail-body"].innerHTML;
    expect("详情页标题", detail.includes(withAI.title));
    expect("详情页 AI 五段", detail.includes("① 解决的问题") && detail.includes("⑤ 对后续读者的启发"));
    expect("引文块带页码链接", /blockquote class="evidence"[\s\S]*PDF p\.1/.test(detail));
    expect("详情页审核入口", detail.includes("人工审核") && detail.includes("issues/new"));
    expect("弹层已打开", store["detail"].open === true);
  }

  const rerun = store["search"].handlers.input;
  store["scope"].value = "all";
  store["search"].value = "segmentation";
  rerun();
  expect("搜索有结果", !/找到 0 篇/.test(store["results"].textContent), store["results"].textContent);
  store["search"].value = "";
  rerun();
  store["scope"].value = "ai";
  store["scope"].handlers.change();
  const aiCount = payload.papers.filter((paper) => paper.ai_draft).length;
  expect("只看 AI 解读", store["results"].textContent.includes(`找到 ${aiCount} 篇`), store["results"].textContent);
}

console.log(failures.length ? `\nFAILED: ${failures.length}` : "\nALL FRONTEND CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
