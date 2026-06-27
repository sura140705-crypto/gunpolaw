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

    refs = extract_citations_by_article(body["articles"])
    grouped = group_by_law(refs)
    link_index = link_index or {}
    law_body_cache = law_cache if law_cache is not None else {}
    version_cache = version_cache if version_cache is not None else {}
    old_cache = old_cache if old_cache is not None else {}
    findings = []
    fetched_laws = {}     # 이번 호출에서 새로 받은 법령(캐시 미스) — 배치가 DB 영속
    fetched_versions = {} # 새로 받은 당시 시행본 {버전MST: {law_id, enforce_date, body_xml}}

    for name, g in grouped.items():
        if g["type"] != "법령":          # 자치법규간 참조는 제외
            continue
        # 이 법령을 인용한 조례 조문(법명only 위치) — 법령단위 finding 의 정비 위치
        law_loc = ", ".join(g.get("law_articles", []))
        law_seq = g.get("law_seq", 0)
        naked_any = g.get("naked_any", False)
        naked_only = g.get("naked_only", False)
        law_id = link_index.get(name.replace(" ", "")) or moleg.resolve_law_id(name)
        if not law_id:
            # 맨몸으로만 잡힌 미해소 법명은 오탐 가능성이 높아 침묵 드롭(노이즈 억제).
            if naked_only:
                continue
            findings.append({
                "law_id": "", "law_name": name, "clause_label": "",
                "category": "status", "severity": "check", "change_type": "법령미해결",
                "detail": "법령ID 미해결 (폐지/제명변경 의심)", "ord_enforce": ord_enforce,
                "clause_enforce": "", "old_enforce": "", "evidence": "",
                "ord_clause": law_loc, "ord_seq": law_seq, "cite_naked": 1 if naked_any else 0})
            continue
        if law_id not in law_body_cache:
            law_xml = moleg.get_law_body(law_id)
            law_body_cache[law_id] = moleg.parse_law_articles(law_xml)
            # 원본 XML도 함께 넘겨 영속 → 파싱 규칙이 바뀌어도 재수집 없이 오프라인 재파싱
            fetched_laws[law_id] = {"name": name, "articles": law_body_cache[law_id],
                                    "body_xml": law_xml or ""}
        cur_arts = law_body_cache[law_id]

        old_arts = None
        old_enforce = ""
        if deep:
            if law_id not in version_cache:
                version_cache[law_id] = history.list_versions(name, law_id)
            vsel = history.as_of(version_cache[law_id], ord_enforce)
            if vsel:
                old_enforce = vsel["enforce_date"]      # 당시 시행본 법령 일자
                if vsel["mst"] not in old_cache:
                    arts, vxml = history.body_with_xml_by_mst(vsel["mst"])
                    old_cache[vsel["mst"]] = arts
                    # 원본 XML 영속 → 다음 파서 변경 시 deep 근거도 재수집 없이 재파싱
                    fetched_versions[vsel["mst"]] = {
                        "law_id": law_id, "enforce_date": old_enforce, "body_xml": vxml}
                old_arts = old_cache[vsel["mst"]]

        clause_articles = g.get("clause_articles", {})
        clause_seq = g.get("clause_seq", {})
        for label in g["clause_labels"]:
            if deep and old_arts is not None:
                f = checks.diff_clause(old_arts, cur_arts, label, ord_enforce,
                                       name, law_id, old_enforce)
            else:
                f = checks.check_clause(cur_arts, label, ord_enforce, name, law_id)
            f["ord_clause"] = ", ".join(clause_articles.get(label, []))
            f["ord_seq"] = clause_seq.get(label, 0)
            f["cite_naked"] = 1 if naked_any else 0
            findings.append(f)

    return {
        "ordinance": meta,
        "body_xml": body.get("xml", ""),
        "findings": findings,
        "fetched_laws": fetched_laws,
        "fetched_versions": fetched_versions,
        "citations": refs,
        "summary": checks.summarize(findings),
    }


def build_link_index(org):
    """lnkOrg -> {정규화 법령명: 법령ID} (인용추출 보강용)."""
    idx = {}
    for row in moleg.get_org_links(org):
        if row["law_name"] and row["law_id"]:
            idx[row["law_name"].replace(" ", "")] = row["law_id"]
    return idx
