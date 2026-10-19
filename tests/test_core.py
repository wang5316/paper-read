"""离线单测:不联网、不需要密钥,用合成 Atom 与假客户端覆盖关键判定。

跑法:python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas import arxiv_source, dblp_source, enrich, openalex_source, site, triage  # noqa: E402
from atlas.common import (  # noqa: E402
    ConfigError,
    arxiv_from_link,
    identity_keys,
    load_config,
    read_json,
    utcnow,
    write_json,
)
from atlas.editorial import REVIEW_MARKER, apply_review, parse_review_request  # noqa: E402
from atlas.pipeline import EMPTY, _dblp_years, _stage_papers, merge_paper, run_update  # noqa: E402

CONFIG = load_config(ROOT / "config.json")
NOW = utcnow().replace(microsecond=0)
STAMP = NOW.isoformat().replace("+00:00", "Z")

# 临时目录必须落在仓库内:有些沙箱/CI 对系统临时目录的写入会卡住或拒绝。
SCRATCH = ROOT / ".scratch"


def make_scratch(name) -> Path:
    path = SCRATCH / f"{name}-{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def atom_feed(entries, total=None) -> bytes:
    """entries: [{"aid": "9900.00001", ...}];total=None 时故意省略 totalResults。"""
    count = len(entries) if total is None else total
    total_node = "" if total is None else f"<opensearch:totalResults>{count}</opensearch:totalResults>"
    body = "".join(
        f"<entry>"
        f"<id>http://arxiv.org/abs/{entry['aid']}v{entry.get('version', 1)}</id>"
        f"<title>{entry.get('title', 'A title about 3D semantic segmentation')}</title>"
        f"<summary>{entry.get('abstract', 'We study 3D semantic segmentation under domain adaptation.')}</summary>"
        f"<published>{entry.get('published', '2026-01-01T00:00:00Z')}</published>"
        f"<updated>{entry.get('updated', '2026-01-02T00:00:00Z')}</updated>"
        f"<author><name>{entry.get('author', 'Demo Author')}</name></author>"
        f"<arxiv:journal_ref>Demo Venue</arxiv:journal_ref>"
        f"</entry>"
        for entry in entries
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" '
        'xmlns:arxiv="http://arxiv.org/schemas/atom">'
        + total_node
        + body
        + "</feed>"
    ).encode("utf-8")


class FakeClient:
    """按 offset 返回固定页;pages 是 Atom 字节列表。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, params):
        self.calls.append(dict(params))
        offset = int(params.get("start", 0))
        index = offset // int(params.get("max_results", 50))
        if index >= len(self.pages):
            return atom_feed([], total=1)
        return self.pages[index]


class FailingClient:
    def get(self, params):
        raise arxiv_source.SourceError("boom")


def make_paper(aid="9900.00001", version=1, title=None, abstract=None, doi=None):
    return {
        "id": "arxiv:" + aid,
        "title": title or "Cross-Domain 3D Semantic Segmentation with Self-Training",
        "authors": ["Demo Author"],
        "abstract": abstract
        or "We tackle 3D semantic segmentation with domain adaptation and pseudo-label self-training.",
        "published": "2026-01-01T00:00:00Z",
        "updated": "2026-01-02T00:00:00Z",
        "version": version,
        "identifiers": {"arxiv": [aid], "doi": [doi] if doi else []},
        "url": "https://arxiv.org/abs/" + aid,
        "pdf": "https://arxiv.org/pdf/" + aid,
        "venue": "arXiv",
        "comment": "",
        "categories": ["cs.CV"],
    }


def dblp_node(value):
    return {"type": "literal", "value": value}


def dblp_binding(
    uri="https://dblp.org/rec/conf/cvpr/Demo26",
    title="3D Semantic Segmentation of Point Clouds",
    authors=("2\tBob", "1\tAlice 0001"),
    doi="",
    ee="",
    venue="CVPR",
    pages="1-10",
):
    binding = {
        "paper": {"type": "uri", "value": uri},
        "title": dblp_node(title),
        "authors": dblp_node("|".join(authors)),
        "venue": dblp_node(venue),
        "pages": dblp_node(pages),
    }
    if doi:
        binding["doi"] = dblp_node(doi)
    if ee:
        binding["ee"] = dblp_node(ee)
    return binding


class FakeDblpClient:
    """按 COUNT / LIMIT-OFFSET 返回固定页;offset 以实际页长为步进。"""

    def __init__(self, total, pages):
        self.total = total
        self.pages = list(pages)
        self.calls = []

    def select(self, query):
        self.calls.append(query)
        if "COUNT(DISTINCT" in query:
            return {"results": {"bindings": [{"count": dblp_node(str(self.total))}]}}
        match = re.search(r"LIMIT (\d+) OFFSET (\d+)", query)
        if not match:
            raise AssertionError("page query lacks LIMIT/OFFSET: " + query)
        limit, offset = int(match.group(1)), int(match.group(2))
        index = offset // limit
        bindings = self.pages[index] if index < len(self.pages) else []
        return {"results": {"bindings": bindings}}


class FailingDblpClient:
    def select(self, query):
        raise dblp_source.SourceError("dblp boom")


def oa_page(works, total, next_cursor=None):
    return {"meta": {"count": total, "next_cursor": next_cursor}, "results": list(works)}


def oa_work(
    wid="W1",
    title="3D Semantic Segmentation of Point Clouds",
    doi="https://doi.org/10.1109/tpami.2026.1000001",
    date="2026-01-15",
    arxiv="",
    author="Demo Author",
    abstract=None,
    venue="IEEE TPAMI",
):
    work = {
        "id": "https://openalex.org/" + wid,
        "doi": doi,
        "title": title,
        "publication_date": date,
        "publication_year": int(date[:4]) if date else None,
        "updated_date": (date + "T00:00:00Z") if date else "",
        "ids": {"openalex": "https://openalex.org/" + wid},
        "authorships": [{"author": {"display_name": author}}],
        "primary_location": {
            "landing_page_url": doi or ("https://openalex.org/" + wid),
            "source": {"display_name": venue},
        },
        "best_oa_location": {},
        "open_access": {},
        "locations": [],
        "type": "article",
        "abstract_inverted_index": abstract,
    }
    if arxiv:
        work["locations"].append(
            {
                "landing_page_url": "https://arxiv.org/abs/" + arxiv,
                "pdf_url": "https://arxiv.org/pdf/" + arxiv,
            }
        )
    return work


class FakeOpenAlexClient:
    """按游标翻页的假客户端;has_api_key=True 让它走游标分支(有 key 的路径)。"""

    has_api_key = True

    def __init__(self, pages_by_cursor):
        self.pages_by_cursor = dict(pages_by_cursor)
        self.calls = []

    def get(self, params):
        self.calls.append(dict(params))
        cursor = params.get("cursor", "*")
        if cursor not in self.pages_by_cursor:
            raise AssertionError(f"unexpected OpenAlex cursor: {cursor!r}")
        return self.pages_by_cursor[cursor]


class FakeOpenAlexPageClient:
    """无 key 的假客户端:没有 cursor,按 page 页码返回,用于验证降级路径。"""

    has_api_key = False

    def __init__(self, pages_by_number):
        self.pages_by_number = dict(pages_by_number)
        self.calls = []

    def get(self, params):
        self.calls.append(dict(params))
        if "cursor" in params:
            raise AssertionError("无 key 时不应使用游标翻页")
        page = int(params.get("page", 1))
        if page not in self.pages_by_number:
            raise AssertionError(f"unexpected OpenAlex page: {page!r}")
        return self.pages_by_number[page]


class FeedTests(unittest.TestCase):
    def test_missing_total_results_is_failure(self):
        payload = (
            '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry><id>http://arxiv.org/abs/9900.00001v1</id><title>T</title>"
            "<published>2026-01-01T00:00:00Z</published><updated>2026-01-01T00:00:00Z</updated>"
            "</entry></feed>"
        ).encode()
        with self.assertRaises(arxiv_source.SourceError):
            arxiv_source.parse_feed(payload)

    def test_dtd_rejected(self):
        payload = b'<?xml version="1.0"?><!DOCTYPE feed SYSTEM "x"><feed/>'
        with self.assertRaises(arxiv_source.SourceError):
            arxiv_source.parse_feed(payload)

    def test_entry_fields_parsed(self):
        entries, total = arxiv_source.parse_feed(atom_feed([{"aid": "9900.00001"}], total=1))
        self.assertEqual(total, 1)
        self.assertEqual(entries[0]["version"], 1)
        self.assertEqual(entries[0]["identifiers"]["arxiv"], ["9900.00001"])
        self.assertEqual(entries[0]["venue"], "Demo Venue")

    def test_query_window_paginates(self):
        client = FakeClient(
            [
                atom_feed([{"aid": "9900.00001"}], total=2),
                atom_feed([{"aid": "9900.00002"}], total=2),
            ]
        )
        papers = arxiv_source.query_window(
            client, "all:test", NOW - timedelta(days=1), NOW, page_size=1, max_pages=5
        )
        self.assertEqual([paper["id"] for paper in papers], ["arxiv:9900.00001", "arxiv:9900.00002"])
        self.assertEqual(len(client.calls), 2)

    def test_duplicate_ids_across_pages_rejected(self):
        client = FakeClient(
            [
                atom_feed([{"aid": "9900.00001"}], total=2),
                atom_feed([{"aid": "9900.00001"}], total=2),
            ]
        )
        with self.assertRaises(arxiv_source.SourceError):
            arxiv_source.query_window(
                client, "all:test", NOW - timedelta(days=1), NOW, page_size=1, max_pages=5
            )

    def test_empty_window_is_success_not_failure(self):
        # totalResults=0 是「窗口内确实没有」的证据,必须返回空列表而不是抛错。
        client = FakeClient([atom_feed([], total=0)])
        papers = arxiv_source.query_window(
            client, "all:test", NOW - timedelta(days=1), NOW, page_size=10, max_pages=3
        )
        self.assertEqual(papers, [])
        self.assertEqual(len(client.calls), 1)

    def test_revisions_omitted_id_is_failure(self):
        client = FakeClient([atom_feed([{"aid": "9900.00001"}], total=1)])
        with self.assertRaises(arxiv_source.SourceError):
            arxiv_source.revisions(client, ["9900.00001", "9900.99999"])


class DblpSourceTests(unittest.TestCase):
    def test_venue_year_paginates_and_parses_authors(self):
        client = FakeDblpClient(
            2,
            [
                [
                    dblp_binding(
                        uri="https://dblp.org/rec/conf/cvpr/First26",
                        doi="10.1109/CVPR.2026.00001",
                        ee="https://arxiv.org/abs/2601.00001",
                        title="Point Cloud Segmentation with LiDAR.",
                    )
                ],
                [
                    dblp_binding(
                        uri="https://dblp.org/rec/conf/cvpr/Second26",
                        title="Gaussian Splatting for 3D Scenes",
                    )
                ],
            ],
        )
        papers = dblp_source.venue_year(client, "conf/cvpr", 2026, page_size=1, max_pages=5)

        self.assertEqual(len(papers), 2)
        self.assertEqual(len(client.calls), 3)  # count + 2 pages
        first = papers[0]
        self.assertEqual(first["title"], "Point Cloud Segmentation with LiDAR")
        self.assertEqual(first["authors"], ["Alice", "Bob"])
        self.assertEqual(first["identifiers"]["arxiv"], ["2601.00001"])
        self.assertEqual(first["id"], "doi:10.1109/cvpr.2026.00001")
        self.assertEqual(first["origin"], "dblp")
        self.assertEqual(first["venue"], "CVPR")
        self.assertEqual(first["published_precision"], "year")

    def test_regular_doi_tail_is_not_an_arxiv_id(self):
        binding = dblp_binding(
            uri="https://dblp.org/rec/conf/cvpr/Tail26",
            doi="10.1109/cvpr52733.2024.01234",
        )
        paper = dblp_source.parse_binding(binding, 2024)
        self.assertEqual(paper["identifiers"]["arxiv"], [])

    def test_incomplete_pagination_is_failure(self):
        client = FakeDblpClient(2, [[dblp_binding()]])
        with self.assertRaises(dblp_source.SourceError):
            dblp_source.venue_year(client, "conf/cvpr", 2026, page_size=1, max_pages=5)

    def test_duplicate_record_across_pages_is_failure(self):
        binding = dblp_binding()
        client = FakeDblpClient(2, [[binding], [binding]])
        with self.assertRaises(dblp_source.SourceError):
            dblp_source.venue_year(client, "conf/cvpr", 2026, page_size=1, max_pages=5)

    def test_count_above_page_safety_limit_is_failure(self):
        client = FakeDblpClient(10, [])
        with self.assertRaises(dblp_source.SourceError):
            dblp_source.venue_year(client, "conf/cvpr", 2026, page_size=1, max_pages=2)

    def test_zero_count_is_success(self):
        client = FakeDblpClient(0, [])
        self.assertEqual(
            dblp_source.venue_year(client, "conf/cvpr", 2026, page_size=10, max_pages=2), []
        )
        self.assertEqual(len(client.calls), 1)


class OpenAlexSourceTests(unittest.TestCase):
    JOURNAL = {
        "id": "tpami",
        "name": "IEEE TPAMI",
        "source_id": "S199944782",
        "issn": ["0162-8828"],
    }

    def test_abstract_reconstruction(self):
        index = {"We": [0], "study": [1], "3D": [2], "segmentation": [3], ".": [4]}
        self.assertEqual(openalex_source.reconstruct_abstract(index), "We study 3D segmentation .")
        self.assertEqual(openalex_source.reconstruct_abstract(None), "")
        self.assertEqual(openalex_source.reconstruct_abstract({"bad": "shape"}), "")

    def test_cursor_pagination(self):
        client = FakeOpenAlexClient(
            {
                "*": oa_page(
                    [oa_work(wid="W1", doi="https://doi.org/10.1109/tpami.2026.1000001")],
                    2,
                    "next-1",
                ),
                "next-1": oa_page(
                    [oa_work(wid="W2", doi="https://doi.org/10.1109/tpami.2026.1000002")],
                    2,
                    None,
                ),
            }
        )
        papers = openalex_source.journal_window(
            client, self.JOURNAL, NOW - timedelta(days=10), NOW, page_size=1, max_pages=5
        )
        self.assertEqual(
            [paper["id"] for paper in papers],
            ["doi:10.1109/tpami.2026.1000001", "doi:10.1109/tpami.2026.1000002"],
        )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["cursor"], "*")
        self.assertEqual(client.calls[1]["cursor"], "next-1")
        self.assertIn("primary_location.source.id:S199944782", client.calls[0]["filter"])

    def test_page_pagination_when_no_api_key(self):
        """没有 API key 时必须自动降级为页码翻页(游标翻页会被 OpenAlex 401 拒绝)。"""
        client = FakeOpenAlexPageClient(
            {
                1: oa_page(
                    [oa_work(wid="W1", doi="https://doi.org/10.1109/tpami.2026.1000001")],
                    2,
                    "next-1",
                ),
                2: oa_page(
                    [oa_work(wid="W2", doi="https://doi.org/10.1109/tpami.2026.1000002")],
                    2,
                    None,
                ),
            }
        )
        papers = openalex_source.journal_window(
            client, self.JOURNAL, NOW - timedelta(days=10), NOW, page_size=1, max_pages=5
        )
        self.assertEqual(
            [paper["id"] for paper in papers],
            ["doi:10.1109/tpami.2026.1000001", "doi:10.1109/tpami.2026.1000002"],
        )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["page"], 1)
        self.assertEqual(client.calls[1]["page"], 2)

    def test_no_key_rejects_window_above_page_limit(self):
        """无 key 时页码翻页上限 1 万条,超出要提前失败并说明原因。"""
        client = FakeOpenAlexPageClient({1: oa_page([oa_work(wid="W1")], 20_000, "next-1")})
        with self.assertRaises(openalex_source.SourceError) as ctx:
            # max_pages 给足,让它先撞上「无 key 1 万条」这条规则而不是 safety limit
            openalex_source.journal_window(
                client,
                self.JOURNAL,
                NOW - timedelta(days=10),
                NOW,
                page_size=1,
                max_pages=100_000,
            )
        self.assertIn("10000", str(ctx.exception))
        self.assertIn("OPENALEX_API_KEY", str(ctx.exception))

    def test_cursor_pagination_stops_when_total_reached(self):
        """OpenAlex 最后一页之后仍可能返回 next_cursor;收满 total 就必须停,
        否则会多请求一页空数据并被误判成「分页失败」。"""
        client = FakeOpenAlexClient(
            {
                "*": oa_page(
                    [oa_work(wid="W1", doi="https://doi.org/10.1109/tpami.2026.1000001")],
                    2,
                    "next-1",
                ),
                "next-1": oa_page(
                    [oa_work(wid="W2", doi="https://doi.org/10.1109/tpami.2026.1000002")],
                    2,
                    "next-2",  # 已经收满 2 条,这个游标不应被使用
                ),
            }
        )
        papers = openalex_source.journal_window(
            client, self.JOURNAL, NOW - timedelta(days=10), NOW, page_size=1, max_pages=5
        )
        self.assertEqual(len(papers), 2)
        self.assertEqual(len(client.calls), 2)

    def test_count_change_is_failure(self):
        client = FakeOpenAlexClient(
            {
                "*": oa_page([oa_work(wid="W1")], 2, "next-1"),
                "next-1": oa_page([oa_work(wid="W2")], 3, None),
            }
        )
        with self.assertRaises(openalex_source.SourceError):
            openalex_source.journal_window(
                client, self.JOURNAL, NOW - timedelta(days=10), NOW, page_size=1, max_pages=5
            )

    def test_duplicate_record_is_failure(self):
        work = oa_work(wid="W1")
        client = FakeOpenAlexClient(
            {"*": oa_page([work], 2, "next-1"), "next-1": oa_page([work], 2, None)}
        )
        with self.assertRaises(openalex_source.SourceError):
            openalex_source.journal_window(
                client, self.JOURNAL, NOW - timedelta(days=10), NOW, page_size=1, max_pages=5
            )

    def test_empty_page_before_count_is_failure(self):
        client = FakeOpenAlexClient(
            {"*": oa_page([], 1, "next-1"), "next-1": oa_page([], 1, None)}
        )
        with self.assertRaises(openalex_source.SourceError):
            openalex_source.journal_window(
                client, self.JOURNAL, NOW - timedelta(days=10), NOW, page_size=1, max_pages=5
            )

    def test_arxiv_ids_only_from_arxiv_links_or_doi(self):
        regular = openalex_source.parse_work(
            oa_work(doi="https://doi.org/10.1109/tpami.2024.01234")
        )
        self.assertEqual(regular["identifiers"]["arxiv"], [])

        arxiv_doi = openalex_source.parse_work(
            oa_work(wid="W2", doi="https://doi.org/10.48550/arXiv.2301.12345")
        )
        self.assertEqual(arxiv_doi["identifiers"]["arxiv"], ["2301.12345"])

        linked = openalex_source.parse_work(oa_work(wid="W3", doi="", arxiv="2301.54321"))
        self.assertEqual(linked["identifiers"]["arxiv"], ["2301.54321"])

    def test_parse_work_keeps_abstract_and_oa_pdf(self):
        work = oa_work(
            abstract={"We": [0], "study": [1], "3D": [2]},
            venue="IEEE TPAMI",
        )
        work["best_oa_location"] = {"pdf_url": "https://example.org/paper.pdf"}
        paper = openalex_source.parse_work(work, self.JOURNAL)
        self.assertEqual(paper["abstract"], "We study 3D")
        self.assertEqual(paper["abstract_source"], "openalex")
        self.assertEqual(paper["oa_pdf"], "https://example.org/paper.pdf")
        self.assertEqual(paper["venue"], "IEEE TPAMI")
        self.assertEqual(paper["origin"], "openalex")


class ArxivLinkTests(unittest.TestCase):
    def test_only_arxiv_hosts_and_dois_are_extracted(self):
        self.assertEqual(arxiv_from_link("https://arxiv.org/abs/2301.12345v2"), ("2301.12345", 2))
        self.assertEqual(
            arxiv_from_link("https://doi.org/10.48550/arXiv.2301.12345"), ("2301.12345", 0)
        )
        self.assertEqual(arxiv_from_link("arXiv:2301.12345"), ("2301.12345", 0))
        self.assertEqual(arxiv_from_link("10.1109/cvpr52733.2024.01234"), ("", 0))


class IdentityTests(unittest.TestCase):
    def test_arxiv_and_doi_keys(self):
        paper = make_paper(doi="10.48550/arXiv.9900.00001")
        keys = identity_keys(paper)
        self.assertIn("arxiv:9900.00001", keys)
        self.assertIn("doi:10.48550/arxiv.9900.00001", keys)

    def test_version_suffix_stripped(self):
        paper = make_paper()
        paper["identifiers"]["arxiv"] = ["9900.00001v3"]
        self.assertEqual(identity_keys(paper), {"arxiv:9900.00001"})


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.store = json.loads(json.dumps(EMPTY))

    def test_new_then_unchanged_then_revised(self):
        self.assertEqual(merge_paper(self.store, [], make_paper(), STAMP, CONFIG), "new")
        self.assertEqual(merge_paper(self.store, [], make_paper(), STAMP, CONFIG), "unchanged")
        self.assertEqual(merge_paper(self.store, [], make_paper(version=2), STAMP, CONFIG), "revised")
        record = self.store["records"]["arxiv:9900.00001"]
        self.assertEqual(record["version"], 2)
        self.assertEqual(len(record["versions"]), 1)
        self.assertEqual([event["kind"] for event in self.store["events"]], ["discovered", "revised"])

    def test_stale_version_does_not_regress(self):
        merge_paper(self.store, [], make_paper(version=3), STAMP, CONFIG)
        merge_paper(self.store, [], make_paper(version=1), STAMP, CONFIG)
        self.assertEqual(self.store["records"]["arxiv:9900.00001"]["version"], 3)

    def test_first_seen_is_stable(self):
        merge_paper(self.store, [], make_paper(), STAMP, CONFIG)
        first = self.store["records"]["arxiv:9900.00001"]["first_seen"]
        later = (NOW + timedelta(days=3)).isoformat().replace("+00:00", "Z")
        merge_paper(self.store, [], make_paper(version=2), later, CONFIG)
        self.assertEqual(self.store["records"]["arxiv:9900.00001"]["first_seen"], first)

    def test_identity_conflict_writes_nothing(self):
        first = dict(make_paper(), id="arxiv:9900.00001")
        first["identifiers"] = {"arxiv": ["9900.00001"], "doi": []}
        second = dict(make_paper(), id="arxiv:9900.00002")
        second["identifiers"] = {"arxiv": ["9900.00002"], "doi": ["10.48550/arxiv.9900.00009"]}
        self.store["records"]["arxiv:9900.00001"] = first
        self.store["records"]["arxiv:9900.00002"] = second

        incoming = make_paper(aid="9900.00009")
        incoming["identifiers"] = {"arxiv": ["9900.00001"], "doi": ["10.48550/arxiv.9900.00009"]}
        kind = merge_paper(self.store, [], incoming, STAMP, CONFIG)

        self.assertEqual(kind, "conflict")
        self.assertIn("identity-conflict", [event["kind"] for event in self.store["events"]])
        self.assertNotIn("arxiv:9900.00009", self.store["records"])

    def test_manual_bucket_not_downgraded(self):
        self.store["records"]["arxiv:9900.00001"] = dict(
            make_paper(),
            imported=True,
            classification={"bucket": "core", "tracks": ["DA"], "task": "semantic", "methods": []},
        )
        vague = make_paper(version=2, abstract="A study of point cloud segmentation.", title="Point cloud study")
        merge_paper(self.store, [], vague, STAMP, CONFIG)
        record = self.store["records"]["arxiv:9900.00001"]
        self.assertEqual(record["classification"]["bucket"], "core")
        self.assertIn("DA", record["classification"]["tracks"])

    def test_title_collision_only_warns(self):
        self.store["records"]["arxiv:9900.00001"] = make_paper()
        twin = make_paper(aid="9900.00002", title=make_paper()["title"])
        merge_paper(self.store, [], twin, STAMP, CONFIG)
        record = self.store["records"]["arxiv:9900.00002"]
        self.assertTrue(any("相同题名" in warning for warning in record["warnings"]))

    def test_non_arxiv_merge_keeps_arxiv_fields_and_version(self):
        merge_paper(self.store, [], make_paper(doi="10.1000/demo"), STAMP, CONFIG)
        incoming = dblp_source.parse_binding(
            dblp_binding(doi="10.1000/demo", venue="CVPR"), 2026
        )
        kind = merge_paper(self.store, [], incoming, STAMP, CONFIG)

        record = self.store["records"]["arxiv:9900.00001"]
        self.assertEqual(kind, "metadata")
        self.assertEqual(record["version"], 1)
        self.assertEqual(record["origin"], "arxiv")
        self.assertEqual(record["sources"], ["arxiv", "dblp"])
        self.assertEqual(record["venue"], "CVPR")
        self.assertTrue(record["abstract"])
        self.assertEqual(record["updated"], "2026-01-02T00:00:00Z")
        self.assertIn("cs.CV", record["categories"])
        self.assertIn("Inproceedings", record["categories"])
        self.assertNotIn("revised", [event["kind"] for event in self.store["events"]])

    def test_scope_prefilter_keeps_known_identity(self):
        known = make_paper(doi="10.1000/demo")
        self.store["records"][known["id"]] = known
        known_incoming = dblp_source.parse_binding(
            dblp_binding(
                uri="https://dblp.org/rec/conf/cvpr/Known26",
                title="Database Systems",
                doi="10.1000/demo",
            ),
            2026,
        )
        unrelated = dblp_source.parse_binding(
            dblp_binding(
                uri="https://dblp.org/rec/conf/cvpr/Other26",
                title="Database Systems",
                doi="10.1000/other",
            ),
            2026,
        )
        stats = dict.fromkeys(
            ("new", "revised", "existing", "metadata", "unchanged", "conflict"), 0
        )
        staged, _local, stored, skipped = _stage_papers(
            CONFIG, self.store, [], [known_incoming, unrelated], STAMP, stats, prefilter=True
        )

        self.assertEqual((stored, skipped), (1, 1))
        self.assertIn("arxiv:9900.00001", staged["records"])
        self.assertNotIn("doi:10.1000/other", staged["records"])
        self.assertIn("dblp", staged["records"]["arxiv:9900.00001"]["sources"])


class ClassifyTests(unittest.TestCase):
    def test_core_related_excluded(self):
        core = triage.classify(
            "Unsupervised 3D Semantic Segmentation of Point Clouds",
            "We propose a self-supervised and contrastive learning framework for point cloud "
            "semantic segmentation without manual annotations.",
            CONFIG["classify"],
        )
        self.assertEqual(core["bucket"], "core")
        self.assertEqual(core["task"], "semantic")
        self.assertIn("UPC", core["tracks"])

        excluded = triage.classify(
            "2D Image Segmentation with Contrastive Learning",
            "Contrastive learning for natural image semantic segmentation.",
            CONFIG["classify"],
        )
        self.assertEqual(excluded["bucket"], "excluded")

    def test_track_requires_guard(self):
        # 出现 unsupervised 但没有任何 3D / 点云线索时,UPC 不该被赋上(track.requires 生效)
        result = triage.classify(
            "Unsupervised Clustering for Image Segmentation",
            "An unsupervised method for natural image segmentation.",
            CONFIG["classify"],
        )
        self.assertNotIn("UPC", result["tracks"])

    def test_config_rejects_unknown_topic_method(self):
        broken = json.loads((ROOT / "config.json").read_text("utf-8"))
        broken["topics"][1]["items"][0]["match"]["methods"] = ["does-not-exist"]
        path = make_scratch("config") / "config.json"
        write_json(path, broken)
        with self.assertRaises(ConfigError):
            load_config(path)


class SourceConfigTests(unittest.TestCase):
    def _broken(self):
        return json.loads((ROOT / "config.json").read_text("utf-8"))

    def test_duplicate_dblp_venue_is_rejected(self):
        config = self._broken()
        config["dblp"]["venues"].append(dict(config["dblp"]["venues"][0]))
        path = make_scratch("config") / "config.json"
        write_json(path, config)
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_openalex_bad_issn_is_rejected(self):
        config = self._broken()
        config["openalex"]["journals"][0]["issn"] = ["not-an-issn"]
        path = make_scratch("config") / "config.json"
        write_json(path, config)
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_no_arxiv_queries_allowed_when_other_sources_enabled(self):
        config = self._broken()
        for query in config["arxiv"]["queries"]:
            query["enabled"] = False
        path = make_scratch("config") / "config.json"
        write_json(path, config)
        loaded = load_config(path)
        self.assertTrue(loaded["dblp"]["enabled"])
        self.assertTrue(loaded["openalex"]["enabled"])

    def test_all_sources_disabled_is_rejected(self):
        config = self._broken()
        for query in config["arxiv"]["queries"]:
            query["enabled"] = False
        config["dblp"]["enabled"] = False
        config["openalex"]["enabled"] = False
        path = make_scratch("config") / "config.json"
        write_json(path, config)
        with self.assertRaises(ConfigError):
            load_config(path)


class NewBehaviourTests(unittest.TestCase):
    """本次新增能力:会议年份白名单、require_track 预筛、journal_ref 校验。"""

    def test_venue_years_whitelist(self):
        config = json.loads((ROOT / "config.json").read_text("utf-8"))
        config["dblp"]["venues"] = [
            {"id": "iccv", "name": "ICCV", "stream": "conf/iccv", "years": [2023, 2025]}
        ]
        years = _dblp_years(config, NOW, venue=config["dblp"]["venues"][0])
        self.assertEqual(years, [2023, 2025])

    def test_venue_years_excludes_future(self):
        """没有显式 years 时,滚动窗口不应包含未来年份(会议还没开)。"""
        config = json.loads((ROOT / "config.json").read_text("utf-8"))
        config["dblp"]["lookback_years"] = 2
        years = _dblp_years(config, NOW, venue={"id": "cvpr"})
        self.assertNotIn(NOW.year + 1, years)
        self.assertIn(NOW.year, years)

    def test_require_track_drops_papers_without_track(self):
        config = json.loads((ROOT / "config.json").read_text("utf-8"))
        store = json.loads(json.dumps(EMPTY))
        stats = {k: 0 for k in ("new", "revised", "existing", "metadata", "unchanged", "conflict")}
        papers = [
            # 命中方向:3D Gaussian + semantic
            dblp_like("dblp:1", "Semantic 3D Gaussian Splatting for Scene Understanding"),
            # 纯重建类高斯论文:有 GS 关键词但没有语义类词,应被 require_track 丢掉
            dblp_like("dblp:2", "Fast 3D Gaussian Splatting Rendering with Sparse Pixels"),
            # 与 3D 无关,连 scope 都不进
            dblp_like("dblp:3", "A Survey on Financial Time Series Forecasting"),
        ]
        staged, _local, stored, skipped = _stage_papers(
            config, store, [], papers, STAMP, stats, prefilter=True, require_track=True
        )
        self.assertEqual(stored, 1)
        self.assertEqual(skipped, 2)
        self.assertEqual(list(staged["records"]), ["dblp:1"])

    def test_gs_requires_semantic_context(self):
        """3D 高斯方向必须同时出现语义/理解类词,纯重建渲染类不算。"""
        config = json.loads((ROOT / "config.json").read_text("utf-8"))
        semantic = triage.classify(
            "Semantic 3D Gaussian Splatting for Scene Understanding", "", config["classify"]
        )
        self.assertIn("GS", semantic["tracks"])
        render_only = triage.classify(
            "Fast 3D Gaussian Splatting Rendering with Sparse Pixels", "", config["classify"]
        )
        self.assertNotIn("GS", render_only["tracks"])

    def test_require_track_off_keeps_related(self):
        """关掉 require_track 时,进入 scope 但没有方向标签的记录会被保留(bucket=related)。"""
        config = json.loads((ROOT / "config.json").read_text("utf-8"))
        store = json.loads(json.dumps(EMPTY))
        stats = {k: 0 for k in ("new", "revised", "existing", "metadata", "unchanged", "conflict")}
        # 有 3D 词(进 scope)、有分割词(bucket=related),但没有任何方向标签
        papers = [dblp_like("dblp:2", "3D Point Cloud Segmentation with a Novel Backbone")]
        staged, _local, stored, _skipped = _stage_papers(
            config, store, [], papers, STAMP, stats, prefilter=True, require_track=False
        )
        self.assertEqual(stored, 1)
        self.assertEqual(
            (staged["records"]["dblp:2"]["classification"] or {}).get("bucket"), "related"
        )

    def test_require_track_on_drops_that_same_paper(self):
        config = json.loads((ROOT / "config.json").read_text("utf-8"))
        store = json.loads(json.dumps(EMPTY))
        stats = {k: 0 for k in ("new", "revised", "existing", "metadata", "unchanged", "conflict")}
        papers = [dblp_like("dblp:2", "3D Point Cloud Segmentation with a Novel Backbone")]
        _staged, _local, stored, skipped = _stage_papers(
            config, store, [], papers, STAMP, stats, prefilter=True, require_track=True
        )
        self.assertEqual(stored, 0)
        self.assertEqual(skipped, 1)

    def test_journal_ref_validation(self):
        self.assertEqual(arxiv_source._journal_ref("IEEE TPAMI"), "IEEE TPAMI")
        self.assertEqual(arxiv_source._journal_ref("CVPR 2025"), "CVPR 2025")
        # 引用串 / 备注不该被当成期刊名
        self.assertEqual(
            arxiv_source._journal_ref(
                "Copilot4D: Learning Unsupervised World Models for Autonomous Driving "
                "via Discrete Diffusion. arXiv:2311.01017, 2023"
            ),
            "",
        )
        self.assertEqual(arxiv_source._journal_ref("Accepted at CVPR 2025"), "")
        self.assertEqual(arxiv_source._journal_ref(""), "")


def dblp_like(pid, title):
    return {
        "id": pid,
        "title": title,
        "authors": ["A"],
        "abstract": "",
        "published": "2025-01-01T00:00:00Z",
        "updated": "2025-01-01T00:00:00Z",
        "version": 0,
        "identifiers": {"arxiv": [], "doi": []},
        "url": "https://example.org",
        "pdf": "",
        "oa_pdf": "",
        "venue": "CVPR",
        "comment": "",
        "categories": ["Inproceedings"],
        "origin": "dblp",
    }


class ReviewAndSiteTests(unittest.TestCase):
    def setUp(self):
        self.root = make_scratch("site")
        (self.root / "web").mkdir()
        for name in ("index.html", "style.css", "app.js"):
            (self.root / "web" / name).write_text("/* " + name + " */", "utf-8")
        write_json(self.root / "config.json", json.loads((ROOT / "config.json").read_text("utf-8")))
        store = json.loads(json.dumps(EMPTY))
        merge_paper(store, [], make_paper(), STAMP, CONFIG)
        store["last_run"] = {"at": STAMP, "status": "ok", "counts": {"new": 1}, "sources": []}
        write_json(self.root / "data/store.json", store)
        write_json(self.root / "data/seed.json", [])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_reviewed_version_goes_stale(self):
        apply_review(self.root, CONFIG, "arxiv:9900.00001", state="metadata-checked")
        editorial = read_json(self.root / "data/editorial.json", {})
        self.assertEqual(editorial["arxiv:9900.00001"]["reviewed_version"], 1)

        store = read_json(self.root / "data/store.json", {})
        merge_paper(store, [], make_paper(version=2), STAMP, CONFIG)
        write_json(self.root / "data/store.json", store)

        papers = site.public_records(
            CONFIG, [], store, read_json(self.root / "data/editorial.json", {})
        )
        record = next(item for item in papers if item["id"] == "arxiv:9900.00001")
        self.assertEqual(record["review_status"], "review-stale")

    def test_build_writes_files_and_escapes_html(self):
        store = read_json(self.root / "data/store.json", {})
        store["records"]["arxiv:9900.00001"]["title"] = "Malicious <script>alert(1)</script>"
        write_json(self.root / "data/store.json", store)
        summary = site.build(CONFIG, root=self.root, out=self.root / "dist")
        self.assertEqual(summary["papers"], 1)
        blob = (self.root / "dist/data.js").read_text("utf-8")
        self.assertNotIn("<script>", blob)
        self.assertIn("\\u003c", blob)
        self.assertGreater(len((self.root / "dist/catalog.ris").read_text("utf-8")), 20)
        self.assertEqual((self.root / "dist/.nojekyll").read_text("utf-8"), "")

    def test_build_refuses_unknown_output_files(self):
        out = self.root / "dist"
        out.mkdir()
        (out / "notes.env").write_text("SECRET=1", "utf-8")
        with self.assertRaises(ValueError):
            site.build(CONFIG, root=self.root, out=out)

    def test_review_requires_marker(self):
        with self.assertRaises(Exception):
            parse_review_request("paper_id: arxiv:9900.00001\nstate: selected")
        fields = parse_review_request(
            REVIEW_MARKER
            + "\npaper_id: arxiv:9900.00001\nstate: selected\ntracks: DA\nnote: 重点跟进\n"
        )
        self.assertEqual(fields["paper_id"], "arxiv:9900.00001")
        self.assertEqual(fields["tracks"], "DA")
        self.assertEqual(fields["note"], "重点跟进")


class EnrichTests(unittest.TestCase):
    def test_split_sentences_keeps_abbreviations(self):
        text = "We use ResNet et al. baseline. Accuracy is 61.4 percent on the target domain. Fig. 3 shows it."
        sentences = enrich.split_sentences(text)
        self.assertTrue(any("et al." in sentence for sentence in sentences))
        self.assertFalse(any(sentence.strip() == "al." for sentence in sentences))

    def test_snippets_are_numbered(self):
        pages = [(1, "First sentence about 3D segmentation with domain adaptation. " * 2)]
        snippets = enrich.build_snippets(pages)
        self.assertTrue(snippets)
        self.assertEqual(snippets[0]["id"], "S1")
        self.assertEqual(snippets[0]["page"], 1)

    def test_unknown_snippet_rejects_draft(self):
        snippets = [{"id": "S1", "page": 1, "text": "We study 3D semantic segmentation."}]
        payload = {
            "summary_zh": "研究 3D 语义分割。",
            "problem": {"text": "问题", "evidence": [{"snippet": "S99"}]},
            "method": {"text": "方法", "evidence": [{"snippet": "S1"}]},
        }
        with self.assertRaises(enrich.DraftRejected):
            enrich.validate_analysis(payload, snippets)

    def test_unsupported_number_drops_item(self):
        snippets = [
            {"id": "S1", "page": 1, "text": "We study 3D semantic segmentation on point clouds."},
            {"id": "S2", "page": 2, "text": "Our method improves mIoU to 61.4 on the target domain."},
        ]
        payload = {
            "summary_zh": "研究 3D 语义分割。",
            "problem": {"text": "跨域掉点", "evidence": [{"snippet": "S1"}]},
            "method": {"text": "自训练", "evidence": [{"snippet": "S1"}]},
            "findings": [
                {"text": "mIoU 达到 61.4", "evidence": [{"snippet": "S2"}]},
                {"text": "提升了 99.9 个点", "evidence": [{"snippet": "S1"}]},
            ],
        }
        clean = enrich.validate_analysis(payload, snippets)
        self.assertEqual(len(clean["findings"]), 1)
        self.assertEqual(clean["findings"][0]["text"], "mIoU 达到 61.4")
        self.assertTrue(clean["dropped"])

    def test_number_is_rescued_by_extra_snippet(self):
        snippets = [
            {"id": "S1", "page": 1, "text": "We study 3D semantic segmentation on point clouds."},
            {"id": "S2", "page": 3, "text": "The best model reaches 61.4 mIoU overall."},
        ]
        payload = {
            "summary_zh": "研究 3D 语义分割。",
            "problem": {"text": "跨域掉点", "evidence": [{"snippet": "S1"}]},
            "method": {"text": "自训练", "evidence": [{"snippet": "S1"}]},
            "findings": [{"text": "最好模型达到 61.4 mIoU", "evidence": [{"snippet": "S1"}]}],
        }
        clean = enrich.validate_analysis(payload, snippets)
        quotes = [item["quote"] for item in clean["findings"][0]["evidence"]]
        self.assertTrue(any("61.4" in quote for quote in quotes))


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.root = make_scratch("update")
        write_json(self.root / "data/seed.json", [])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _config(self):
        config = json.loads((ROOT / "config.json").read_text("utf-8"))
        config["arxiv"]["queries"] = [{"id": "only", "query": "all:test", "enabled": True}]
        config["arxiv"]["revision_batch_size"] = 0
        config["arxiv"]["initial_lookback_days"] = 7
        # 这些用例只测 arXiv;显式关掉新来源,保证测试绝不联网。
        config["dblp"]["enabled"] = False
        config["openalex"]["enabled"] = False
        return config

    def _multi_config(self):
        config = json.loads((ROOT / "config.json").read_text("utf-8"))
        config["arxiv"]["queries"] = [{"id": "only", "query": "all:test", "enabled": False}]
        config["arxiv"]["revision_batch_size"] = 0
        config["dblp"] = {
            "enabled": True,
            "request_interval_seconds": 0.5,
            "timeout_seconds": 10,
            "attempts": 1,
            "page_size": 10,
            "max_pages_per_venue_year": 5,
            "lookback_years": 0,
            "recheck_completed_years_days": 30,
            "scope_prefilter": True,
            "venues": [
                {"id": "cvpr", "name": "CVPR", "stream": "conf/cvpr", "enabled": True}
            ],
        }
        config["openalex"] = {
            "enabled": False,
            "request_interval_seconds": 0.1,
            "timeout_seconds": 10,
            "attempts": 1,
            "page_size": 10,
            "max_pages_per_journal": 5,
            "initial_lookback_days": 30,
            "overlap_days": 7,
            "max_window_days": 30,
            "types": ["article", "review"],
            "scope_prefilter": True,
            "journals": [
                {
                    "id": "tpami",
                    "name": "IEEE TPAMI",
                    "source_id": "S199944782",
                    "issn": ["0162-8828"],
                    "enabled": True,
                }
            ],
        }
        return config

    def test_ok_round_moves_cursor(self):
        client = FakeClient([atom_feed([{"aid": "9900.00001"}], total=1)])
        report = run_update(self._config(), root=self.root, client=client, now=NOW)
        self.assertEqual(report["status"], "ok")
        store = read_json(self.root / "data/store.json", {})
        self.assertIn("only", store["queries"])
        self.assertEqual(store["last_full_success"], STAMP)
        self.assertEqual(len(store["records"]), 1)

    def test_failed_round_keeps_cursor_and_full_success(self):
        client = FakeClient([atom_feed([{"aid": "9900.00001"}], total=1)])
        run_update(self._config(), root=self.root, client=client, now=NOW)
        before = read_json(self.root / "data/store.json", {})["queries"]["only"]

        later = NOW + timedelta(days=1)
        report = run_update(self._config(), root=self.root, client=FailingClient(), now=later)
        self.assertEqual(report["status"], "failed")
        store = read_json(self.root / "data/store.json", {})
        self.assertEqual(store["queries"]["only"], before)
        self.assertEqual(store["last_full_success"], STAMP)

    def test_no_enabled_query_is_a_failure_not_empty_success(self):
        config = self._config()
        config["arxiv"]["queries"][0]["enabled"] = False
        report = run_update(config, root=self.root, client=FakeClient([]), now=NOW)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["sources"][0]["source"], "configuration")

    def test_dblp_round_records_year_state_only_on_success(self):
        config = self._multi_config()
        client = FakeDblpClient(
            1,
            [
                [
                    dblp_binding(
                        doi="10.1109/cvpr.2026.00001",
                        title="3D Semantic Segmentation of Point Clouds",
                    )
                ]
            ],
        )
        report = run_update(
            config, root=self.root, dblp_client=client, now=NOW, check_revisions=False
        )
        self.assertEqual(report["status"], "ok")
        outcome = next(item for item in report["sources"] if item["source"] == "dblp:cvpr")
        self.assertEqual((outcome["status"], outcome["matched"], outcome["stored"]), ("ok", 1, 1))

        store = read_json(self.root / "data/store.json", {})
        year_state = store["dblp"]["cvpr"]["years"][str(NOW.year)]
        self.assertEqual(year_state["matched"], 1)
        self.assertEqual(year_state["last_success"], STAMP)
        record = store["records"]["doi:10.1109/cvpr.2026.00001"]
        self.assertEqual(record["origin"], "dblp")
        self.assertEqual(record["venue"], "CVPR")
        self.assertEqual(record["published_precision"], "year")

        later = NOW + timedelta(days=1)
        report = run_update(
            config,
            root=self.root,
            dblp_client=FailingDblpClient(),
            now=later,
            check_revisions=False,
        )
        self.assertEqual(report["status"], "failed")
        store = read_json(self.root / "data/store.json", {})
        self.assertEqual(store["dblp"]["cvpr"]["years"][str(NOW.year)], year_state)

    def test_dblp_recently_completed_previous_year_is_skipped(self):
        config = self._multi_config()
        config["dblp"]["lookback_years"] = 1
        previous_year = str(NOW.year - 1)
        write_json(
            self.root / "data/store.json",
            {
                "dblp": {
                    "cvpr": {
                        "years": {previous_year: {"last_success": STAMP, "matched": 10, "stored": 2}}
                    }
                }
            },
        )
        client = FakeDblpClient(0, [])
        report = run_update(
            config, root=self.root, dblp_client=client, now=NOW, check_revisions=False
        )
        self.assertEqual(report["status"], "ok")
        skipped = [item for item in report["sources"] if item["status"] == "skipped"]
        self.assertEqual([item["year"] for item in skipped], [NOW.year - 1])
        self.assertEqual(len(client.calls), 1)  # 只查了当前年份的 COUNT
        store = read_json(self.root / "data/store.json", {})
        self.assertEqual(
            store["dblp"]["cvpr"]["years"][previous_year]["last_success"], STAMP
        )

    def test_openalex_round_uses_cursor_state_and_prefilter(self):
        config = self._multi_config()
        config["dblp"]["enabled"] = False
        config["openalex"]["enabled"] = True
        client = FakeOpenAlexClient(
            {
                "*": oa_page(
                    [
                        oa_work(wid="W1"),
                        oa_work(
                            wid="W2",
                            title="Database Indexing for Warehouse Systems",
                            doi="https://doi.org/10.1109/tpami.2026.1000002",
                        ),
                    ],
                    2,
                    None,
                )
            }
        )
        report = run_update(
            config, root=self.root, openalex_client=client, now=NOW, check_revisions=False
        )
        self.assertEqual(report["status"], "ok")
        outcome = next(item for item in report["sources"] if item["source"] == "openalex:tpami")
        self.assertEqual(
            (outcome["status"], outcome["matched"], outcome["stored"], outcome["skipped"]),
            ("ok", 2, 1, 1),
        )
        store = read_json(self.root / "data/store.json", {})
        state = store["openalex"]["tpami"]
        self.assertEqual(state["completed_until"], STAMP)
        self.assertTrue(state["filter_hash"])
        self.assertEqual(list(store["records"]), ["doi:10.1109/tpami.2026.1000001"])

        # 第二轮从 completed_until - overlap_days 重建窗口。
        follow = FakeOpenAlexClient({"*": oa_page([], 0, None)})
        report = run_update(
            config, root=self.root, openalex_client=follow, now=NOW, check_revisions=False
        )
        self.assertEqual(report["status"], "ok")
        start = (NOW - timedelta(days=7)).date().isoformat()
        self.assertIn("from_publication_date:" + start, follow.calls[0]["filter"])


if __name__ == "__main__":
    unittest.main()
