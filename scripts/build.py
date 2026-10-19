"""生成静态站点到 dist/(index.html + data.js + feed.xml + RIS + digest)。

不联网、不读密钥;抓取失败时也能用旧库构建,这是「失败不阻断发布」的落点。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas.common import load_config  # noqa: E402
from atlas.site import build  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="build the static site into dist/")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--out", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--no-imports", action="store_true", help="不发布既有题录(seed)")
    parser.add_argument("--preview", action="store_true", help="标记为本地预览")
    parser.add_argument("--site-url", default=None, help="覆盖 config.site_url(用于 Pages 域名)")
    args = parser.parse_args(argv)

    root = Path(args.root)
    config = load_config(args.config or root / "config.json")
    if args.site_url:
        config["site_url"] = args.site_url

    summary = build(
        config,
        root=root,
        out=Path(args.out) if args.out else root / "dist",
        include_imports=not args.no_imports,
        preview=args.preview,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
