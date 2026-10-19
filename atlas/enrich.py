"""可选的 AI 解读:把模型限制在「编号候选句」上作答。

这是整个项目里唯一值得花力气防幻觉的地方,机制只有三条:
  1. 模型拿到的不是 PDF 正文,而是切成 S1…S90 的候选句列表;
  2. 每条结论必须用 {"snippet": "S12"} 引用,存库的 quote 直接取自被引句,
     所以「引用原文」在物理上不可能被编造;
  3. 结论里的每个数字都必须出现在被引句里,否则补挂证据句,补不上就丢弃该条。

没有 key 时整层自动跳过(status=not-configured),站点照常构建。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import pdf_text
from .common import ROOT, digest, iso, read_json, utcnow, write_json

PROMPT_VERSION = "obs-reading-v1"
MAX_SNIPPETS = 90
MAX_SNIPPET_CHARS = 320
MAX_CLAIM_CHARS = 600

ROLE_LABELS = {
    "setting": "任务设置",
    "supervision": "监督条件",
    "modalities": "模态与数据",
    "datasets": "主要数据集",
    "evaluation": "评测口径",
}

SYSTEM_PROMPT = """你是严谨的中文科研文献拆解助手,只服务于一个「文献观察站」。

硬性规则,违反任何一条你的输出都会被程序丢弃:
1. 你只能依据给定的 snippets(编号候选句)作答,禁止引入外部知识或补充细节。
2. 每条结论的 evidence 必须是 [{"snippet": "S12"}] 这种形式,只能填出现过的编号,禁止自己写 quote 文本。
3. 结论里出现的每个数字(指标、百分比、倍数、数据集规模)都必须能在你引用的句子里找到原文。
4. 不要复述摘要,不要吹捧,不要给论文打分。找不到依据就少写一条,不要编。
5. 输出必须是单个 JSON 对象,不要 Markdown、不要代码块。

输出结构:
{
  "summary_zh": "两到三句话的中文摘要,说明做了什么、在什么设定下、主要结论",
  "problem": {"text": "它要解决的问题", "evidence": [{"snippet": "S1"}]},
  "method": {"text": "核心方法(一到两句)", "evidence": [{"snippet": "S4"}]},
  "findings": [{"text": "一条具体结果,带数字时数字必须来自被引句", "evidence": [{"snippet": "S12"}]}],
  "open_problems": [{"text": "作者承认的局限或未来方向", "kind": "limitation 或 future-work", "evidence": [{"snippet": "S20"}]}],
  "insights": [{"text": "对后续读者有依据的启发(可以是推断,但要基于引文)", "evidence": [{"snippet": "S7"}]}],
  "role": {"setting": "", "supervision": "", "modalities": "", "datasets": "", "evaluation": ""}
}
insights 的 evidence 允许为空数组;其它字段的 evidence 不允许为空。"""


class DraftRejected(RuntimeError):
    """整篇拒收;code 会进报告,方便统计到底卡在哪。"""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


class EvidenceError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ 文本切分

_ABBREVIATIONS = (
    "e.g.",
    "i.e.",
    "et al.",
    "Fig.",
    "Figs.",
    "Eq.",
    "Eqs.",
    "Tab.",
    "Sec.",
    "vs.",
    "etc.",
    "resp.",
    "approx.",
    "cf.",
    "w.r.t.",
    "No.",
    "Vol.",
    "pp.",
)
_PROTECT = "\u0001"
_SPLIT = re.compile(r"(?<=[.!?。])\s+")


def split_sentences(text, max_chars=MAX_SNIPPET_CHARS):
    """保守分句:保护缩写与小数点,避免把 'et al. 2024' 切成两半。"""
    text = " ".join(str(text or "").split())
    if not text:
        return []
    protected = text
    for index, token in enumerate(_ABBREVIATIONS):
        protected = protected.replace(token, token.replace(".", _PROTECT))
    protected = re.sub(r"(\d)\.(\d)", lambda m: m.group(1) + _PROTECT + m.group(2), protected)

    sentences, buffer = [], ""
    for piece in _SPLIT.split(protected):
        candidate = (buffer + " " + piece).strip() if buffer else piece.strip()
        if len(candidate) < 20:
            buffer = candidate
            continue
        sentences.append(candidate)
        buffer = ""
    if buffer:
        sentences.append(buffer)
    return [item.replace(_PROTECT, ".").strip()[:max_chars] for item in sentences if item.strip()]


def build_snippets(pages, max_snippets=MAX_SNIPPETS, max_chars=MAX_SNIPPET_CHARS):
    """pages: [(page_no, text)] -> [{id, page, text}],编号稳定且可引用。"""
    snippets = []
    for page_no, text in pages:
        for sentence in split_sentences(text, max_chars=max_chars):
            if len(sentence) < 20 or not any(char.isalpha() for char in sentence):
                continue
            snippets.append({"id": f"S{len(snippets) + 1}", "page": page_no, "text": sentence})
            if len(snippets) >= max_snippets:
                return snippets
    return snippets


# ------------------------------------------------------------------ 数字校验

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?\s*%?")


def numeric_tokens(text) -> set:
    tokens = set()
    for hit in _NUMBER.finditer(str(text or "")):
        raw = hit.group(0).replace(",", "").replace(" ", "")
        if not raw:
            continue
        tokens.add(raw)
        if raw.endswith("%"):
            tokens.add(raw[:-1])
    return tokens


def _resolve(ref, snippet_map) -> dict:
    if not isinstance(ref, dict):
        raise EvidenceError("invalid-evidence")
    snippet_id = str(ref.get("snippet") or "").strip().upper()
    if not snippet_id:
        raise EvidenceError("missing-snippet")
    item = snippet_map.get(snippet_id)
    if not item:
        raise EvidenceError("unknown-snippet")
    return {"page": item["page"], "quote": item["text"], "snippet": snippet_id}


def _claim(raw, snippet_map, label, allow_empty_evidence=False) -> dict:
    if not isinstance(raw, dict):
        raise DraftRejected(f"invalid-{label}")
    text = " ".join(str(raw.get("text") or "").split())
    if not text or len(text) > MAX_CLAIM_CHARS:
        raise DraftRejected(f"invalid-{label}-text")
    refs = raw.get("evidence") or []
    if not isinstance(refs, list) or (not refs and not allow_empty_evidence):
        raise DraftRejected(f"missing-evidence-{label}")
    evidence, checked = [], []
    for ref in refs[:3]:
        try:
            item = _resolve(ref, snippet_map)
        except EvidenceError as exc:
            raise DraftRejected(f"{exc.code}-{label}") from exc
        evidence.append(item)
        checked.append(item["quote"])

    supported = set()
    for quote in checked:
        supported |= numeric_tokens(quote)
    missing = numeric_tokens(text) - supported
    for snippet in snippet_map.values():
        if not missing or len(evidence) >= 3:
            break
        available = numeric_tokens(snippet["text"])
        if missing and missing <= available:
            evidence.append({"page": snippet["page"], "quote": snippet["text"], "snippet": snippet["id"]})
            supported |= available
            missing = missing - available
    if missing:
        raise DraftRejected("unsupported-number")
    return {"text": text, "evidence": evidence}


def validate_analysis(result, snippets) -> dict:
    """机械校验:能救的救,救不了的丢;problem/method 挂了就整篇拒收。"""
    if not isinstance(result, dict):
        raise DraftRejected("not-an-object")
    snippet_map = {item["id"]: item for item in snippets}
    collected = []

    problem = _claim(result.get("problem"), snippet_map, "problem")
    method = _claim(result.get("method"), snippet_map, "method")
    collected.extend(problem["evidence"] + method["evidence"])

    dropped = []

    def many(key, allow_empty_evidence=False, with_kind=False):
        items, out = result.get(key) or [], []
        if not isinstance(items, list):
            return out
        for index, raw in enumerate(items):
            try:
                claim = _claim(raw, snippet_map, key, allow_empty_evidence)
            except DraftRejected as exc:
                dropped.append({"field": f"{key}[{index}]", "reason": exc.code})
                continue
            if with_kind:
                kind = str(raw.get("kind") or "").strip()
                claim["kind"] = kind if kind in ("limitation", "future-work") else "limitation"
            out.append(claim)
            collected.extend(claim["evidence"])
        return out

    findings = many("findings")
    open_problems = many("open_problems", with_kind=True)
    insights = many("insights", allow_empty_evidence=True)

    summary = " ".join(str(result.get("summary_zh") or "").split())
    if not summary:
        raise DraftRejected("missing-summary")
    allowed_numbers = set()
    for item in collected:
        allowed_numbers |= numeric_tokens(item["quote"])
    for item in snippets:
        allowed_numbers |= numeric_tokens(item["text"])
    missing = numeric_tokens(summary) - allowed_numbers
    if missing:
        raise DraftRejected("unsupported-summary-number")

    role = {}
    raw_role = result.get("role") or {}
    if isinstance(raw_role, dict):
        for key in ROLE_LABELS:
            value = str(raw_role.get(key) or "").strip()
            if value:
                role[key] = value[:200]

    flat = []
    for claim in [problem, method] + findings + open_problems + insights:
        for item in claim["evidence"]:
            flat.append({"claim": claim["text"], **item})
    for item in flat:
        item["source"] = item.get("source") or ""

    return {
        "summary_zh": summary,
        "problem": problem,
        "method": method,
        "findings": findings,
        "open_problems": open_problems,
        "insights": insights,
        "role": role,
        "evidence": flat,
        "dropped": dropped,
    }


# -------------------------------------------------------------------- 模型调用


def _api_key(env) -> str:
    return str((env or {}).get("LLM_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()


def call_model(config, env, messages, timeout=None) -> str:
    llm = config["llm"]
    base = str((env or {}).get("LLM_BASE_URL") or os.environ.get("LLM_BASE_URL") or llm.get("base_url") or "https://api.openai.com/v1")
    model = str((env or {}).get("LLM_MODEL") or os.environ.get("LLM_MODEL") or llm.get("model") or "gpt-4o-mini")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    payload[llm.get("token_parameter", "max_completion_tokens")] = int(llm.get("max_output_tokens", 3000))
    request = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + _api_key(env),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=float(timeout or llm.get("timeout_seconds", 150))) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def _parse_json(text) -> dict:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    return json.loads(raw)


def _messages(paper, snippets):
    payload = {
        "title": paper.get("title"),
        "arxiv_id": (paper.get("identifiers") or {}).get("arxiv", [""])[0],
        "snippets": snippets,
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def generate_draft(config, env, paper, pages, now=None) -> dict:
    """生成一条 ai_draft;失败抛 DraftRejected / pdf_text.NoPdf。"""
    llm = config["llm"]
    snippets = build_snippets(pages)
    if len(snippets) < 8:
        raise DraftRejected("too-few-snippets")
    messages = _messages(paper, snippets)
    raw = call_model(config, env, messages)

    try:
        result = _parse_json(raw)
    except json.JSONDecodeError as exc:
        raise DraftRejected("invalid-json") from exc

    try:
        clean = validate_analysis(result, snippets)
    except DraftRejected as exc:
        if exc.code != "unsupported-summary-number":
            raise
        # 只改文字、不改引用的一次修复机会。
        repair = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": "summary_zh 里出现的数字没有在被引用的候选句中找到。请只重写 summary_zh,"
                "去掉没有出处的数字,evidence 保持原样,仍然输出完整 JSON。",
            },
        ]
        retry = call_model(config, env, repair)
        try:
            clean = validate_analysis(_parse_json(retry), snippets)
        except (json.JSONDecodeError, DraftRejected) as second:
            raise DraftRejected(
                getattr(second, "code", "repair-failed"), "summary numbers unsupported after repair"
            ) from second

    for item in clean["evidence"]:
        aid = (paper.get("identifiers") or {}).get("arxiv", [""])[0]
        if aid and item.get("page"):
            item["source"] = f"https://arxiv.org/pdf/{aid}#page={item['page']}"

    clean.update(
        {
            "model": str(
                (env or {}).get("LLM_MODEL") or os.environ.get("LLM_MODEL") or llm.get("model") or ""
            ),
            "prompt_version": PROMPT_VERSION,
            "scope": "pdf-evidence",
            "generated_at": iso(now or utcnow()),
            "human_approved": False,
            "stale": False,
            "source_hash": digest([paper.get("title"), paper.get("abstract"), paper.get("version")]),
            "input_pages": [page for page, _text in pages],
        }
    )
    return clean


# -------------------------------------------------------------------- 批量任务


def _needs_draft(paper, llm) -> tuple:
    if not paper.get("pdf"):
        return False, "no-pdf"
    if paper.get("imported") and not llm.get("include_imported", True):
        return False, "imported-skipped"
    draft = paper.get("ai_draft")
    if draft and not draft.get("stale") and not llm.get("regenerate_legacy_drafts"):
        if draft.get("prompt_version") != PROMPT_VERSION:
            return False, "prompt-version-changed"
        if draft.get("source_hash") == digest(
            [paper.get("title"), paper.get("abstract"), paper.get("version")]
        ):
            return False, "cached"
    return True, ""


def run_enrichment(config, root=ROOT, limit=None, env=None, now=None, store=None) -> dict:
    root = Path(root)
    llm = config.get("llm") or {}
    if not llm.get("enabled", True):
        return {"status": "disabled", "note": "config.llm.enabled=false"}
    if not _api_key(env):
        return {"status": "not-configured", "note": "缺少 LLM_API_KEY,已跳过 AI 解读"}

    now = now or utcnow()
    env = env or {}
    store = store or read_json(root / "data/store.json", {}) or {}
    records = store.get("records") or {}
    budget = store.setdefault("llm_budget", {})
    today = now.strftime("%Y-%m-%d")
    used_today = int(budget.get(today, 0))

    ceiling = int(llm.get("max_per_run", 8))
    daily_cap = int(llm.get("max_per_utc_day", 24))
    if limit:
        ceiling = min(ceiling, int(limit))
    ceiling = max(0, min(ceiling, daily_cap - used_today))

    skipped, errors, attempted, succeeded = [], [], 0, 0
    cache_dir = root / ".cache/pdfs"
    candidates = sorted(records.values(), key=lambda item: item.get("first_seen") or "")

    status = "ok"
    for paper in candidates:
        if attempted >= ceiling:
            break
        needed, reason = _needs_draft(paper, llm)
        if not needed:
            if reason not in ("cached",):
                skipped.append({"id": paper.get("id"), "reason": reason})
            continue
        attempted += 1
        try:
            payload = pdf_text.read_cache(cache_dir, digest(paper["id"]))
            if payload is None:
                payload = pdf_text.download_pdf(
                    paper.get("pdf"), timeout=float(llm.get("timeout_seconds", 150))
                )
                pdf_text.write_cache(cache_dir, digest(paper["id"]), payload)
            pages = pdf_text.load_pages(payload, max_pages=int(llm.get("max_pdf_pages", 40)))
            pages = pdf_text.select_pages(pages, max_chars=int(llm.get("max_input_chars", 48_000)))
            # 先记账再请求:超时/崩溃也不会少算一次调用。
            budget[today] = int(budget.get(today, 0)) + 1
            write_json(root / "data/store.json", store)
            draft = generate_draft(config, env, paper, pages, now=now)
            paper["ai_draft"] = draft
            paper.setdefault("ai_history", []).append(
                {"at": draft["generated_at"], "model": draft["model"], "prompt_version": PROMPT_VERSION}
            )
            succeeded += 1
            time.sleep(float(llm.get("request_interval_seconds", 3.0)))
        except (DraftRejected, pdf_text.NoPdf) as exc:
            code = getattr(exc, "code", type(exc).__name__)
            errors.append({"id": paper.get("id"), "error": f"{code}: {str(exc)[:200]}"})
        except (urllib.error.URLError, TimeoutError, OSError, KeyError) as exc:
            errors.append({"id": paper.get("id"), "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            if len(errors) >= int(llm.get("max_consecutive_errors", 5)):
                status = "partial"
                break

    if errors and succeeded == 0:
        status = "failed"
    elif errors:
        status = "partial"

    report = {
        "at": iso(now),
        "status": status,
        "attempted": attempted,
        "succeeded": succeeded,
        "skipped": skipped[:50],
        "errors": errors[:20],
        "budget_used_today": int(budget.get(today, 0)),
        "budget_cap": daily_cap,
        "prompt_version": PROMPT_VERSION,
    }
    store["llm_last_run"] = report
    write_json(root / "data/store.json", store)
    return report
