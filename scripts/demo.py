"""离线演示:不需要 arXiv、不需要任何密钥,生成一份假数据并构建站点。

用途是把「合并状态机 -> 分类 -> 出站 -> 前端」整条链在自己机器上跑通,
顺便给你一个能立刻打开的页面看版式。演示数据全部是编的,别当真。

注意:演示数据写在 `.demo/`(已 gitignore),不会污染你真正的 data/。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas.common import load_config, utcnow, write_json  # noqa: E402
from atlas.pipeline import EMPTY, merge_paper  # noqa: E402
from atlas.site import build  # noqa: E402


def _paper(aid, title, abstract, authors, days_ago, version=1, venue="arXiv", doi=""):
    now = utcnow()
    published = (now - timedelta(days=days_ago)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "id": "arxiv:" + aid,
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "published": published,
        "updated": published,
        "version": version,
        "identifiers": {"arxiv": [aid], "doi": [doi] if doi else []},
        "url": "https://arxiv.org/abs/" + aid,
        "pdf": "https://arxiv.org/pdf/" + aid,
        "venue": venue,
        "comment": "",
        "categories": ["cs.CV"],
    }


DEMO_PAPERS = [
    _paper(
        "9900.00001",
        "[演示数据] Cross-Domain Semantic Segmentation of LiDAR Point Clouds via Pseudo-Label Self-Training",
        "Unsupervised domain adaptation for 3D semantic segmentation of LiDAR point clouds remains difficult "
        "because class distributions shift across sensors. We propose a self-training framework with pseudo-label "
        "class balancing that improves mIoU from 52.1 to 61.4 on the target domain, using a teacher-student "
        "scheme and prototype alignment across domains.",
        ["Demo Author A", "Demo Author B"],
        3,
    ),
    _paper(
        "9900.00002",
        "[演示数据] Domain Generalization for 3D Semantic Segmentation with Geometry-Aware Augmentation",
        "We study domain generalization for 3D semantic segmentation under unseen domains. A geometry-aware "
        "augmentation strategy together with contrastive feature alignment is introduced, reaching 48.7 mIoU on "
        "three unseen datasets without any target data.",
        ["Demo Author C"],
        5,
    ),
    _paper(
        "9900.00003",
        "[演示数据] Open-Vocabulary 3D Scene Understanding with Vision-Language Prototypes",
        "Open-vocabulary 3D segmentation requires recognizing categories beyond the annotated label set. We "
        "distill CLIP features into a point-cloud backbone and build class-agnostic prototypes, improving novel "
        "class IoU by 6.2 points on ScanNet-style benchmarks.",
        ["Demo Author D", "Demo Author E", "Demo Author F"],
        8,
    ),
    _paper(
        "9900.00004",
        "[演示数据] Continual Test-Time Adaptation for Point Cloud Semantic Segmentation",
        "Test-time adaptation for 3D semantic segmentation must handle continually shifting domains. We update "
        "normalization statistics online with an uncertainty-aware selection rule, reducing error by 18 percent "
        "over the source model across five corruption types.",
        ["Demo Author G"],
        11,
        version=2,
        doi="10.48550/arxiv.9900.00004",
    ),
    _paper(
        "9900.00005",
        "[演示数据] Instance Segmentation of Indoor Point Clouds with Sparse Voxel Queries",
        "We present a sparse voxel query formulation for instance segmentation of indoor 3D scans, removing the "
        "need for dense clustering post-processing and running at 12 frames per second.",
        ["Demo Author H"],
        15,
    ),
    _paper(
        "9900.00006",
        "[演示数据] 2D Image Domain Adaptation for Semantic Segmentation (should be excluded)",
        "This image-only study performs unsupervised domain adaptation for semantic segmentation on natural "
        "photographs, using style-transfer augmentation and adversarial feature alignment. It has no novel "
        "modality and is expected to be filtered out by the scope rule.",
        ["Demo Author I"],
        18,
    ),
]

DEMO_SEEDS = [
    {
        "id": "W0001",
        "title": "[演示数据] A Survey of Domain Adaptation for 3D Semantic Segmentation",
        "authors": ["Demo Author J"],
        "published": "2024-06-01",
        "venue": "Demo Journal",
        "url": "https://arxiv.org/abs/9900.00010",
        "identifiers": {"arxiv": ["9900.00010"], "doi": ["10.1000/demo.0001"]},
        "abstract": "A curated survey kept in the seed library to demonstrate how existing references and "
        "newly discovered papers sit side by side.",
        "abstract_source": "openalex",
        "imported": True,
        "origin": "curated-seed",
        "review_status": "imported-unreviewed",
        "full_text_read": False,
        "classification": {"bucket": "core", "tracks": ["DA"], "task": "semantic", "methods": ["alignment"]},
    },
    {
        "id": "W0002",
        "title": "[演示数据] Benchmarking Sensor Shift in Outdoor Point Cloud Segmentation",
        "authors": ["Demo Author K"],
        "published": "2023-11-20",
        "venue": "arXiv",
        "url": "https://arxiv.org/abs/9900.00011",
        "identifiers": {"arxiv": [], "doi": []},
        "abstract": "A benchmark entry with no arXiv id, showing how a curated record can live without one.",
        "imported": True,
        "origin": "curated-seed",
        "review_status": "selected",
        "full_text_read": False,
        "classification": {"bucket": "related", "tracks": [], "task": "semantic", "methods": []},
    },
]

QUOTE_MAIN = (
    "We propose a self-training framework with pseudo-label class balancing that improves "
    "mIoU from 52.1 to 61.4 on the target domain"
)

DEMO_DRAFT = {
    "summary_zh": "针对 LiDAR 点云跨传感器场景,用教师-学生自训练加类别平衡的伪标签,把目标域 mIoU 从 52.1 提到 61.4。",
    "problem": {
        "text": "不同传感器采集的点云存在类别分布偏移,直接迁移会在目标域掉点。",
        "evidence": [
            {
                "page": 1,
                "quote": "Unsupervised domain adaptation for 3D semantic segmentation of LiDAR point clouds "
                "remains difficult because class distributions shift across sensors.",
            }
        ],
    },
    "method": {"text": "自训练框架 + 伪标签类别平衡 + 跨域原型对齐。", "evidence": [{"page": 1, "quote": QUOTE_MAIN}]},
    "findings": [{"text": "目标域 mIoU 由 52.1 提升到 61.4。", "evidence": [{"page": 1, "quote": QUOTE_MAIN}]}],
    "open_problems": [
        {
            "text": "演示数据,没有真实的局限段落。",
            "kind": "limitation",
            "evidence": [{"page": 1, "quote": "Demo record; no real limitations section is available."}],
        }
    ],
    "insights": [],
    "role": {
        "setting": "无监督域适应",
        "supervision": "源域有标签,目标域无标签",
        "datasets": "演示数据",
        "evaluation": "mIoU",
    },
    "evidence": [],
    "dropped": [],
    "model": "demo-model",
    "prompt_version": "obs-reading-v1",
    "scope": "pdf-evidence",
    "generated_at": "2026-01-01T00:00:00Z",
    "human_approved": False,
    "stale": False,
    "source_hash": "demo",
    "input_pages": [1],
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="build a demo site from synthetic data (offline)")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--demo-root", default=None, help="演示数据目录(默认 <root>/.demo)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    root = Path(args.root)
    config = load_config(root / "config.json")
    demo_root = Path(args.demo_root) if args.demo_root else root / ".demo"

    # web/ 是静态资源,直接复用;data/ 用演示数据,和真库彻底分开。
    shutil.rmtree(demo_root, ignore_errors=True)
    (demo_root / "data").mkdir(parents=True, exist_ok=True)
    shutil.copytree(root / "web", demo_root / "web")

    now = utcnow()
    timestamp = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    seeds = json.loads(json.dumps(DEMO_SEEDS))
    store = json.loads(json.dumps(EMPTY))
    for paper in DEMO_PAPERS:
        merge_paper(store, seeds, paper, timestamp, config)

    first = store["records"].get("arxiv:9900.00001")
    if first:
        draft = json.loads(json.dumps(DEMO_DRAFT))
        draft["generated_at"] = timestamp
        first["ai_draft"] = draft

    store["queries"] = {
        query["id"]: {"query_hash": "demo", "completed_until": timestamp, "last_success": timestamp}
        for query in config["arxiv"]["queries"]
    }
    report = {
        "at": timestamp,
        "status": "ok",
        "counts": {"new": len(DEMO_PAPERS), "revised": 0, "existing": 0, "metadata": 0, "unchanged": 0, "conflict": 0},
        "sources": [
            {"source": query["id"], "status": "ok", "matched": 2, "backlog_days": 0}
            for query in config["arxiv"]["queries"]
        ],
        "note": "演示数据 · 未联网抓取。",
    }
    store["last_run"] = report
    store["last_full_success"] = timestamp
    store["runs"] = [report]

    write_json(demo_root / "data/seed.json", seeds)
    write_json(demo_root / "data/store.json", store)
    write_json(
        demo_root / "data/editorial.json",
        {"arxiv:9900.00002": {"state": "selected", "bucket": "core", "edited_at": timestamp}},
    )

    summary = build(
        config,
        root=demo_root,
        out=Path(args.out) if args.out else root / "dist",
        preview=True,
        now=now,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(
        "\n打开 dist/index.html 预览;若浏览器拦截本地脚本,用 python -m http.server -d dist 8000",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
