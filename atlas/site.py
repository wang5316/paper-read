"""静态站点生成:白名单投影 -> dist/(index.html, style.css, app.js, data.js, ...)。

要点:
  * HTML 不套模板、不在服务端渲染 —— 三个静态文件原样复制,渲染全在前端;
  * 出站字段走白名单,私有字段(warnings/versions/last_seen/内部标记)永不透出;
  * 专题在服务端算好写进每条记录,前端不再维护一套并行的匹配逻辑
    (上游两边各写一套,词表不一致导致专题永远为空)。
"""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

from .common import DATA, ROOT, digest, iso, parse_time, read_json, safe_url, utcnow
from .editorial import REVIEWED_STATES, load as load_editorial

PUBLIC_FIELDS = (
    "id",
    "title",
    "authors",
    "published",
    "published_precision",
    "updated",
    "version",
    "venue",
    "origin",
    "sources",
    "categories",
    "identifiers",
    "first_seen",
)
STEADY_FILES = ("index.html", "style.css", "app.js")
OUTPUT_FILES = STEADY_FILES + (
    "data.js",
    "data.json",
    "feed.xml",
    "catalog.ris",
    "recent.ris",
    "digest.md",
    ".nojekyll",
)
FORBIDDEN_OUTPUTS = {"", ".", "data", "web", "atlas", "scripts", "tests", "docs", ".github"}


def _topic_haystack(paper) -> str:
    classification = paper.get("classification") or {}
    parts = [
        paper.get("title") or "",
        paper.get("abstract") or "",
        paper.get("venue") or "",
        " ".join(classification.get("methods") or []),
    ]
    return " ".join(parts).lower()


def topic_match(paper, topic) -> bool:
    match = topic.get("match") or {}
    classification = paper.get("classification") or {}
    if set(match.get("tracks") or []) & set(classification.get("tracks") or []):
        return True
    if match.get("tasks") and classification.get("task") in match["tasks"]:
        return True
    if set(match.get("methods") or []) & set(classification.get("methods") or []):
        return True
    haystack = _topic_haystack(paper)
    if any(str(keyword).lower() in haystack for keyword in match.get("keywords") or [] if keyword):
        return True
    venue = str(paper.get("venue") or "").strip()
    if match.get("venue_named") and venue and venue.lower() != "arxiv":
        return True
    if match.get("abstract_only") and paper.get("abstract") and not paper.get("ai_draft"):
        return True
    return False


def assigned_topics(paper, topics) -> list:
    ids = []
    for group in topics or []:
        for topic in group.get("items") or []:
            if topic_match(paper, topic) and topic["id"] not in ids:
                ids.append(topic["id"])
    return ids


def public_records(config, seeds, store, editorial=None, include_imports=False) -> list:
    editorial = editorial or {}
    publication = config.get("publication") or {}
    include_related = bool(publication.get("include_related", True))
    publish_abstracts = bool(publication.get("publish_abstracts", True))
    publish_ai = bool(publication.get("publish_ai_drafts", True))

    combined = {}
    for seed in seeds or []:
        if seed.get("id"):
            combined[seed["id"]] = dict(seed)
    for pid, record in (store.get("records") or {}).items():
        merged = dict(combined.get(pid) or {})
        merged.update(record)
        combined[pid] = merged

    out = []
    for pid, record in combined.items():
        override = editorial.get(pid) or {}
        if override.get("state") == "rejected" or override.get("public") is False:
            continue

        review_status = override.get("state") or record.get("review_status") or "candidate"
        if review_status in REVIEWED_STATES:
            reviewed_version = override.get("reviewed_version")
            if reviewed_version is not None and reviewed_version != record.get("version", 0):
                review_status = "review-stale"

        classification = dict(record.get("classification") or {})
        if override.get("tracks") is not None:
            classification["tracks"] = list(override["tracks"])
        if override.get("bucket"):
            classification["bucket"] = override["bucket"]
        if override.get("task"):
            classification["task"] = override["task"]

        bucket = classification.get("bucket")
        imported = bool(record.get("imported"))
        if bucket == "excluded" and not override.get("public"):
            continue
        if bucket == "related" and not include_related:
            continue
        if imported and not include_imports and not publication.get("publish_imported_references", True):
            continue
        if review_status in ("candidate",) and not publication.get("publish_auto_candidates", True):
            continue

        paper = {field: record.get(field) for field in PUBLIC_FIELDS if field in record}
        paper["id"] = pid
        paper["classification"] = classification
        paper["review_status"] = review_status
        paper["full_text_read"] = review_status == "full-text-read"
        paper["imported"] = imported
        paper["url"] = safe_url(record.get("url") or record.get("pdf"))
        if publish_abstracts and record.get("abstract"):
            paper["abstract"] = record["abstract"]
            paper["abstract_source"] = record.get("abstract_source") or "arxiv"
        if record.get("oa_pdf"):
            paper["oa_pdf"] = safe_url(record["oa_pdf"])
        draft = record.get("ai_draft")
        if publish_ai and draft and draft.get("summary_zh") and not draft.get("stale"):
            paper["ai_draft"] = draft
        if override.get("public_note"):
            paper["public_note"] = str(override["public_note"])[:2000]
        paper["topics"] = assigned_topics(paper, config.get("topics") or [])
        out.append(paper)

    out.sort(
        key=lambda item: (
            str(item.get("first_seen") or ""),
            str(item.get("published") or ""),
            str(item.get("id") or ""),
        ),
        reverse=True,
    )
    return out


def to_ris(papers) -> str:
    def clean(value):
        return " ".join(str(value or "").split())

    blocks = []
    for paper in papers:
        conference = "Inproceedings" in (paper.get("categories") or [])
        lines = ["TY  - " + ("CP" if conference else "JOUR"), "TI  - " + clean(paper.get("title"))]
        for author in paper.get("authors") or []:
            lines.append("AU  - " + clean(author))
        published = clean(paper.get("published"))
        if published:
            lines.append("PY  - " + published[:4])
        for doi in (paper.get("identifiers") or {}).get("doi") or []:
            lines.append("DO  - " + clean(doi))
            break
        for aid in (paper.get("identifiers") or {}).get("arxiv") or []:
            lines.append("AN  - arXiv:" + clean(aid))
            break
        url = safe_url(paper.get("url"))
        if url:
            lines.append("UR  - " + url)
        venue = clean(paper.get("venue"))
        if venue:
            lines.append("JO  - " + venue)
        lines.extend(["ER  - ", ""])
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def _event_items(store, public_ids, limit=100) -> list:
    events = []
    for item in reversed(store.get("events") or []):
        if item.get("paper_id") not in public_ids:
            continue
        events.append(
            {
                "kind": item.get("kind"),
                "paper_id": item.get("paper_id"),
                "title": item.get("title"),
                "at": item.get("at"),
            }
        )
        if len(events) >= limit:
            break
    return list(reversed(events))


def _feed(config, papers, events) -> str:
    site_url = str(config.get("site_url") or "").rstrip("/")
    titles = {
        "discovered": "[新收录] ",
        "revised": "[新版本] ",
        "metadata-updated": "[题录更新] ",
    }
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss version="2.0"><channel>',
        f"<title>{html.escape(str(config.get('name_zh') or config.get('name') or 'observatory'))}</title>",
        f"<link>{html.escape(site_url)}</link>",
        f"<description>{html.escape('自动发现的论文候选;不代表精读或质量背书。')}</description>",
        f"<lastBuildDate>{html.escape(iso(utcnow()))}</lastBuildDate>",
    ]
    by_id = {paper["id"]: paper for paper in papers}
    for event in reversed(events):
        paper = by_id.get(event["paper_id"])
        if not paper:
            continue
        prefix = titles.get(event["kind"], "")
        link = safe_url(paper.get("url")) or site_url
        parts.append(
            "<item>"
            f"<title>{html.escape(prefix + str(paper.get('title') or ''))}</title>"
            f"<link>{html.escape(link)}</link>"
            f'<guid isPermaLink="false">{html.escape(digest([event["kind"], event["paper_id"], event["at"]])[:24])}</guid>'
            f"<pubDate>{html.escape(str(event.get('at') or ''))}</pubDate>"
            f"<description>{html.escape('题录/摘要初筛;未自动核实实验协议。')}</description>"
            "</item>"
        )
    parts.append("</channel></rss>")
    return "".join(parts)


def _source_detail(source) -> str:
    status = source.get("status")
    if status == "failed":
        return str(source.get("error") or "failed")
    if status == "skipped":
        return str(source.get("reason") or "skipped")
    bits = []
    for key in ("year", "journal"):
        if source.get(key) and str(source[key]) not in bits:
            bits.append(str(source[key]))
    bits.append(f"matched {source.get('matched', 0)}")
    if "stored" in source:
        bits.append(f"stored {source.get('stored', 0)}")
    if source.get("skipped"):
        bits.append(f"prefiltered {source.get('skipped')}")
    if source.get("checked") is not None:
        bits.append(f"checked {source.get('checked')}")
    if source.get("backlog_days"):
        bits.append(f"{source['backlog_days']}d backlog")
    return " · ".join(bits)


def _digest_md(config, papers, events, report) -> str:
    lines = [f"# {config.get('name_zh') or config.get('name')} · 运行摘要", ""]
    status = (report or {}).get("status", "not-run")
    lines.append(f"- 本轮状态:`{status}`(at {(report or {}).get('at') or '-'})")
    counts = (report or {}).get("counts") or {}
    if counts:
        lines.append(
            "- 计数:" + " / ".join(f"{key} {value}" for key, value in counts.items() if value)
        )
    lines.append("- 可浏览文献:**%d** 篇(含既有题录)" % len(papers))
    lines.append("")
    lines.append("## 近七天首次收录(不是论文首发日)")
    lines.append("")
    recent = [event for event in events if event.get("kind") == "discovered"][-7:]
    if not recent:
        lines.append(
            "没有可公开的新收录。**请同时检查抓取状态,不能据此推断没有新论文。**"
        )
    else:
        for event in reversed(recent):
            lines.append(f"- {event.get('at', '')[:10]} · {event.get('title')}")
    lines.append("")
    lines.append("## 来源状态")
    for source in (report or {}).get("sources") or []:
        lines.append(
            f"- `{source.get('source')}` : {source.get('status')} · {_source_detail(source)}"
        )
    return "\n".join(lines) + "\n"


def build(config, root=ROOT, out=None, include_imports=True, preview=False, now=None) -> dict:
    root = Path(root)
    out = Path(out) if out else root / "dist"
    resolved = out.resolve()
    if resolved == root.resolve().parent or str(resolved.name) in FORBIDDEN_OUTPUTS:
        raise ValueError(f"refusing to build into {resolved}")

    seeds = read_json(root / "data/seed.json", []) or []
    store = read_json(root / "data/store.json", {}) or {}
    editorial = load_editorial(root)

    papers = public_records(config, seeds, store, editorial, include_imports=include_imports)
    public_ids = {paper["id"] for paper in papers}
    events = _event_items(store, public_ids, limit=100)
    report = store.get("last_run") or {}
    classify_cfg = config.get("classify") or {}
    task_names = {}
    for slot in ("primary", "adjacent", "any"):
        item = (classify_cfg.get("task") or {}).get(slot) or {}
        if item.get("id"):
            task_names[item["id"]] = item.get("name") or item["id"]

    payload = {
        "site": {
            "name": config.get("name"),
            "name_zh": config.get("name_zh"),
            "url": config.get("site_url"),
            "timezone": config.get("display_timezone", "Asia/Shanghai"),
            "repo": (config.get("review") or {}).get("repo"),
            "preview": bool(preview),
        },
        "classify": {
            "tracks": [
                {"id": track["id"], "name": track.get("name") or track["id"]}
                for track in classify_cfg.get("tracks") or []
            ],
            "tasks": task_names,
        },
        "built_at": iso(now or utcnow()),
        "last_full_success": store.get("last_full_success"),
        "index_count": len(seeds),
        "status": {
            "status": report.get("status", "not-run"),
            "at": report.get("at"),
            "counts": report.get("counts") or {},
            "sources": report.get("sources") or [],
            "note": report.get("note"),
        },
        "topics": config.get("topics") or [],
        "papers": papers,
        "events": events,
    }

    if resolved.exists():
        unknown = [
            child.name
            for child in resolved.iterdir()
            if child.is_file() and child.name not in OUTPUT_FILES
        ]
        if unknown:
            raise ValueError(f"unknown files in output dir, refusing to publish: {sorted(unknown)}")
    resolved.mkdir(parents=True, exist_ok=True)

    web = root / "web"
    for name in STEADY_FILES:
        shutil.copyfile(web / name, resolved / name)

    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    blob = blob.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    (resolved / "data.js").write_text("window.ATLAS=" + blob + ";\n", "utf-8", newline="\n")
    (resolved / "data.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8", newline="\n"
    )
    (resolved / "feed.xml").write_text(_feed(config, papers, events), "utf-8", newline="\n")
    (resolved / "catalog.ris").write_text(to_ris(papers), "utf-8", newline="\n")

    cutoff = (now or utcnow()).timestamp() - 7 * 86400
    fresh = []
    for event in events:
        if event.get("kind") != "discovered":
            continue
        try:
            if parse_time(event["at"]).timestamp() >= cutoff:
                fresh.append(event["paper_id"])
        except (KeyError, ValueError):
            continue
    by_id = {paper["id"]: paper for paper in papers}
    (resolved / "recent.ris").write_text(
        to_ris([by_id[pid] for pid in dict.fromkeys(fresh) if pid in by_id]), "utf-8", newline="\n"
    )
    (resolved / "digest.md").write_text(_digest_md(config, papers, events, report), "utf-8", newline="\n")
    (resolved / ".nojekyll").write_text("", "utf-8", newline="\n")

    return {
        "out": str(resolved),
        "papers": len(papers),
        "events": len(events),
        "files": sorted(OUTPUT_FILES),
        "status": payload["status"]["status"],
    }
