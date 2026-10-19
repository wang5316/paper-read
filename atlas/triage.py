"""题录级自动分类:纯布尔、可解释、保守。

和上游最大的不同:词表来自 config.classify,而不是硬编码在代码里。
这样换研究方向只改 config,专题(topic)引用的标签也一定存在 ——
上游项目就是栽在「专题词表 ≠ 分类器输出词表」上,导致部分专题永远为空。
"""

from __future__ import annotations

import re

NOTE = (
    "自动关键词初筛;标签可能来自背景或对比描述,"
    "不代表已核实任务设置、实验条件或论文贡献。"
)

BUCKET_RANK = {"excluded": 0, "related": 1, "core": 2}


def _compile(patterns):
    return [re.compile(p, re.I) for p in patterns or [] if p]


def _first_hit(text, patterns):
    for pattern in patterns:
        hit = pattern.search(text)
        if hit:
            return hit
    return None


def _quote(text, hit, before=65, after=100):
    return " ".join(text[max(0, hit.start() - before) : hit.end() + after].split())


def classify(title, abstract, classify_cfg) -> dict:
    """返回 {bucket, tracks[], task, methods[], basis, certainty, note, evidence[]}。"""
    title = str(title or "")
    abstract = str(abstract or "")
    text = title + "\n" + abstract

    scope_cfg = classify_cfg["scope"]
    task_cfg = classify_cfg["task"]
    scope_patterns = _compile(scope_cfg["patterns"])
    primary_patterns = _compile(task_cfg["primary"]["patterns"])
    adjacent_patterns = _compile(task_cfg.get("adjacent", {}).get("patterns"))
    any_patterns = _compile(task_cfg.get("any", {}).get("patterns"))

    evidence = []
    scope_hit = _first_hit(text, scope_patterns)
    scope_in_title = bool(_first_hit(title, scope_patterns))
    primary_hit = _first_hit(text, primary_patterns)
    primary_in_title = bool(_first_hit(title, primary_patterns))
    adjacent_hit = _first_hit(text, adjacent_patterns)
    adjacent_in_title = bool(_first_hit(title, adjacent_patterns))
    any_hit = _first_hit(text, any_patterns)

    if primary_hit:
        task = task_cfg["primary"]["id"]
    elif adjacent_hit:
        task = task_cfg["adjacent"]["id"]
    elif any_hit:
        task = task_cfg.get("any", {}).get("id", "unspecified")
    else:
        task = "other"

    methods, tracks = [], []
    for method in classify_cfg.get("methods") or []:
        hit = _first_hit(text, _compile(method.get("patterns")))
        if hit:
            methods.append(method["id"])
            evidence.append({"label": method["id"], "quote": _quote(text, hit)})
    for track in classify_cfg.get("tracks") or []:
        hit = _first_hit(text, _compile(track.get("patterns")))
        if hit:
            tracks.append(track["id"])
            evidence.append({"label": track["id"], "quote": _quote(text, hit)})

    # 弱线索降级:track 声明了 requires(更强的证据)时,没命中就不算该 track。
    # 例:「open-set / open-world」只是线索,不等于 open-vocabulary。
    for track in classify_cfg.get("tracks") or []:
        requires = track.get("requires")
        if track["id"] not in tracks or not requires:
            continue
        if _first_hit(text, _compile([requires])):
            continue
        tracks.remove(track["id"])
        weak_hit = _first_hit(text, _compile(track.get("patterns")))
        evidence.append(
            {
                "label": track["id"] + "-weak-signal",
                "quote": (
                    _quote(text, weak_hit)
                    if weak_hit
                    else f"仅有弱线索,未命中必需模式 {requires!r}"
                ),
            }
        )

    scope_ok = scope_hit is not None
    if scope_ok and task == task_cfg["primary"]["id"] and tracks:
        bucket = "core"
    elif scope_ok and (any_hit or tracks):
        bucket = "related"
    else:
        bucket = "excluded"

    clear = bool(scope_in_title and (primary_in_title or adjacent_in_title) and tracks)
    if not clear and adjacent_in_title:
        clear = False
    return {
        "bucket": bucket,
        "tracks": tracks,
        "task": task,
        "methods": methods,
        "basis": "title+abstract" if abstract else "title-only",
        "certainty": "clear-keywords" if clear else "needs-review",
        "note": NOTE,
        "evidence": evidence[:12],
    }


def merge_classification(old, new, imported=False) -> dict:
    """人工标签不被自动分类降级:tracks 取并集,bucket/task 只升不降。"""
    old = old or {}
    merged = dict(new or {})
    tracks = list(new.get("tracks") or [])
    for track in old.get("tracks") or []:
        if track not in tracks:
            tracks.append(track)
    merged["tracks"] = tracks

    old_bucket, new_bucket = old.get("bucket"), merged.get("bucket")
    if imported and BUCKET_RANK.get(old_bucket, -1) >= BUCKET_RANK.get(new_bucket, -1):
        merged["bucket"] = old_bucket

    good_tasks = {"semantic", "adjacent"}
    if old.get("task") in good_tasks and merged.get("task") not in good_tasks:
        merged["task"] = old["task"]

    if old.get("note"):
        merged["note"] = old["note"] if not new.get("note") else new["note"]
    return merged
