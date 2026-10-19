"""人工审核入口:命令行与 Issue 工作流共用这一个函数,只写 data/editorial.json。

用法:
  python scripts/review.py 'arxiv:2610.00855' --state metadata-checked
  python scripts/review.py 'arxiv:2610.00855' --bucket core --tracks DA OV --note "重点跟进"
  python scripts/review.py --issue-body .cache/issue.md      # 由 review.yml 调用
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas.common import load_config  # noqa: E402
from atlas.editorial import ReviewError, apply_review, parse_review_request  # noqa: E402

MESSAGES = {
    "missing-review-marker": "Issue 正文缺少 <!-- observatory-review --> 标记。",
    "missing-paper-id": "Issue 正文缺少 paper_id 行。",
    "unknown-paper-id": "库里没有这条记录,先跑一次 update 或检查编号。",
    "invalid-state": "state 取值不合法。",
    "invalid-bucket": "bucket 取值不合法。",
    "invalid-tracks": "tracks 里有未知研究方向。",
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="apply a human review to editorial.json")
    parser.add_argument("paper_id", nargs="?", default=None)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--config", default=None)
    parser.add_argument("--state", default=None)
    parser.add_argument("--bucket", default=None)
    parser.add_argument("--tracks", nargs="*", default=None)
    parser.add_argument("--public", default=None, choices=["yes", "no"])
    parser.add_argument("--note", default=None)
    parser.add_argument("--issue-body", default=None, help="从文件读 Issue 正文")
    args = parser.parse_args(argv)

    root = Path(args.root)
    config = load_config(args.config or root / "config.json")

    try:
        if args.issue_body:
            body = Path(args.issue_body).read_text("utf-8")
            fields = parse_review_request(body)
            result = apply_review(
                root,
                config,
                fields["paper_id"],
                state=fields.get("state") or None,
                bucket=fields.get("bucket") or None,
                tracks=fields.get("tracks") or None,
                public=fields.get("public") or None,
                note=fields.get("note") or None,
            )
        elif args.paper_id:
            result = apply_review(
                root,
                config,
                args.paper_id,
                state=args.state,
                bucket=args.bucket,
                tracks=args.tracks,
                public=args.public,
                note=args.note,
            )
        else:
            parser.error("需要 paper_id 或 --issue-body")
            return 2
    except ReviewError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": MESSAGES.get(exc.code, str(exc))}, ensure_ascii=False))
        return 1

    print(json.dumps({"ok": True, "editorial": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
