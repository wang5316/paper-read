"""arXiv Atom API 客户端与解析。

设计要点(和上游一致,因为这几点是对的):
  * 节流下限 3 秒 —— arXiv 的 ToU 要求,不要调低;
  * 只有「被证明完整的窗口」才算成功:拿不到 totalResults、中间页为空、
    跨页重复 ID、翻页无进展,统统抛 SourceError,而不是当成 0 条结果;
  * 版本号只从 <id> 的 vN 取,拿不到记为 0(未知),由主链决定怎么用。
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from .common import ARXIV_ABS, ARXIV_PDF, arxiv_ref, doi_id, iso, parse_time

ENDPOINT = "https://export.arxiv.org/api/query"
MAX_BYTES = 12_000_001
RETRY_STATUS = {429, 500, 502, 503, 504}

A = "{http://www.w3.org/2005/Atom}"
X = "{http://arxiv.org/schemas/atom}"
OS = "{http://a9.com/-/spec/opensearch/1.1/}"

_DTD = re.compile(rb"<!(DOCTYPE|ENTITY)", re.I)


class SourceError(RuntimeError):
    """抓取失败。消息会原样进 status.sources[].error,所以要写人话。"""


def _text(node) -> str:
    return " ".join((node.text or "").split()) if node is not None else ""


class Client:
    """带节流与有限重试的 HTTP 客户端;opener 可注入,便于离线测试。"""

    def __init__(self, interval=5.0, user_agent="observatory/1.0", timeout=60, attempts=5, opener=None):
        self.interval = max(3.0, float(interval))
        self.user_agent = user_agent
        self.timeout = float(timeout)
        self.attempts = max(1, int(attempts))
        self._last = 0.0
        self._opener = opener or urllib.request.urlopen

    def _throttle(self):
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, params: dict) -> bytes:
        url = ENDPOINT + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/atom+xml"}
        )
        last_error = None
        for attempt in range(self.attempts):
            self._throttle()
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    payload = response.read(MAX_BYTES)
                if len(payload) >= MAX_BYTES:
                    raise SourceError("arXiv response exceeds 12 MB safety limit")
                return payload
            except urllib.error.HTTPError as exc:  # noqa: PERF203
                if exc.code not in RETRY_STATUS:
                    raise SourceError(f"HTTP {exc.code} from arXiv") from exc
                retry_after = _retry_after(exc)
                if retry_after is not None and retry_after > 180:
                    raise SourceError("arXiv asked for a long Retry-After; retry next run") from exc
                last_error = SourceError(f"HTTP {exc.code} from arXiv")
                time.sleep(retry_after if retry_after is not None else min(60, 5 * 2**attempt))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = SourceError(f"network error talking to arXiv: {exc}")
                time.sleep(min(30, 5 * 2**attempt))
        raise last_error or SourceError("arXiv request failed")


def _retry_after(exc):
    raw = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        from email.utils import parsedate_to_datetime

        return max(0.0, (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _journal_ref(raw) -> str:
    """校验 arXiv 的 journal_ref 字段。

    这个字段是作者自由填写的,经常被写成引用串或备注,例如:
      "Copilot4D: Learning Unsupervised World Models ... arXiv:2311.01017, 2023"
      "Accepted at CVPR 2025; see also arXiv:2401.00001"
    这类值当期刊名会污染 venue 与「期刊与会议论文」专题,所以只接受
    「像刊物名」的短文本:不含 arXiv/doi 引用痕迹、不含年份逗号后缀、长度受限。
    """
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    lowered = text.lower()
    if "arxiv:" in lowered or "arxiv.org" in lowered or "doi:" in lowered:
        return ""
    if re.search(r"\b(19|20)\d{2}\s*[;,.、]\s*\d{0,2}\s*$", text):
        return ""
    if len(text) > 90 or text.count(".") > 1:
        return ""
    if re.search(r"\b(accepted|submitted|to appear|under review|preprint)\b", lowered):
        return ""
    return text


def parse_entry(entry) -> dict:
    raw_id = _text(entry.find(A + "id"))
    aid, version = arxiv_ref(raw_id)
    if not aid:
        raise SourceError(f"entry without a usable arXiv id: {raw_id!r}")
    title = _text(entry.find(A + "title"))
    published = _text(entry.find(A + "published"))
    updated = _text(entry.find(A + "updated"))
    if not title or not published or not updated:
        raise SourceError(f"entry {aid} lacks title/published/updated")
    parse_time(published)
    parse_time(updated)
    doi = doi_id(_text(entry.find(X + "doi")))
    return {
        "id": "arxiv:" + aid,
        "title": title,
        "authors": [_text(node) for node in entry.findall(A + "author/" + A + "name") if _text(node)],
        "abstract": _text(entry.find(A + "summary")),
        "published": published,
        "updated": updated,
        "version": version,
        "identifiers": {"arxiv": [aid], "doi": [doi] if doi else []},
        "url": ARXIV_ABS + aid,
        "pdf": ARXIV_PDF + aid,
        "venue": _journal_ref(_text(entry.find(X + "journal_ref"))) or "arXiv",
        "comment": _text(entry.find(X + "comment")),
        "categories": [node.get("term") for node in entry.findall(A + "category") if node.get("term")],
    }


def parse_feed(payload: bytes):
    """返回 (entries, totalResults)。缺 totalResults 就是失败,不是「空结果」。"""
    if _DTD.search(payload):
        raise SourceError("refusing Atom feed with DTD/ENTITY declarations")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise SourceError(f"unparsable Atom feed: {exc}") from exc
    if root.tag != A + "feed":
        raise SourceError(f"unexpected Atom root element: {root.tag}")
    total_node = root.find(OS + "totalResults")
    raw_total = _text(total_node)
    if not raw_total.isdigit():
        raise SourceError("Feed lacks totalResults; refusing to mark pagination complete")
    entries = [parse_entry(node) for node in root.findall(A + "entry")]
    return entries, int(raw_total)


def _window_bounds(start: datetime, stop: datetime) -> str:
    fmt = "%Y%m%d%H%M"
    return f"[{start.astimezone(timezone.utc):{fmt}} TO {stop.astimezone(timezone.utc):{fmt}}]"


def query_window(client, query, start, stop, page_size=50, max_pages=12):
    """拉取一个时间窗内的全部结果;任何分页异常都抛 SourceError。"""
    search_query = f"({query}) AND submittedDate:{_window_bounds(start, stop)}"
    found, seen, offset = [], set(), 0
    for _ in range(max_pages):
        payload = client.get(
            {
                "search_query": search_query,
                "start": offset,
                "max_results": page_size,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        entries, total = parse_feed(payload)
        # totalResults=0 是「窗口内确实没有」的正面证据,不是失败。
        if offset >= total:
            return found
        if not entries:
            raise SourceError(f"empty page at offset {offset} but totalResults={total}")
        added = 0
        for entry in entries:
            if entry["id"] in seen:
                raise SourceError(f"duplicate id across pages: {entry['id']}")
            seen.add(entry["id"])
            found.append(entry)
            added += 1
        if not added:
            raise SourceError(f"pagination made no progress at offset {offset}")
        offset += page_size
        if offset >= total:
            return found
    raise SourceError(
        f"pagination incomplete after {max_pages} pages (window too wide?); narrow max_window_days"
    )


def revisions(client, ids):
    """按 id_list 批量巡检版本;请求的编号必须全部返回,否则整批失败。"""
    wanted = [str(item) for item in ids if item]
    if not wanted:
        return []
    payload = client.get({"id_list": ",".join(wanted), "max_results": len(wanted)})
    entries, _total = parse_feed(payload)
    returned = {entry["identifiers"]["arxiv"][0] for entry in entries}
    missing = [item for item in wanted if item not in returned]
    if missing:
        raise SourceError("revision query omitted requested IDs: " + ", ".join(missing[:5]))
    return entries


def window_label(start, stop) -> str:
    return f"{iso(start)} → {iso(stop)}"
