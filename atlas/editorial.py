"""人工覆盖层:唯一写 data/editorial.json 的入口。

抓取链路只读这一层,绝不回写 —— 这样重跑流水线不会覆盖人工判断。
reviewed_version 把「已核对」绑定到具体版本:论文出了新版,状态自动退化为 review-stale。
"""

from __future__ import annotations

import re
from pathlib import Path

from .common import ROOT, iso, parse_time, read_json, utcnow, write_json

REVIEW_STATES = (
    "candidate",
    "imported-unreviewed",
    "selected",
    "metadata-checked",
    "abstract-checked",
    "full-text-read",
    "rejected",
)
REVIEWED_STATES = {"metadata-checked", "abstract-checked", "full-text-read"}
BUCKETS = ("core", "related", "excluded")
REVIEW_MARKER = "<!-- observatory-review -->"

_FIELD = re.compile(r"^(paper_id|state|bucket|tracks|public|note)\s*[:=]\s*(.*)$", re.I)


class ReviewError(ValueError):
    """审核请求不合法;code 会被 CLI / Issue 工作流翻成中文提示。"""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


def load(root=ROOT) -> dict:
    return read_json(Path(root) / "data/editorial.json", {}) or {}


def _current_version(paper_id, store, seeds):
    record = (store.get("records") or {}).get(paper_id)
    if record:
        return record.get("version", 0)
    for seed in seeds or []:
        if seed.get("id") == paper_id:
            return seed.get("version", 0)
    return 0


def apply_review(
    root,
    config,
    paper_id,
    state=None,
    bucket=None,
    tracks=None,
    public=None,
    note=None,
    now=None,
) -> dict:
    """写入一条人工覆盖。只改传入的字段,其余保持原样。"""
    root = Path(root)
    paper_id = str(paper_id or "").strip()
    if not paper_id:
        raise ReviewError("missing-paper-id", "缺少 paper_id")

    store = read_json(root / "data/store.json", {}) or {}
    seeds = read_json(root / "data/seed.json", []) or []
    known = paper_id in (store.get("records") or {}) or any(s.get("id") == paper_id for s in seeds)
    if not known:
        raise ReviewError("unknown-paper-id", f"库里没有这条记录:{paper_id}")

    allowed_tracks = {str(item["id"]) for item in config["classify"].get("tracks") or []}
    entry = load(root).get(paper_id, {})

    if state is not None:
        state = str(state).strip()
        if state not in REVIEW_STATES:
            raise ReviewError("invalid-state", f"不支持的 state:{state}")
        entry["state"] = state
        if state in REVIEWED_STATES:
            entry["reviewed_version"] = _current_version(paper_id, store, seeds)
        else:
            entry.pop("reviewed_version", None)

    if bucket is not None:
        bucket = str(bucket).strip()
        if bucket not in BUCKETS:
            raise ReviewError("invalid-bucket", f"不支持的 bucket:{bucket}")
        entry["bucket"] = bucket

    if tracks is not None:
        if isinstance(tracks, str):
            tracks = tracks.replace(",", " ").split()
        wanted = [str(item) for item in tracks if str(item)]
        unknown = [item for item in wanted if item not in allowed_tracks]
        if unknown:
            raise ReviewError("invalid-tracks", f"未知方向:{', '.join(unknown)}")
        entry["tracks"] = wanted

    if public is not None:
        if isinstance(public, str):
            public = public.strip().lower() in ("1", "yes", "y", "true", "是", "公开")
        entry["public"] = bool(public)

    if note is not None:
        entry["public_note"] = str(note).strip()

    entry["edited_at"] = iso(now or utcnow())
    document = load(root)
    document[paper_id] = entry
    write_json(root / "data/editorial.json", document)
    return {paper_id: entry}


def parse_review_request(body: str) -> dict:
    """解析 Issue 正文里的 `key: value` 行;必须有标记注释。"""
    text = str(body or "")
    if REVIEW_MARKER not in text:
        raise ReviewError("missing-review-marker", f"正文缺少标记 {REVIEW_MARKER}")
    fields = {}
    for line in text.splitlines():
        hit = _FIELD.match(line.strip())
        if hit:
            key, value = hit.group(1).lower(), hit.group(2).strip()
            if key == "note":
                fields.setdefault("note", "")
                fields["note"] = (fields["note"] + " " + value).strip()
            else:
                fields[key] = value
    if not fields.get("paper_id"):
        raise ReviewError("missing-paper-id", "正文缺少 paper_id")
    return fields
