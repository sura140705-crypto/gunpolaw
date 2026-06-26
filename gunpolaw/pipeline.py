# -*- coding: utf-8 -*-
"""오케스트레이션 — 조례 1건 변경 탐지 (1단계 수직 슬라이스).

흐름: 조례 본문 → 인용추출 → (연계로 법령ID 보강) → 법령 현행본문 →
      인용 조항별 변경 판정 → findings.
"API는 채울 때만" — 같은 법령 본문은 1회만 받아 캐시.
"""
from . import moleg
from . import checks
from . import history
from .extract import extract_citations_by_article, group_by_law


def analyze_ordinance(mst, link_index=None, law_cache=None,
                      deep=False, version_cache=None, old_cache=None):
    """단일 조례 분석 -> {ordinance, findings, summary}.

    link_index: {정규화 법령명: 법령ID} (lnkOrg 사전, 선택). 법령ID 보강용.
    law_cache:  {법령ID: 현행 조문dict} 공유 캐시. 법령 본문 1회만 호출.
    deep:       True면 2단계 — 조례 당시 시행본을 받아 현행과 내용 diff로 확정.
    version_cache/old_cache: 2단계 캐시({법령ID:버전목록}, {버전MST:조문dict}).
    """
    body = moleg.get_ordinance_body(mst)
    if "error" in body:
        return {"error": body["error"], "mst": mst}
    meta = body["meta"]
    ord_enforce = meta["enforce_date"]

    grouped = group_by_law(extract_citations_by_article(body["articles"]))
    link_index = link_index or {}
    law_body_cache = law_cache if law_cache is not None else {}
    version_cache = version_cache if version_cache is not None else {}
    old_cache = old_cache if old_cache is not None else {}
    findings = []

    for name, g in grouped.items():
        if g["type"] != "법령":          # 자치법규간 참조는 제외
            continue
        # 이 법령을 인용한 조례 조문(법명only 위치) — 법령단위 finding 의 정비 위치
        law_loc = ", ".join(g.get("law_articles", []))
        law_id = link_index.get(name.replace(" ", "")) or moleg.resolve_law_id(name)
        if not law_id:
            findings.append({
                "law_id": "", "law_name": name, "clause_label": "",
                "category": "status", "severity": "check", "change_type": "법령미해결",
                "detail": "법령ID 미해결 (폐지/제명변경 의심)", "ord_enforce": ord_enforce,
                "clause_enforce": "", "evidence": "", "ord_clause": law_loc})
            continue
        if law_id not in law_body_cache:
            law_body_cache[law_id] = moleg.parse_law_articles(moleg.get_law_body(law_id))
        cur_arts = law_body_cache[law_id]

        old_arts = None
        if deep:
            if law_id not in version_cache:
                version_cache[law_id] = history.list_versions(name, law_id)
            vsel = history.as_of(version_cache[law_id], ord_enforce)
            if vsel:
                if vsel["mst"] not in old_cache:
                    old_cache[vsel["mst"]] = history.body_articles_by_mst(vsel["mst"])
                old_arts = old_cache[vsel["mst"]]

        clause_articles = g.get("clause_articles", {})
        for label in g["clause_labels"]:
            if deep and old_arts is not None:
                f = checks.diff_clause(old_arts, cur_arts, label, ord_enforce, name, law_id)
            else:
                f = checks.check_clause(cur_arts, label, ord_enforce, name, law_id)
            f["ord_clause"] = ", ".join(clause_articles.get(label, []))
            findings.append(f)

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
