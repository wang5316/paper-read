"""抓取状态机:身份索引、合并、事件、逐查询游标、整轮报告。

三条不可退让的规则(它们就是「没有结果 ≠ 抓取成功」的实现):
  1. 游标只在整窗完整成功时前移;失败不动。
  2. 一个查询窗内任何一篇合并出错,整窗都不落盘(事务性)。
  3. last_full_success 只在整轮 ok 时刷新,partial/failed 保留旧值。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

from .arxiv_source import Client, query_window, revisions
from .common import (
    ROOT,
    digest,
    identity_keys,
    iso,
    parse_time,
    read_json,
    strip_version,
    title_key,
    utcnow,
    write_json,
)
from .triage import classify, merge_classification

EMPTY = {
    "schema_version": 1,
    "records": {},
    "queries": {},
    "events": [],
    "revision_cursor": 0,
    "runs": [],
}

MAX_EVENTS = 5000
MAX_RUNS = 90
NOTE = "新增指本站首次发现;修订另计;关键词初筛不等于全文审核。"


def load_store(root=ROOT) -> dict:
    store = read_json(root / "data/store.json", None) or deepcopy(EMPTY)
    for key, value in EMPTY.items():
        store.setdefault(key, deepcopy(value))
    # 显式的 None 比缺键更好读:前端据此显示「尚无完整成功」。
    store.setdefault("last_full_success", None)
    store.setdefault("last_run", None)
    return store


def save_store(store, root=ROOT) -> None:
    write_json(root / "data/store.json", store)


def index_papers(papers) -> dict:
    """强标识 -> 命中的 paper id 集合。一个键命中多个 id 即身份冲突。"""
    index = {}
    for paper in papers:
        pid = paper.get("id")
        if not pid:
            continue
        for key in identity_keys(paper):
            index.setdefault(key, set()).add(pid)
    return index


def event(store, kind, paper, at, **extra) -> bool:
    """事件幂等:同 kind/id/version/updated/extra 只写一次。"""
    payload = {key: value for key, value in extra.items() if value is not None}
    key = digest([kind, paper.get("id"), paper.get("version"), paper.get("updated"), payload])[:24]
    if any(item.get("key") == key for item in store["events"]):
        return False
    store["events"].append(
        {
            "key": key,
            "kind": kind,
            "paper_id": paper.get("id"),
            "title": paper.get("title"),
            "at": at,
            **payload,
        }
    )
    return True


def _merge_identifiers(target, source):
    ids = target.setdefault("identifiers", {"arxiv": [], "doi": []})
    for kind in ("arxiv", "doi"):
        merged = list(ids.get(kind) or [])
        for value in (source or {}).get(kind) or []:
            if value and value not in merged:
                merged.append(value)
        ids[kind] = merged


def merge_paper(store, seeds, incoming, timestamp, config) -> str:
    """把一条 arXiv 记录并进 store,返回 new / revised / existing / metadata / unchanged / conflict。"""
    seed_map = {paper["id"]: paper for paper in seeds if paper.get("id")}
    combined = dict(seed_map)
    combined.update(store["records"])
    index = index_papers(combined.values())

    matches = set()
    for key in identity_keys(incoming):
        matches |= index.get(key, set())
    if len(matches) > 1:
        event(store, "identity-conflict", incoming, timestamp, matching_ids=sorted(matches))
        return "conflict"

    pid = next(iter(matches)) if matches else incoming["id"]
    old = store["records"].get(pid)
    seed = seed_map.get(pid)
    previous = old or seed

    paper = deepcopy(incoming)
    paper["id"] = pid
    paper["imported"] = bool(seed or (old and old.get("imported")))
    paper["origin"] = "arxiv"
    paper["first_seen"] = (old or {}).get("first_seen") or timestamp
    paper["last_seen"] = timestamp
    paper["classification"] = merge_classification(
        (previous or {}).get("classification"),
        classify(paper.get("title"), paper.get("abstract"), config["classify"]),
        paper["imported"],
    )
    paper["review_status"] = (old or {}).get("review_status") or (
        "imported-unreviewed" if paper["imported"] else "candidate"
    )
    # 已读全文永远由人工层决定,抓取链路不猜。
    paper["full_text_read"] = False
    paper["warnings"] = list((old or {}).get("warnings") or [])
    paper["versions"] = list((old or {}).get("versions") or [])

    if old:
        stale = paper.get("version", 0) < old.get("version", 0)
        if not stale and paper.get("version", 0) == old.get("version", 0):
            try:
                stale = parse_time(paper["updated"]) < parse_time(old["updated"])
            except (KeyError, ValueError):
                stale = False
        if stale:
            # 缓存/乱序响应不得把状态往回拨。
            old["last_seen"] = timestamp
            return "unchanged"
        for extra_key in ("ai_draft", "ai_history", "public_note"):
            if extra_key in old:
                paper[extra_key] = deepcopy(old[extra_key])
        draft = paper.get("ai_draft")
        if draft and digest([paper.get("title"), paper.get("abstract"), paper.get("version")]) != draft.get("source_hash"):
            draft["stale"] = True

    if previous:
        _merge_identifiers(paper, previous.get("identifiers"))

    if not previous:
        key = title_key(paper.get("title"))
        for other in combined.values():
            if other.get("id") != pid and key and title_key(other.get("title")) == key:
                paper["warnings"].append(
                    f"相同题名但未证实同一身份:{other.get('id')}(标题相同不合并,需人工确认)"
                )
        event(store, "discovered", paper, timestamp)
        kind = "new"
    elif old and paper.get("version", 0) > old.get("version", 0):
        paper["versions"].append(
            {
                "title": old.get("title"),
                "version": old.get("version"),
                "updated": old.get("updated"),
                "published": old.get("published"),
            }
        )
        event(store, "revised", paper, timestamp, previous_version=old.get("version"))
        kind = "revised"
    elif not old:
        event(store, "indexed-existing", paper, timestamp)
        kind = "existing"
    elif digest(
        [paper.get("title"), paper.get("abstract"), paper.get("venue"), paper.get("comment")]
    ) != digest([old.get("title"), old.get("abstract"), old.get("venue"), old.get("comment")]):
        event(store, "metadata-updated", paper, timestamp)
        kind = "metadata"
    else:
        kind = "unchanged"

    store["records"][pid] = paper
    return kind


def run_update(config, root=ROOT, client=None, now=None, days=None, check_revisions=True) -> dict:
    now = now or utcnow()
    timestamp = iso(now)
    root = root or ROOT
    arxiv = config["arxiv"]

    seeds = read_json(root / "data/seed.json", []) or []
    store = load_store(root)
    client = client or Client(
        interval=arxiv.get("request_interval_seconds", 5.0),
        user_agent=config.get("user_agent", "observatory/1.0"),
        timeout=arxiv.get("timeout_seconds", 60),
        attempts=arxiv.get("attempts", 5),
    )

    stats = {key: 0 for key in ("new", "revised", "existing", "metadata", "unchanged", "conflict")}
    outcomes = []

    for query in arxiv.get("queries") or []:
        if query.get("enabled") is False:
            continue
        qid, expression = query["id"], query["query"]
        previous = store["queries"].get(qid, {})
        same = previous.get("query_hash") == digest(expression)

        if days:
            start = now - timedelta(days=float(days))
        elif same and previous.get("completed_until"):
            start = parse_time(previous["completed_until"]) - timedelta(
                days=float(arxiv.get("overlap_days", 7))
            )
        else:
            start = now - timedelta(days=float(arxiv.get("initial_lookback_days", 30)))
        stop = min(now, start + timedelta(days=float(arxiv.get("max_window_days", 30))))

        result = {
            "source": qid,
            "start": iso(start),
            "end": iso(stop),
            "backlog_days": max(0, int((now - stop).total_seconds() // 86400)),
        }
        try:
            papers = query_window(
                client,
                expression,
                start,
                stop,
                page_size=int(arxiv.get("page_size", 50)),
                max_pages=int(arxiv.get("max_pages_per_query", 12)),
            )
            staged = deepcopy(store)  # 整窗成功才生效
            local = dict.fromkeys(stats, 0)
            for paper in papers:
                local[merge_paper(staged, seeds, paper, timestamp, config)] += 1
            store = staged
            for key, value in local.items():
                stats[key] += value

            cursor = stop
            if same and previous.get("completed_until"):
                cursor = max(cursor, parse_time(previous["completed_until"]))
            store["queries"][qid] = {
                "query_hash": digest(expression),
                "completed_until": iso(cursor),
                "last_success": timestamp,
            }
            result.update(status="ok", matched=len(papers))
        except Exception as exc:  # noqa: BLE001 - 任何异常都算该来源失败
            result.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:400]}")
        outcomes.append(result)

    if not outcomes:
        outcomes.append(
            {"source": "configuration", "status": "failed", "error": "No enabled queries"}
        )

    batch = int(arxiv.get("revision_batch_size", 40))
    if check_revisions and batch > 0:
        known = []
        seen = set()
        for paper in list(seeds) + list(store["records"].values()):
            for raw in (paper.get("identifiers") or {}).get("arxiv") or []:
                aid = strip_version(raw)
                if aid and aid not in seen:
                    seen.add(aid)
                    known.append(aid)
        if known:
            known.sort()
            cursor = int(store.get("revision_cursor", 0)) % len(known)
            count = min(batch, len(known))
            selected = [known[(cursor + offset) % len(known)] for offset in range(count)]
            try:
                papers = revisions(client, selected)
                staged = deepcopy(store)
                for paper in papers:
                    stats[merge_paper(staged, seeds, paper, timestamp, config)] += 1
                store = staged
                outcomes.append(
                    {"source": "known-revisions", "status": "ok", "checked": len(papers), "total_known": len(known)}
                )
            except Exception as exc:  # noqa: BLE001
                outcomes.append(
                    {
                        "source": "known-revisions",
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {str(exc)[:400]}",
                        "total_known": len(known),
                    }
                )
            # 失败也要轮转,别让一个坏编号卡死全部巡检。
            store["revision_cursor"] = (cursor + count) % len(known)

    failed = sum(1 for outcome in outcomes if outcome.get("status") == "failed")
    if failed == len(outcomes):
        state = "failed"
    elif failed:
        state = "partial"
    else:
        state = "ok"

    report = {"at": timestamp, "status": state, "counts": stats, "sources": outcomes, "note": NOTE}
    store["last_run"] = report
    if state == "ok":
        store["last_full_success"] = timestamp
    store["runs"] = (store.get("runs") or [] + [report])[-MAX_RUNS:]
    store["events"] = (store.get("events") or [])[-MAX_EVENTS:]
    save_store(store, root)
    return report


def store_stats(store, seeds) -> dict:
    """给 CLI / Step Summary 用的一行摘要。"""
    return {
        "records": len(store.get("records") or {}),
        "seeds": len(seeds or []),
        "events": len(store.get("events") or []),
        "status": (store.get("last_run") or {}).get("status", "not-run"),
        "last_full_success": store.get("last_full_success"),
    }
