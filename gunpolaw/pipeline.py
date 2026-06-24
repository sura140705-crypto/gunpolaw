# -*- coding: utf-8 -*-
"""오케스트레이션 — 조례 1건 변경 탐지 (1단계 수직 슬라이스).

흐름: 조례 본문 → 인용추출 → (연계로 법령ID 보강) → 법령 현행본문 →
      인용 조항별 변경 판정 → findings.
"API는 채울 때만" — 같은 법령 본문은 1회만 받아 캐시.
"""
from . import moleg
from . import checks
from .extract import extract_citations, group_by_law


def analyze_ordinance(mst, link_index=None, law_cache=None):
    """단일 조례 분석 -> {ordinance, findings, summary}.

    link_index: {정규화 법령명: 법령ID} (lnkOrg 사전, 선택). 법령ID 보강용.
    law_cache:  {법령ID: 조문dict} 공유 캐시(선택). 배치에서 법령 본문 1회만 호출.
    """
    body = moleg.get_ordinance_body(mst)
    if "error" in body:
        return {"error": body["error"], "mst": mst}
    meta = body["meta"]
    ord_enforce = meta["enforce_date"]

    grouped = group_by_law(extract_citations(body["full_text"]))
    link_index = link_index or {}
    law_body_cache = law_cache if law_cache is not None else {}
    findings = []

    for name, g in grouped.items():
        if g["type"] != "법령":          # 자치법규간 참조는 1단계 제외
            continue
        law_id = link_index.get(name.replace(" ", "")) or moleg.resolve_law_id(name)
        if not law_id:
            findings.append({
                "law_id": "", "law_name": name, "clause_label": "",
                "category": "status", "severity": "check",
                "detail": "법령ID 미해결 (폐지/제명변경 의심)", "ord_enforce": ord_enforce,
                "clause_enforce": "", "evidence": ""})
            continue
        if law_id not in law_body_cache:
            law_body_cache[law_id] = moleg.parse_law_articles(moleg.get_law_body(law_id))
        arts = law_body_cache[law_id]
        for label in g["clause_labels"]:
            findings.append(
                checks.check_clause(arts, label, ord_enforce, name, law_id))

    return {
        "ordinance": meta,
        "findings": findings,
        "summary": checks.summarize(findings),
    }


def build_link_index(org):
    """lnkOrg -> {정규화 법령명: 법령ID} (인용추출 보강용)."""
    idx = {}
    for row in moleg.get_org_links(org):
        if row["law_name"] and row["law_id"]:
            idx[row["law_name"].replace(" ", "")] = row["law_id"]
    return idx
