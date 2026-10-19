"""把最近几轮状态打印成 Markdown(直接拼进 GitHub Actions Step Summary)。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas.common import read_json  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="print a status summary")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--json", action="store_true", help="输出原始 JSON 而不是 Markdown")
    args = parser.parse_args(argv)

    root = Path(args.root)
    store = read_json(root / "data/store.json", {}) or {}
    seeds = read_json(root / "data/seed.json", []) or []
    report = store.get("last_run") or {}

    if args.json:
        print(
            json.dumps(
                {
                    "status": report.get("status", "not-run"),
                    "at": report.get("at"),
                    "records": len(store.get("records") or {}),
                    "seeds": len(seeds),
                    "last_full_success": store.get("last_full_success"),
                    "llm_last_run": store.get("llm_last_run"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("## 文献观察站 · 本轮抓取状态\n")
    print(f"- 整轮状态:`{report.get('status', 'not-run')}`")
    print(f"- 时间:`{report.get('at') or '-'}`")
    print(f"- 上次完整成功:`{store.get('last_full_success') or '-'}`")
    counts = report.get("counts") or {}
    if counts:
        print("- 计数:" + " / ".join(f"{key} {value}" for key, value in counts.items() if value))
    print(f"- 库内记录:{len(store.get('records') or {})} 条;既有题录:{len(seeds)} 条\n")

    print("| 来源 | 状态 | 细节 |\n|---|---|---|")
    for source in report.get("sources") or []:
        status = source.get("status")
        if status == "failed":
            detail = source.get("error") or "failed"
        elif status == "skipped":
            detail = source.get("reason") or "skipped"
        else:
            bits = []
            if source.get("year"):
                bits.append(str(source["year"]))
            bits.append(f"matched {source.get('matched', 0)}")
            if "stored" in source:
                bits.append(f"stored {source.get('stored', 0)}")
            if source.get("skipped"):
                bits.append(f"prefiltered {source.get('skipped')}")
            detail = ";".join(bits)
        if source.get("backlog_days"):
            detail += f";还有 {source['backlog_days']} 天待回查"
        print(f"| `{source.get('source')}` | {status} | {detail} |")

    llm = store.get("llm_last_run")
    if llm:
        print(
            f"\n- AI 解读:`{llm.get('status')}` · 尝试 {llm.get('attempted', 0)} ·"
            f" 成功 {llm.get('succeeded', 0)} · 今日预算 {llm.get('budget_used_today')}/{llm.get('budget_cap')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
