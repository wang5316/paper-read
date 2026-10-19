"""离线单测:不联网、不需要密钥,用合成 Atom 与假客户端覆盖关键判定。

跑法:python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas import arxiv_source, enrich, site, triage  # noqa: E402
from atlas.common import (  # noqa: E402
    ConfigError,
    identity_keys,
    load_config,
    read_json,
    utcnow,
    write_json,
)
from atlas.editorial import REVIEW_MARKER, apply_review, parse_review_request  # noqa: E402
from atlas.pipeline import EMPTY, merge_paper, run_update  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
