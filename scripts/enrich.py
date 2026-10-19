"""可选的 AI 解读:给 store 里的记录生成带引用的中文拆解。没有 key 就整层跳过。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas.common import load_config  # noqa: E402
from atlas.enrich import run_enrichment  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="generate AI reading notes (needs LLM_API_KEY)")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--config", default=None)
    parser.add_argument("--limit", type=int, default=None, help="本轮最多处理几篇")
    args = parser.parse_args(argv)

    root = Path(args.root)
    config = load_config(args.config or root / "config.json")
    report = run_enrichment(config, root=root, limit=args.limit)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    # disabled / not-configured 都不算失败:这是设计好的降级路径。
    return 0 if report.get("status") in ("ok", "partial", "disabled", "not-configured") else 2


if __name__ == "__main__":
    raise SystemExit(main())
