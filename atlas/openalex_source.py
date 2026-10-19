"""OpenAlex 期刊文章客户端:works 过滤 + 游标分页。

设计要点:
  * 只有 ``meta.count`` 与完整翻页一致才算成功;空结果是 count=0 的正面证据;
  * 游标分页遇到空页、重复记录、翻页无进展,统统抛 SourceError;
  * 摘要由 ``abstract_inverted_index`` 重建,并尽量回推 arXiv 编号,
    这样同一篇论文的期刊版、会议版与预印本可以按 DOI/arXiv 号合并。

礼貌池:请求带 User-Agent;配置了 ``openalex.mailto`` 或环境变量
``OPENALEX_MAILTO`` 时一并带上。可选环境变量 ``OPENALEX_API_KEY``。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone

from .common import arxiv_from_link, doi_id, parse_time, safe_url

ENDPOINT = "https://api.openalex.org/works"
MAX_BYTES = 64_000_001
RETRY_STATUS = {429, 500, 502, 503, 504}
_ISSN = re.compile(r"^\d{4}-?\d{3}[\dXx]$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SELECT_FIELDS = ",".join(
    (
        "id",
        "doi",
        "title",
        "display_name",
        "publication_year",
        "publication_date",
        "updated_date",
        "ids",
        "authorships",
        "primary_location",
        "best_oa_location",
        "open_access",
        "locations",
        "type",
        "abstract_inverted_index",
    )
)


class SourceError(RuntimeError):
    """抓取失败。消息会原样进 status.sources[].error,所以要写人话。"""


def _retry_after(exc):
    raw = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        from email.utils import parsedate_to_datetime

        return max(0.0, (parsedate_to_datetime(raw).timestamp() - time.time()))
    except (TypeError, ValueError):
        return None


class Client:
    """带节流与有限重试的 OpenAlex 客户端;opener 可注入,便于离线测试。"""

    def __init__(
        self,
        interval=1.0,
        user_agent="observatory/1.0",
        timeout=60,
        attempts=5,
        mailto="",
        api_key=None,
        opener=None,
        endpoint=ENDPOINT,
    ):
        self.interval = max(0.1, float(interval))
        self.user_agent = user_agent
        self.timeout = float(timeout)
        self.attempts = max(1, int(attempts))
        self.mailto = str(mailto or os.environ.get("OPENALEX_MAILTO") or "").strip()
        self.api_key = str(api_key or os.environ.get("OPENALEX_API_KEY") or "").strip()
        self.endpoint = str(endpoint or ENDPOINT)
        self._last = 0.0
        self._opener = opener or urllib.request.urlopen

    def _throttle(self):
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    @property
    def has_api_key(self) -> bool:
        """OpenAlex 强制要求游标翻页必须带 key;没有 key 时只能用页码翻页。"""
        return bool(self.api_key)

    def get(self, params: dict) -> dict:
        query = dict(params)
        if self.mailto:
            query.setdefault("mailto", self.mailto)
        if self.api_key:
            query.setdefault("api_key", self.api_key)
        url = self.endpoint + "?" + urllib.parse.urlencode(query, doseq=True)
        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}
        )

        last_error = None
        for attempt in range(self.attempts):
            self._throttle()
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    payload = response.read(MAX_BYTES)
                if len(payload) >= MAX_BYTES:
                    raise SourceError("OpenAlex response exceeds 64 MB safety limit")
                try:
                    body = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise SourceError("OpenAlex returned unparsable JSON") from exc
                if not isinstance(body, dict):
                    raise SourceError("OpenAlex returned a non-object JSON payload")
                if body.get("error"):
                    raise SourceError("OpenAlex error: " + str(body["error"])[:300])
                if not isinstance(body.get("meta"), dict) or not isinstance(body.get("results"), list):
                    raise SourceError("OpenAlex response lacks meta/results")
                return body
            except urllib.error.HTTPError as exc:  # noqa: PERF203
                detail = ""
                try:
                    detail = exc.read(400).decode("utf-8", "replace").strip()
                except OSError:
                    pass
                if exc.code == 401:
                    raise SourceError(
                        "OpenAlex 拒绝了请求(401)。最常见原因是游标翻页需要 API key:"
                        "请在 https://openalex.org/settings/api 申请免费 key,"
                        "然后设置环境变量 OPENALEX_API_KEY(或改用页码分页)。"
                        + (" 原始信息:" + " ".join(detail.split())[:200] if detail else "")
                    ) from exc
                message = f"HTTP {exc.code} from OpenAlex"
                if detail:
                    message += ": " + " ".join(detail.split())[:300]
                if exc.code not in RETRY_STATUS:
                    raise SourceError(message) from exc
                retry_after = _retry_after(exc)
                if retry_after is not None and retry_after > 180:
                    raise SourceError("OpenAlex asked for a long Retry-After; retry next run") from exc
                last_error = SourceError(message)
                time.sleep(retry_after if retry_after is not None else min(60, 5 * 2**attempt))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = SourceError(f"network error talking to OpenAlex: {exc}")
                time.sleep(min(30, 5 * 2**attempt))
        raise last_error or SourceError("OpenAlex request failed")


def _text(value) -> str:
    return " ".join(str(value or "").split())


def _issn_values(journal) -> list:
    raw = journal.get("issn") or []
    if isinstance(raw, str):
        raw = [raw]
    values = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        if not _ISSN.match(text):
            raise SourceError(f"invalid ISSN for OpenAlex journal {journal.get('id')!r}: {text!r}")
        normalized = text[:4] + "-" + text[4:].upper() if "-" not in text else text.upper()
        if normalized not in values:
            values.append(normalized)
    return values


def _source_id(journal) -> str:
    raw = str(journal.get("source_id") or "").strip().rstrip("/")
    if not raw:
        return ""
    return raw.rsplit("/", 1)[-1]


def filter_spec(journal, types=None) -> dict:
    """用于 query_hash:期刊过滤条件一变,游标就应回退重建基线。"""
    return {
        "id": str(journal.get("id") or ""),
        "issn": _issn_values(journal),
        "source_id": _source_id(journal),
        "types": sorted(str(item).strip() for item in (types or []) if str(item).strip()),
        "search": str(journal.get("search") or "").strip(),
    }


def _filters(journal, start, stop, types=None) -> list:
    issns = _issn_values(journal)
    source_id = _source_id(journal)
    if not issns and not source_id:
        raise SourceError(f"OpenAlex journal {journal.get('id')!r} needs an issn or source_id")
    parts = []
    if source_id:
        parts.append("primary_location.source.id:" + source_id)
    else:
        parts.append("primary_location.source.issn:" + "|".join(issns))
    try:
        from_date = parse_time(start).astimezone(timezone.utc).date().isoformat()
        to_date = parse_time(stop).astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError) as exc:
        raise SourceError("invalid OpenAlex date window") from exc
    parts.append("from_publication_date:" + from_date)
    parts.append("to_publication_date:" + to_date)
    clean_types = [str(item).strip() for item in (types or []) if str(item).strip()]
    if clean_types:
        parts.append("type:" + "|".join(clean_types))
    return parts


def reconstruct_abstract(index) -> str:
    """把 OpenAlex 的倒排摘要还原成正常文本;非法输入返回空串。"""
    if not isinstance(index, dict):
        return ""
    positions = {}
    for word, places in index.items():
        if not isinstance(places, list):
            continue
        for place in places:
            try:
                position = int(place)
            except (TypeError, ValueError):
                continue
            if 0 <= position <= 1_000_000:
                positions[position] = str(word)
    if not positions:
        return ""
    return " ".join(positions[position] for position in sorted(positions))


def _extract_arxiv(work) -> list:
    found = []
    candidates = [work.get("doi")]
    for value in (work.get("ids") or {}).values():
        candidates.append(value)
    for location in work.get("locations") or []:
        if isinstance(location, dict):
            candidates.extend((location.get("landing_page_url"), location.get("pdf_url")))
    primary = work.get("primary_location") or {}
    if isinstance(primary, dict):
        candidates.extend((primary.get("landing_page_url"), primary.get("pdf_url")))
    for raw in candidates:
        aid, _version = arxiv_from_link(raw)
        if aid and aid not in found:
            found.append(aid)
    return found


def _best_pdf(work) -> str:
    best = work.get("best_oa_location") or {}
    if isinstance(best, dict):
        url = safe_url(best.get("pdf_url"))
        if url:
            return url
    for location in work.get("locations") or []:
        if isinstance(location, dict):
            url = safe_url(location.get("pdf_url"))
            if url:
                return url
    return ""


def _published(work) -> tuple:
    date = str(work.get("publication_date") or "").strip()
    if _DATE.match(date):
        return date + "T00:00:00Z", "day"
    year = work.get("publication_year")
    if year:
        try:
            return f"{int(year):04d}-01-01T00:00:00Z", "year"
        except (TypeError, ValueError):
            pass
    return "", "unknown"


def parse_work(work, journal=None) -> dict:
    """把一条 OpenAlex work 转成站内 paper 记录。"""
    if not isinstance(work, dict):
        raise SourceError("OpenAlex returned a non-object work")
    openalex_url = str(work.get("id") or "").strip().rstrip("/")
    openalex_id = openalex_url.rsplit("/", 1)[-1] if openalex_url else ""
    if not openalex_id:
        raise SourceError("OpenAlex work lacks an id")

    title = _text(work.get("title") or work.get("display_name"))
    if not title:
        raise SourceError(f"OpenAlex work {openalex_id} lacks a title")

    doi = doi_id(work.get("doi") or (work.get("ids") or {}).get("doi"))
    authors = []
    for authorship in work.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") or {}
        name = _text(author.get("display_name") if isinstance(author, dict) else "") or _text(
            authorship.get("raw_author_name")
        )
        if name:
            authors.append(name)

    published, precision = _published(work)
    updated = str(work.get("updated_date") or "").strip() or published
    primary = work.get("primary_location") or {}
    source = primary.get("source") if isinstance(primary, dict) else {}
    venue = ""
    if isinstance(source, dict):
        venue = _text(source.get("display_name"))
    if not venue and journal:
        venue = str(journal.get("name") or "").strip()

    pdf = _best_pdf(work)
    landing = ""
    if isinstance(primary, dict):
        landing = safe_url(primary.get("landing_page_url"))
    oa_url = safe_url((work.get("open_access") or {}).get("oa_url"))
    url = (
        ("https://doi.org/" + doi)
        if doi
        else (landing or oa_url or "https://openalex.org/" + openalex_id)
    )

    arxiv_ids = _extract_arxiv(work)
    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    return {
        "id": "doi:" + doi if doi else "openalex:" + openalex_id,
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "abstract_source": "openalex" if abstract else "",
        "published": published,
        "published_precision": precision,
        "updated": updated,
        "version": 0,
        "identifiers": {"arxiv": arxiv_ids, "doi": [doi] if doi else []},
        "url": url,
        "pdf": pdf,
        "oa_pdf": pdf,
        "venue": venue,
        "comment": "",
        "categories": [str(work.get("type"))] if work.get("type") else [],
        "origin": "openalex",
    }


def journal_window(
    client,
    journal,
    start,
    stop,
    page_size=200,
    max_pages=30,
    types=None,
) -> list:
    """拉取某期刊一个出版日期窗口内的全部文章;分页异常一律失败。"""
    try:
        size = int(page_size)
        pages = int(max_pages)
    except (TypeError, ValueError) as exc:
        raise SourceError("OpenAlex page_size/max_pages must be integers") from exc
    if not 1 <= size <= 200:
        raise SourceError("OpenAlex page_size must be within 1..200")
    if pages < 1:
        raise SourceError("OpenAlex max_pages must be >= 1")

    # OpenAlex 从 2026 年起要求「游标翻页」必须带 API key(否则 401),
    # 而「页码翻页」不需要 key(上限 1 万条,对我们 30 天窗口足够)。
    # 所以有 key 用游标(可翻过 1 万条),没 key 自动降级用页码,避免整条期刊失败。
    use_cursor = getattr(client, "has_api_key", False)
    params = {
        "filter": ",".join(_filters(journal, start, stop, types)),
        "per-page": size,
        "sort": "publication_date:desc",
        "select": SELECT_FIELDS,
    }
    params["cursor" if use_cursor else "page"] = "*" if use_cursor else 1
    search = str(journal.get("search") or "").strip()
    if search:
        params["search"] = search

    found, seen = [], set()
    total = None
    for page_index in range(pages):
        payload = client.get(params)
        meta = payload.get("meta") or {}
        results = payload.get("results") or []
        try:
            count = int(meta.get("count"))
        except (TypeError, ValueError) as exc:
            raise SourceError("OpenAlex response lacks an integer meta.count") from exc
        if total is None:
            total = count
            if total > size * pages:
                raise SourceError(
                    f"OpenAlex {journal.get('id')} window has {total} works, above the configured "
                    f"{size}x{pages} safety limit; narrow max_window_days or raise max_pages_per_journal"
                )
            if not use_cursor and total > 10_000:
                raise SourceError(
                    f"OpenAlex {journal.get('id')} window has {total} works, above the 10000-record "
                    "limit of page-based paging without an API key; configure OPENALEX_API_KEY "
                    "or narrow the window"
                )
        elif count != total:
            raise SourceError(
                f"OpenAlex count changed while paging {journal.get('id')}: {count} != {total}"
            )
        if total == 0:
            return []
        if not results:
            raise SourceError(f"empty OpenAlex page but meta.count={total}")

        for work in results:
            # 用 OpenAlex work id 检测翻页重复;不同 work 共用 DOI 的情况
            # 交给合并层处理,避免把正常的预印本/正式版关系当成翻页故障。
            work_id = str(work.get("id") or "").strip() if isinstance(work, dict) else ""
            if not work_id:
                raise SourceError("OpenAlex result lacks a work id")
            if work_id in seen:
                raise SourceError(f"duplicate OpenAlex record across pages: {work_id}")
            seen.add(work_id)
            found.append(parse_work(work, journal))
            if len(found) > total:
                raise SourceError(
                    f"OpenAlex pagination overshot {journal.get('id')}: got {len(found)} of {total}"
                )

        # 收满即停:OpenAlex 在最后一页之后仍可能返回 next_cursor,
        # 若只看 next_cursor 就会再请求一页空数据,被下面的守卫误判成失败。
        if len(found) >= total:
            break
        if use_cursor:
            next_cursor = meta.get("next_cursor")
            if not next_cursor:
                break
            params["cursor"] = next_cursor
        else:
            params["page"] = page_index + 2
    else:
        raise SourceError(
            f"OpenAlex pagination incomplete after {pages} pages; narrow max_window_days"
        )

    if total is None:
        raise SourceError("OpenAlex returned no pages")
    if len(found) != total:
        raise SourceError(
            f"OpenAlex pagination incomplete for {journal.get('id')}: got {len(found)} of {total}"
        )
    return found
