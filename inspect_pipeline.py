# -*- coding: utf-8 -*-
"""파이프라인 단계별 투명 점검 (재설계용 작업대).

한 조례의 STEP을 펼쳐서, 각 단계가 무엇을 만들어내는지 본다.
Windows 콘솔(cp949) 인코딩 문제를 피하려 결과를 UTF-8 파일로 쓴다.

실행 (OC 키 필요):
    LAW_OC_KEY=<키> python inspect_pipeline.py 1537339
    LAW_OC_KEY=<키> python inspect_pipeline.py 1537339 <다른MST> --deep
→ inspect_<MST>.txt (UTF-8) 생성
"""
import re
import sys

from gunpolaw import moleg, checks, history
from gunpolaw.extract import (
    extract_citations, group_by_law, normalize_text,
    LAW_CITE_RE, SAME_LAW_RE,
)


def _ctx(text, start, end, w=18):
    a = max(0, start - w)
    b = min(len(text), end + w)
    return "…" + text[a:b].replace("\n", " ") + "…"


def inspect(mst, deep, out):
    """out: 줄을 받는 list.append."""
    out("=" * 70)
    out(f"조례 MST={mst}")
    out("=" * 70)
    body = moleg.get_ordinance_body(mst)
    if "error" in body:
        out("  본문 조회 실패: " + str(body["error"]))
        return
    meta = body["meta"]
    out(f"  조례명 : {meta['name']}")
    out(f"  시행일 : {meta['enforce_date']}   공포일: {meta['promulg_date']}")
    text = normalize_text(body["full_text"])
    out(f"  본문 길이: {len(text)}자, 조문 {len(body['articles'])}개")

    # ---------- STEP 1 ----------
    out("\n" + "-" * 70)
    out("STEP 1  인용 추출 (정규식이 잡은 원문 위치)")
    out("-" * 70)
    out("[LAW_CITE_RE — 「법령명」(+약칭)(+조항)]")
    for m in LAW_CITE_RE.finditer(text):
        out(f"  「{m.group(1)}」 약칭={m.group(2)!r} 조항={m.group(3)!r}")
        out(f"      ↳ {_ctx(text, m.start(), m.end())}")
    out("[SAME_LAW_RE — 같은 법/영/시행규칙 + 조항]")
    for m in SAME_LAW_RE.finditer(text):
        out(f"  같은 {m.group(1)} {m.group(2)}")
        out(f"      ↳ {_ctx(text, m.start(), m.end())}")

    refs = extract_citations(body["full_text"])
    out("\n[extract_citations 결과 — ref 단위]")
    for r in refs:
        out(f"  {r['type']:4} | {r['name']} | clause={r['clause']!r} "
            f"| labels={r['clause_labels']} | src={r['alias_source']}")

    grouped = group_by_law(refs)
    out("\n[group_by_law — 법령 단위 (검증 입력)]")
    for name, g in grouped.items():
        out(f"  [{g['type']}] {name}  →  {g['clause_labels']}")

    # ---------- STEP 2 ----------
    out("\n" + "-" * 70)
    out("STEP 2  법령ID 해결 (resolve_law_id)")
    out("-" * 70)
    law_ids = {}
    for name, g in grouped.items():
        if g["type"] != "법령":
            out(f"  (skip 자치법규) {name}")
            continue
        lid = moleg.resolve_law_id(name)
        law_ids[name] = lid
        out(f"  {name}  →  법령ID={lid}")

    # ---------- STEP 3 ----------
    out("\n" + "-" * 70)
    out("STEP 3  조항별 변경 판정" + ("  [deep=당시본 vs 현행 diff]" if deep else ""))
    out("-" * 70)
    for name, g in grouped.items():
        if g["type"] != "법령":
            continue
        lid = law_ids.get(name)
        if not lid:
            out(f"  {name}: 법령ID 미해결 → 건너뜀")
            continue
        cur = moleg.parse_law_articles(moleg.get_law_body(lid))
        old = None
        if deep:
            vers = history.list_versions(name, lid)
            vsel = history.as_of(vers, meta["enforce_date"])
            if vsel:
                old = history.body_articles_by_mst(vsel["mst"])
                out(f"  [{name}] 당시 시행본: MST={vsel['mst']} "
                    f"(시행 {vsel.get('enforce_date','?')}, 버전 {len(vers)}개 중 선택)")
            else:
                out(f"  [{name}] 당시 시행본 찾지 못함 → 현행 기준 1단계 판정")
        for label in g["clause_labels"]:
            if deep and old is not None:
                f = checks.diff_clause(old, cur, label, meta["enforce_date"], name, lid)
            else:
                f = checks.check_clause(cur, label, meta["enforce_date"], name, lid)
            out(f"  {name} {label}: [{f['severity']}/{f.get('change_type','')}] {f['detail']}")
            if f.get("evidence"):
                ev = re.sub(r"\s+", " ", f["evidence"])[:240]
                out(f"      근거: {ev}")


def main(argv):
    deep = "--deep" in argv
    msts = [a for a in argv if a.isdigit()]
    if not msts:
        print("usage: LAW_OC_KEY=<key> python inspect_pipeline.py <MST> [<MST>...] [--deep]")
        return 1
    for mst in msts:
        lines = []
        try:
            inspect(mst, deep, lines.append)
        except Exception as e:
            import traceback
            lines.append("EXCEPTION: " + repr(e))
            lines.append(traceback.format_exc())
        path = f"inspect_{mst}.txt"
        with open(path, "w", encoding="utf-8") as fp:
            fp.write("\n".join(lines))
        print(f"wrote {path} ({len(lines)} lines)")   # ASCII only
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
