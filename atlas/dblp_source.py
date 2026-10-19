"""DBLP 会议论文客户端:官方 SPARQL 端点 + 计数/翻页守卫。

为什么走 SPARQL,而不是 ``search/publ/api``:
  dblp.org 自 2026 年起在搜索接口前加了 Anubis 挑战(返回 HTML 而不是 JSON),
  纯标准库客户端无法通过。官方 SPARQL 端点 ``sparql.dblp.org`` 不受这层拦截,
  数据每天同步,字段也足够(标题/作者/DOI/年份/会议)。

完整性规则与 arXiv 源一致:
  * 先用 COUNT(DISTINCT ?paper) 拿到该会议该年份的总数;
  * 每一页都必须有数据、不能出现重复记录;
  * 最终累计条数必须等于总数,否则抛 SourceError,绝不把半页当成成功。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from .common import ARXIV_PDF, arxiv_from_link, doi_id, safe_url

ENDPOINT = "https://sparql.dblp.org/sparql"
MAX_BYTES = 32_000_001
RETRY_STATUS = {429, 500, 502, 503, 504}
STREAM_RE = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*$")
_DBLP_AUTHOR_SUFFIX = re.compile(r"\s+\d{4}$")

PREFIX = "PREFIX dblp: <https://dblp.org/rdf/schema#>"

COUNT_QUERY = PREFIX + """
SELECT (COUNT(DISTINCT ?paper) AS ?count) WHERE {{
  ?paper dblp:publishedInStream <{stream}> .
  ?paper a ?type .
  VALUES ?type {{ dblp:Inproceedings dblp:Article }}
  ?paper dblp:yearOfPublication ?year .
  FILTER(STR(?year) = "{year}")
  ?paper dblp:title ?titleText .
}}"""

PAGE_QUERY = PREFIX + """
SELECT ?paper
       (MIN(?titleText) AS ?title)
       (MIN(STR(?doiValue)) AS ?doi)
       (GROUP_CONCAT(?eeValue; separator="|") AS ?ee)
       (MIN(?venueValue) AS ?venue)
       (MIN(?pagesValue) AS ?pages)
       (GROUP_CONCAT(CONCAT(STR(?ord), "\\t", ?signatureName); separator="|") AS ?authors)
WHERE {{
  ?paper dblp:publishedInStream <{stream}> .
  ?paper a ?type .
  VALUES ?type {{ dblp:Inproceedings dblp:Article }}
  ?paper dblp:yearOfPublication ?year .
  FILTER(STR(?year) = "{year}")
  ?paper dblp:title ?titleText .
  OPTIONAL {{ ?paper dblp:doi ?doiValue . }}
  OPTIONAL {{ ?paper dblp:primaryDocumentPage ?eeValue . }}
  OPTIONAL {{ ?paper dblp:documentPage ?eeValue . }}
  OPTIONAL {{ ?paper dblp:publishedIn ?venueValue . }}
  OPTIONAL {{ ?paper dblp:pagination ?pagesValue . }}
  OPTIONAL {{
    ?paper dblp:hasSignature ?sig .
    ?sig dblp:signatureDblpName ?signatureName .
    ?sig dblp:signatureOrdinal ?ord .
  }}
}}
GROUP BY ?paper
ORDER BY ?paper
LIMIT {limit} OFFSET {offset}"""


class SourceError(RuntimeError):
    """抓取失败。消息会原样进 status.sources[].error,所以要写人话。"""


def _text(node) -> str:
    return " ".join(str((node or {}).get("value") or "").split())


def _raw(node) -> str:
    """作者字段用制表符编码序号,不能像普通文本那样折叠空白。"""
    return str((node or {}).get("value") or "")


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
    """带节流与有限重试的 SPARQL 客户端;opener 可注入,便于离线测试。"""

    def __init__(
        self,
        interval=2.0,
        user_agent="observatory/1.0",
        timeout=120,
        attempts=4,
        opener=None,
        endpoint=ENDPOINT,
    ):
        self.interval = max(0.5, float(interval))
        self.user_agent = user_agent
        self.timeout = float(timeout)
        self.attempts = max(1, int(attempts))
        self.endpoint = str(endpoint or ENDPOINT)
        self._last = 0.0
        self._opener = opener or urllib.request.urlopen

    def _throttle(self):
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def select(self, query: str) -> dict:
        request = urllib.request.Request(
            self.endpoint,
            data=query.encode("utf-8"),
            method="POST",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/sparql-results+json",
                "Content-Type": "application/sparql-query; charset=utf-8",
            },
        )
        last_error = None
        for attempt in range(self.attempts):
            self._throttle()
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    payload = response.read(MAX_BYTES)
                if len(payload) >= MAX_BYTES:
                    raise SourceError("DBLP response exceeds 32 MB safety limit")
                try:
                    body = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise SourceError("DBLP returned unparsable JSON") from exc
                if not isinstance(body, dict):
                    raise SourceError("DBLP returned a non-object JSON payload")
                if body.get("exception"):
                    raise SourceError("DBLP SPARQL error: " + str(body["exception"])[:300])
                if not isinstance(body.get("results"), dict):
                    raise SourceError("DBLP response lacks SPARQL results")
                return body
            except urllib.error.HTTPError as exc:  # noqa: PERF203
                detail = ""
                try:
                    detail = exc.read(400).decode("utf-8", "replace").strip()
                except OSError:
                    pass
                message = f"HTTP {exc.code} from DBLP"
                if detail:
                    message += ": " + " ".join(detail.split())[:300]
                if exc.code not in RETRY_STATUS:
                    raise SourceError(message) from exc
                retry_after = _retry_after(exc)
                if retry_after is not None and retry_after > 180:
                    raise SourceError("DBLP asked for a long Retry-After; retry next run") from exc
                last_error = SourceError(message)
                time.sleep(retry_after if retry_after is not None else min(60, 5 * 2**attempt))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = SourceError(f"network error talking to DBLP: {exc}")
                time.sleep(min(30, 5 * 2**attempt))
        raise last_error or SourceError("DBLP request failed")


def _stream_uri(stream) -> str:
    raw = str(stream or "").strip().rstrip("/")
    prefix = "https://dblp.org/streams/"
    if raw.startswith(prefix):
        raw = raw[len(prefix) :]
    if not STREAM_RE.match(raw):
        raise SourceError(f"invalid DBLP stream id: {stream!r} (expected e.g. conf/cvpr)")
    return prefix + raw


def _year_text(year) -> str:
    try:
        value = int(year)
    except (TypeError, ValueError) as exc:
        raise SourceError(f"invalid DBLP year: {year!r}") from exc
    if not 1900 <= value <= 2100:
        raise SourceError(f"DBLP year out of range: {value}")
    return str(value)


def _author_list(raw) -> list:
    """把 ``序号\\t姓名|序号\\t姓名`` 还原成有序作者,并去掉 DBLP 消歧后缀。"""
    items = []
    for chunk in str(raw or "").split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        order_text, separator, name = chunk.partition("\t")
        if not separator:
            order_text, name = "9999", chunk
        try:
            order = int(order_text.strip())
        except ValueError:
            order = 9999
        name = _DBLP_AUTHOR_SUFFIX.sub("", " ".join(name.split()))
        if name:
            items.append((order, name))
    items.sort(key=lambda item: item[0])
    return [name for _order, name in items]


def _arxiv_ids(doi: str, links) -> list:
    found = []
    candidates = [("doi:" + doi) if doi else "", *(links or [])]
    for raw in candidates:
        aid, _version = arxiv_from_link(raw)
        if aid and aid not in found:
            found.append(aid)
    return found


def _clean_title(raw) -> str:
    title = " ".join(str(raw or "").split())
    # DBLP 的 RDF 标题习惯在末尾加句号;去掉一个,避免和 arXiv/OpenAlex 标题对不齐。
    if title.endswith(".") and not title.endswith(".."):
        title = title[:-1].rstrip()
    return title


def _ee_links(raw) -> list:
    links = []
    for chunk in str(raw or "").split("|"):
        url = safe_url(chunk.strip())
        if url and url not in links:
            links.append(url)
    return links


def _pdf_url(arxiv_ids, links) -> str:
    if arxiv_ids:
        return ARXIV_PDF + arxiv_ids[0]
    for url in links:
        base = url.split("?", 1)[0].split("#", 1)[0].lower()
        if base.endswith(".pdf"):
            return url
    return ""


def parse_binding(binding, year, venue_name="") -> dict:
    """把一条 SPARQL binding 转成站内 paper 记录。"""
    paper_uri = _text(binding.get("paper"))
    if not paper_uri.startswith("https://dblp.org/rec/"):
        raise SourceError(f"DBLP row lacks a usable record URI: {paper_uri!r}")
    title = _clean_title(_text(binding.get("title")))
    if not title:
        raise SourceError(f"DBLP record {paper_uri} lacks a title")

    doi = doi_id(_text(binding.get("doi")))
    links = _ee_links(_text(binding.get("ee")))
    arxiv_ids = _arxiv_ids(doi, links)
    webpage = links[0] if links else paper_uri
    url = "https://doi.org/" + doi if doi else webpage
    pdf = _pdf_url(arxiv_ids, links)
    key = paper_uri.rsplit("/rec/", 1)[-1]
    venue = _text(binding.get("venue")) or str(venue_name or "").strip()

    return {
        "id": "doi:" + doi if doi else "dblp:" + key,
        "title": title,
        "authors": _author_list(_raw(binding.get("authors"))),
        "abstract": "",
        "published": f"{int(year):04d}-01-01T00:00:00Z",
        "published_precision": "year",
        "updated": f"{int(year):04d}-01-01T00:00:00Z",
        "version": 0,
        "identifiers": {"arxiv": arxiv_ids, "doi": [doi] if doi else []},
        "url": url,
        "pdf": pdf,
        "oa_pdf": pdf,
        "venue": venue,
        "comment": "",
        "categories": ["Inproceedings"],
        "origin": "dblp",
    }


def _count(payload) -> int:
    bindings = (payload.get("results") or {}).get("bindings")
    if not isinstance(bindings, list) or not bindings:
        raise SourceError("DBLP count query returned no count")
    raw = _text(bindings[0].get("count"))
    if not raw.isdigit():
        raise SourceError(f"DBLP count query returned a non-integer count: {raw!r}")
    return int(raw)


def venue_year(
    client,
    stream,
    year,
    page_size=500,
    max_pages=20,
    venue_name="",
) -> list:
    """拉取某会议某年份的全部论文;任何分页异常都抛 SourceError。"""
    uri = _stream_uri(stream)
    year_text = _year_text(year)
    try:
        size = int(page_size)
        pages = int(max_pages)
    except (TypeError, ValueError) as exc:
        raise SourceError("DBLP page_size/max_pages must be integers") from exc
    if not 1 <= size <= 1000:
        raise SourceError("DBLP page_size must be within 1..1000")
    if pages < 1:
        raise SourceError("DBLP max_pages must be >= 1")

    total = _count(client.select(COUNT_QUERY.format(stream=uri, year=year_text)))
    if total == 0:
        return []
    if total > size * pages:
        raise SourceError(
            f"DBLP {stream} {year_text} has {total} records, above the configured "
            f"{size}x{pages} safety limit; raise dblp.max_pages_per_venue_year"
        )

    found, seen = [], set()
    offset = 0
    while offset < total:
        payload = client.select(
            PAGE_QUERY.format(
                stream=uri,
                year=year_text,
                limit=size,
                offset=offset,
            )
        )
        bindings = (payload.get("results") or {}).get("bindings")
        if not isinstance(bindings, list) or not bindings:
            raise SourceError(f"empty DBLP page at offset {offset} but count={total}")
        for binding in bindings:
            # 用 DBLP 记录 URI 检测翻页重复;两条不同记录若碰巧共用 DOI,
            # 交给合并层按身份去重,不应误报分页失败。
            record_uri = _text(binding.get("paper"))
            if not record_uri:
                raise SourceError(f"DBLP row at offset {offset} lacks a record URI")
            if record_uri in seen:
                raise SourceError(f"duplicate DBLP record across pages: {record_uri}")
            seen.add(record_uri)
            found.append(parse_binding(binding, int(year_text), venue_name=venue_name))
        offset += len(bindings)
        if len(found) > total:
            raise SourceError(
                f"DBLP count changed while paging {stream} {year_text}: got {len(found)} of {total}"
            )
    if len(found) != total:
        raise SourceError(
            f"DBLP pagination incomplete for {stream} {year_text}: got {len(found)} of {total}"
        )
    return found
