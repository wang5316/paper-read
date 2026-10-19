"""抓取状态机:身份索引、合并、事件、逐查询游标、整轮报告。

三条不可退让的规则(它们就是「没有结果 ≠ 抓取成功」的实现):
  1. 游标只在整窗完整成功时前移;失败不动。
  2. 一个查询窗内任何一篇合并出错,整窗都不落盘(事务性)。
  3. last_full_success 只在整轮 ok 时刷新,partial/failed 保留旧值。
"""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import timedelta

from . import dblp_source, openalex_source
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
    "dblp": {},
    "openalex": {},
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


def _origin_of(record) -> str:
    """旧库记录可能没有 origin;用 arXiv 标识反推,避免被新来源覆盖。"""
    origin = str((record or {}).get("origin") or "").strip()
    if origin:
        return origin
    if not record:
        return ""
    if (record.get("identifiers") or {}).get("arxiv") or str(record.get("id") or "").startswith(
        "arxiv:"
    ):
        return "arxiv"
    return ""


def _sources(record) -> list:
    values = list((record or {}).get("sources") or [])
    origin = _origin_of(record)
    if origin and origin not in values:
        values.append(origin)
    return values


def _is_arxiv_record(record) -> bool:
    if not record:
        return False
    if "arxiv" in _sources(record):
        return True
    return bool(((record.get("identifiers") or {}).get("arxiv")))


def _index_add(index, paper, pid=None) -> None:
    pid = pid or paper.get("id")
    if not pid:
        return
    for key in identity_keys(paper):
        index.setdefault(key, set()).add(pid)


def _known_paper(index, paper) -> bool:
    return any(key in index for key in identity_keys(paper))


def merge_paper(store, seeds, incoming, timestamp, config, index=None, title_index=None) -> str:
    """把一条来源记录并进 store,返回 new / revised / existing / metadata / unchanged / conflict。

    ``index`` / ``title_index`` 可由调用方预先构建并复用;不传时保持旧行为(每次重建)。
    多来源批量抓取时,复用索引可以避免 O(n²) 的身份查找与同题名告警。
    """
    seed_map = {paper["id"]: paper for paper in seeds if paper.get("id")}
    if index is None:
        combined = dict(seed_map)
        combined.update(store["records"])
        known = combined.values()
        index = index_papers(known)
    else:
        # 调用方复用了索引;只有需要现场建 title_index 时才真的遍历全库,
        # 否则每合并一条都复制一次 records 会退化成 O(n²)。
        known = None

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
    incoming_origin = str(incoming.get("origin") or "arxiv").strip() or "arxiv"
    incoming_arxiv = incoming_origin == "arxiv"
    paper["origin"] = _origin_of(previous) or incoming_origin
    sources = _sources(previous)
    if incoming_origin not in sources:
        sources.append(incoming_origin)
    paper["sources"] = sources
    paper["first_seen"] = (old or {}).get("first_seen") or timestamp
    paper["last_seen"] = timestamp
    paper["review_status"] = (old or {}).get("review_status") or (
        "imported-unreviewed" if paper["imported"] else "candidate"
    )
    # 已读全文永远由人工层决定,抓取链路不猜。
    paper["full_text_read"] = False
    paper["warnings"] = list((old or {}).get("warnings") or [])
    paper["versions"] = list((old or {}).get("versions") or [])

    # 不同来源的字段丰度不同:DBLP 没有摘要,OpenAlex 有摘要和 OA PDF。
    # 合并时只补空,不让一条较薄的题录抹掉已有内容。
    if previous:
        for field in ("abstract", "abstract_source", "oa_pdf", "pdf", "comment"):
            if not paper.get(field) and previous.get(field):
                paper[field] = deepcopy(previous[field])
        if not paper.get("authors") and previous.get("authors"):
            paper["authors"] = list(previous["authors"])
        # 来源类型(arXiv 分类 / DBLP Inproceedings / OpenAlex article)各自有意义,取并集。
        old_categories = list(previous.get("categories") or [])
        new_categories = list(paper.get("categories") or [])
        merged_categories = old_categories + [
            item for item in new_categories if item not in old_categories
        ]
        if merged_categories:
            paper["categories"] = merged_categories
        # 期刊/会议题录的年月日通常不如 arXiv 版本时间精确,不要把 updated 往回拨。
        if not incoming_arxiv:
            try:
                if previous.get("updated") and parse_time(previous["updated"]) > parse_time(
                    paper.get("updated")
                ):
                    paper["updated"] = previous["updated"]
            except (KeyError, TypeError, ValueError):
                pass
        incoming_venue = str(paper.get("venue") or "").strip()
        old_venue = str(previous.get("venue") or "").strip()
        if (not incoming_venue or incoming_venue.lower() == "arxiv") and old_venue and old_venue.lower() != "arxiv":
            paper["venue"] = previous["venue"]
        if paper.get("published_precision") == "year" and previous.get("published"):
            old_published = str(previous["published"])
            new_published = str(paper.get("published") or "")
            if old_published[:4] == new_published[:4] and len(old_published) > 10:
                paper["published"] = previous["published"]
                paper["published_precision"] = previous.get("published_precision") or "day"
        if not paper.get("url") and previous.get("url"):
            paper["url"] = previous["url"]
        if paper.get("abstract") and not paper.get("abstract_source"):
            paper["abstract_source"] = (
                (previous or {}).get("abstract_source")
                or _origin_of(previous)
                or incoming_origin
            )

    old_version = int((old or {}).get("version") or 0)
    incoming_version = int(paper.get("version") or 0)
    previous_arxiv = _is_arxiv_record(previous)

    if old and incoming_arxiv and previous_arxiv:
        stale = incoming_version < old_version
        if not stale and incoming_version == old_version:
            try:
                stale = parse_time(paper["updated"]) < parse_time(old["updated"])
            except (KeyError, ValueError):
                stale = False
        if stale:
            # 缓存/乱序响应不得把状态往回拨。
            old["last_seen"] = timestamp
            return "unchanged"
    elif old and not incoming_arxiv:
        # 期刊/会议记录没有 arXiv 版本号;沿用已有记录的版本,避免被判成旧数据。
        paper["version"] = old_version

    if old:
        for extra_key in ("ai_draft", "ai_history", "public_note"):
            if extra_key in old:
                paper[extra_key] = deepcopy(old[extra_key])
        draft = paper.get("ai_draft")
        if draft and digest([paper.get("title"), paper.get("abstract"), paper.get("version")]) != draft.get("source_hash"):
            draft["stale"] = True

    if previous:
        _merge_identifiers(paper, previous.get("identifiers"))
    if paper.get("abstract") and not paper.get("abstract_source"):
        paper["abstract_source"] = incoming_origin

    paper["classification"] = merge_classification(
        (previous or {}).get("classification"),
        classify(paper.get("title"), paper.get("abstract"), config["classify"]),
        paper["imported"],
    )

    if not previous:
        key = title_key(paper.get("title"))
        if title_index is None:
            if known is None:
                known = list(seed_map.values()) + list(store["records"].values())
            title_index = {}
            for other in known:
                other_key = title_key(other.get("title"))
                if other_key:
                    title_index.setdefault(other_key, set()).add(other.get("id"))
        for other_id in title_index.get(key, set()) if key else set():
            if other_id and other_id != pid:
                paper["warnings"].append(
                    f"相同题名但未证实同一身份:{other_id}(标题相同不合并,需人工确认)"
                )
        event(store, "discovered", paper, timestamp)
        kind = "new"
    elif (
        old
        and incoming_arxiv
        and previous_arxiv
        and incoming_version > old_version
    ):
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
        [
            paper.get("title"),
            paper.get("abstract"),
            paper.get("venue"),
            paper.get("comment"),
            paper.get("authors"),
            paper.get("published"),
        ]
    ) != digest(
        [
            old.get("title"),
            old.get("abstract"),
            old.get("venue"),
            old.get("comment"),
            old.get("authors"),
            old.get("published"),
        ]
    ):
        event(store, "metadata-updated", paper, timestamp)
        kind = "metadata"
    else:
        kind = "unchanged"

    store["records"][pid] = paper
    _index_add(index, paper, pid)
    title_key_value = title_key(paper.get("title"))
    if title_key_value:
        if title_index is None:
            title_index = {}
        title_index.setdefault(title_key_value, set()).add(pid)
    return kind


def _build_title_index(records) -> dict:
    index = {}
    for record in records:
        key = title_key(record.get("title"))
        pid = record.get("id")
        if key and pid:
            index.setdefault(key, set()).add(pid)
    return index


def _stage_papers(
    config, store, seeds, papers, timestamp, stats, prefilter=False, require_track=False
):
    """事务性合并一批记录:整批成功才返回新 store。

    返回 ``(staged_store, local_counts, stored, skipped)``。
    ``prefilter`` 只对 DBLP/OpenAlex 这类「先拿全量、再由站内分类器筛」的来源使用;
    已知身份的记录不会被跳过,否则会漏掉期刊/会议版对既有 arXiv 题录的补全。

    ``require_track=True`` 时进一步要求命中至少一个研究方向(``tracks`` 非空)。
    会议 proceedings 里绝大多数论文和本站主题无关,只靠 bucket 挡不住
    「3D Gaussian 重建/渲染」这类沾边但不对题的记录,所以默认按方向标签再筛一层。
    """
    local = dict.fromkeys(stats, 0)
    if not papers:
        return store, local, 0, 0

    base = list(seeds or []) + list(store["records"].values())
    index = index_papers(base)
    title_index = _build_title_index(base)
    staged = store
    activated = False
    stored = skipped = 0

    for paper in papers:
        if prefilter and not _known_paper(index, paper):
            classification = classify(
                paper.get("title"), paper.get("abstract"), config["classify"]
            )
            if classification.get("bucket") == "excluded":
                skipped += 1
                continue
            if require_track and not classification.get("tracks"):
                skipped += 1
                continue
        if not activated:
            staged = deepcopy(store)
            activated = True
        kind = merge_paper(
            staged,
            seeds,
            paper,
            timestamp,
            config,
            index=index,
            title_index=title_index,
        )
        local[kind] += 1
        if kind != "conflict":
            stored += 1
    return staged, local, stored, skipped


def _dblp_years(config, now, days=None, venue=None) -> list:
    """某会议要抓哪些年份。

    ``venue.years`` 显式指定时优先(用于 ICCV 奇数年 / ECCV 偶数年这类双年会议);
    否则回退到 ``lookback_years`` 的滚动窗口。
    """
    dblp = config.get("dblp") or {}
    explicit = (venue or {}).get("years")
    if explicit:
        years = sorted({int(year) for year in explicit})
    else:
        lookback = int(dblp.get("lookback_years", 1))
        if days:
            lookback = max(lookback, int(math.ceil(float(days) / 365.0)))
        lookback = max(0, lookback)
        years = list(range(now.year - lookback, now.year + 1))
    if not days:
        # 未显式回补时,未来年份没有意义(会议还没开,DBLP 必然 0 条),直接排除。
        years = [year for year in years if year <= now.year]
    return years


def run_update(
    config,
    root=ROOT,
    client=None,
    now=None,
    days=None,
    check_revisions=True,
    dblp_client=None,
    openalex_client=None,
) -> dict:
    now = now or utcnow()
    timestamp = iso(now)
    root = root or ROOT
    arxiv = config.get("arxiv") or {}
    dblp_cfg = config.get("dblp") or {}
    openalex_cfg = config.get("openalex") or {}

    seeds = read_json(root / "data/seed.json", []) or []
    store = load_store(root)

    stats = {key: 0 for key in ("new", "revised", "existing", "metadata", "unchanged", "conflict")}
    outcomes = []

    # ---------------------------------------------------------------- arXiv
    arxiv_queries = [
        query for query in arxiv.get("queries") or [] if query.get("enabled") is not False
    ]
    arxiv_client = client
    if arxiv_queries and arxiv_client is None:
        arxiv_client = Client(
            interval=arxiv.get("request_interval_seconds", 5.0),
            user_agent=config.get("user_agent", "observatory/1.0"),
            timeout=arxiv.get("timeout_seconds", 60),
            attempts=arxiv.get("attempts", 5),
        )

    for query in arxiv_queries:
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
                arxiv_client,
                expression,
                start,
                stop,
                page_size=int(arxiv.get("page_size", 50)),
                max_pages=int(arxiv.get("max_pages_per_query", 12)),
            )
            staged, local, kept, skipped = _stage_papers(
                config, store, seeds, papers, timestamp, stats
            )
            store = staged
            for key, value in local.items():
                stats[key] += value

            cursor = stop
            if same and previous.get("completed_until"):
                cursor = max(cursor, parse_time(previous["completed_until"]))
            store.setdefault("queries", {})[qid] = {
                "query_hash": digest(expression),
                "completed_until": iso(cursor),
                "last_success": timestamp,
            }
            result.update(status="ok", matched=len(papers), stored=kept, skipped=skipped)
        except Exception as exc:  # noqa: BLE001 - 任何异常都算该来源失败
            result.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:400]}")
        outcomes.append(result)

    # ----------------------------------------------------------------- DBLP
    dblp_venues = [
        venue for venue in dblp_cfg.get("venues") or [] if venue.get("enabled") is not False
    ]
    if dblp_cfg.get("enabled") is not False and dblp_venues:
        if dblp_client is None:
            dblp_client = dblp_source.Client(
                interval=dblp_cfg.get("request_interval_seconds", 2.0),
                user_agent=config.get("user_agent", "observatory/1.0"),
                timeout=dblp_cfg.get("timeout_seconds", 120),
                attempts=dblp_cfg.get("attempts", 4),
                endpoint=dblp_cfg.get("endpoint", dblp_source.ENDPOINT),
            )
        recheck_days = float(dblp_cfg.get("recheck_completed_years_days", 30))
        for venue in dblp_venues:
            vid = venue["id"]
            years_state = store.setdefault("dblp", {}).setdefault(vid, {})
            for year in _dblp_years(config, now, days=days, venue=venue):
                previous_year = (years_state.get("years") or {}).get(str(year)) or {}
                recently_done = False
                if not days and year < now.year and previous_year.get("last_success"):
                    try:
                        recently_done = parse_time(previous_year["last_success"]) >= (
                            now - timedelta(days=recheck_days)
                        )
                    except ValueError:
                        recently_done = False
                if recently_done:
                    outcomes.append(
                        {
                            "source": f"dblp:{vid}",
                            "year": year,
                            "status": "skipped",
                            "reason": "最近已完整抓取;未到复查间隔",
                        }
                    )
                    continue

                result = {
                    "source": f"dblp:{vid}",
                    "year": year,
                    "venue": venue.get("name") or vid,
                    "start": f"{year:04d}-01-01",
                    "end": f"{year:04d}-12-31",
                }
                try:
                    papers = dblp_source.venue_year(
                        dblp_client,
                        venue["stream"],
                        year,
                        page_size=int(dblp_cfg.get("page_size", 500)),
                        max_pages=int(dblp_cfg.get("max_pages_per_venue_year", 20)),
                        venue_name=venue.get("name") or vid,
                    )
                    staged, local, kept, skipped = _stage_papers(
                        config,
                        store,
                        seeds,
                        papers,
                        timestamp,
                        stats,
                        prefilter=bool(dblp_cfg.get("scope_prefilter", True)),
                        require_track=bool(dblp_cfg.get("require_track", False)),
                    )
                    store = staged
                    for key, value in local.items():
                        stats[key] += value

                    state = store.setdefault("dblp", {}).setdefault(vid, {})
                    state["stream"] = venue["stream"]
                    if venue.get("name"):
                        state["name"] = venue["name"]
                    state.setdefault("years", {})[str(year)] = {
                        "last_success": timestamp,
                        "matched": len(papers),
                        "stored": kept,
                    }
                    result.update(status="ok", matched=len(papers), stored=kept, skipped=skipped)
                except Exception as exc:  # noqa: BLE001
                    result.update(
                        status="failed", error=f"{type(exc).__name__}: {str(exc)[:400]}"
                    )
                outcomes.append(result)

    # -------------------------------------------------------------- OpenAlex
    openalex_journals = [
        journal
        for journal in openalex_cfg.get("journals") or []
        if journal.get("enabled") is not False
    ]
    if openalex_cfg.get("enabled") is not False and openalex_journals:
        if openalex_client is None:
            openalex_client = openalex_source.Client(
                interval=openalex_cfg.get("request_interval_seconds", 1.0),
                user_agent=config.get("user_agent", "observatory/1.0"),
                timeout=openalex_cfg.get("timeout_seconds", 60),
                attempts=openalex_cfg.get("attempts", 5),
                mailto=openalex_cfg.get("mailto", ""),
            )
        openalex_types = openalex_cfg.get("types") or []
        for journal in openalex_journals:
            jid = journal["id"]
            spec = openalex_source.filter_spec(journal, openalex_types)
            query_hash = digest(spec)
            previous = store.setdefault("openalex", {}).get(jid, {})
            same = previous.get("filter_hash") == query_hash

            if days:
                start = now - timedelta(days=float(days))
            elif same and previous.get("completed_until"):
                start = parse_time(previous["completed_until"]) - timedelta(
                    days=float(openalex_cfg.get("overlap_days", 7))
                )
            else:
                start = now - timedelta(
                    days=float(openalex_cfg.get("initial_lookback_days", 30))
                )
            stop = min(
                now,
                start + timedelta(days=float(openalex_cfg.get("max_window_days", 30))),
            )

            result = {
                "source": f"openalex:{jid}",
                "journal": journal.get("name") or jid,
                "start": iso(start),
                "end": iso(stop),
                "backlog_days": max(0, int((now - stop).total_seconds() // 86400)),
            }
            try:
                papers = openalex_source.journal_window(
                    openalex_client,
                    journal,
                    start,
                    stop,
                    page_size=int(openalex_cfg.get("page_size", 200)),
                    max_pages=int(openalex_cfg.get("max_pages_per_journal", 30)),
                    types=openalex_types,
                )
                staged, local, kept, skipped = _stage_papers(
                    config,
                    store,
                    seeds,
                    papers,
                    timestamp,
                    stats,
                    prefilter=bool(openalex_cfg.get("scope_prefilter", True)),
                    require_track=bool(openalex_cfg.get("require_track", False)),
                )
                store = staged
                for key, value in local.items():
                    stats[key] += value

                cursor = stop
                if same and previous.get("completed_until"):
                    cursor = max(cursor, parse_time(previous["completed_until"]))
                store.setdefault("openalex", {})[jid] = {
                    "filter_hash": query_hash,
                    "completed_until": iso(cursor),
                    "last_success": timestamp,
                }
                result.update(status="ok", matched=len(papers), stored=kept, skipped=skipped)
            except Exception as exc:  # noqa: BLE001
                result.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:400]}")
            outcomes.append(result)

    if not outcomes:
        outcomes.append(
            {"source": "configuration", "status": "failed", "error": "No enabled sources"}
        )

    # ------------------------------------------------------ 已知 arXiv 版本巡检
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
            if arxiv_client is None:
                arxiv_client = Client(
                    interval=arxiv.get("request_interval_seconds", 5.0),
                    user_agent=config.get("user_agent", "observatory/1.0"),
                    timeout=arxiv.get("timeout_seconds", 60),
                    attempts=arxiv.get("attempts", 5),
                )
            known.sort()
            cursor = int(store.get("revision_cursor", 0)) % len(known)
            count = min(batch, len(known))
            selected = [known[(cursor + offset) % len(known)] for offset in range(count)]
            try:
                papers = revisions(arxiv_client, selected)
                staged, local, kept, skipped = _stage_papers(
                    config, store, seeds, papers, timestamp, stats
                )
                store = staged
                for key, value in local.items():
                    stats[key] += value
                outcomes.append(
                    {
                        "source": "known-revisions",
                        "status": "ok",
                        "matched": len(papers),
                        "checked": len(papers),
                        "total_known": len(known),
                        "stored": kept,
                        "skipped": skipped,
                    }
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
    succeeded = sum(1 for outcome in outcomes if outcome.get("status") == "ok")
    if failed and not succeeded:
        state = "failed"
    elif failed:
        state = "partial"
    elif succeeded:
        state = "ok"
    else:
        # 全是 skipped(例如所有来源都因「最近刚完成」被跳过)不是成功:
        # 本轮没有实际抓取,不能刷新 last_full_success。
        state = "failed"
        outcomes.append(
            {"source": "round", "status": "failed", "error": "本轮所有来源都被跳过,没有实际抓取"}
        )

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
