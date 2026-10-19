"""跑一轮自动发现:抓 arXiv / DBLP / OpenAlex -> 合并去重 -> 写 data/store.json。

退出码:0 = ok/partial(部分来源失败只在站点状态栏提示),2 = 整轮失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas.common import load_config, read_json  # noqa: E402
from atlas.pipeline import run_update  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="run one discovery round")
    parser.add_argument("--root", default=str(ROOT), help="仓库根目录")
    parser.add_argument("--days", type=float, default=None, help="一次性回补最近 N 天(按窗口切片推进)")
    parser.add_argument("--no-revisions", action="store_true", help="跳过已知编号的版本巡检")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args(argv)

    root = Path(args.root)
    config = load_config(args.config or root / "config.json")
    report = run_update(
        config,
        root=root,
        days=args.days,
        check_revisions=not args.no_revisions,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))

    store = read_json(root / "data/store.json", {}) or {}
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data/last-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8", newline="\n"
    )
    return 0 if report.get("status") in ("ok", "partial") else 2


if __name__ == "__main__":
    raise SystemExit(main())
