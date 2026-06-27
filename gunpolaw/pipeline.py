# -*- coding: utf-8 -*-
"""오케스트레이션 — 조례 1건 변경 탐지 (1단계 수직 슬라이스).

흐름: 조례 본문 → 인용추출 → (연계로 법령ID 보강) → 법령 현행본문 →
      인용 조항별 변경 판정 → findings.
"API는 채울 때만" — 같은 법령 본문은 1회만 받아 캐시.
"""
import re

from . import moleg
from . import checks
from . import history
from .extract import extract_citations_by_article, group_by_law


class LiveSource:
    """기본(라이브) 본문 출처 — 법제처 OpenAPI. analyze_ordinance의 기본값.

    DB 기반 오프라인 재파싱(reparse._DBSource)이 같은 인터페이스로 갈아끼워진다.
    """
    get_ordinance_body = staticmethod(moleg.get_ordinance_body)
    resolve_law_id = staticmethod(moleg.resolve_law_id)
    get_law_body = staticmethod(moleg.get_law_body)
    list_versions = staticmethod(history.list_versions)
    body_with_xml_by_mst = staticmethod(history.body_with_xml_by_mst)


def _name_key(s):
    """법령명 비교용 정규화 — 공백·가운뎃점류 제거(제명변경만 잡고 표기차는 무시)."""
    return re.sub(r"[\s·ㆍ・]", "", s or "")


def analyze_ordinance(mst, link_index=None, law_cache=None,
                      deep=False, version_cache=None, old_cache=None, src=None,
                      law_name_cache=None):
    """단일 조례 분석 -> {ordinance, findings, summary}.

    link_index: {정규화 법령명: 법령ID} (lnkOrg 사전, 선택). 법령ID 보강용.
    law_cache:  {법령ID: 현행 조문dict} 공유 캐시. 법령 본문 1회만 호출.
    deep:       True면 2단계 — 조례 당시 시행본을 받아 현행과 내용 diff로 확정.
    version_cache/old_cache: 2단계 캐시({법령ID:버전목록}, {버전MST:조문dict}).
    src:        본문 출처(기본 LiveSource=API). reparse는 DB 기반 출처를 주입.
    """
    src = src or LiveSource
    body = src.get_ordinance_body(mst)
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
    law_name_cache = law_name_cache if law_name_cache is not None else {}
    findings = []
    fetched_laws = {}     # 이번 호출에서 새로 받은 법령(캐시 미스) — 배치가 DB 영속
    fetched_versions = {} # 새로 받은 당시 시행본 {버전MST: {law_id, enforce_date, body_xml}}
    resolved_ids = {}     # 법령명 → 해소된 law_id (citations 영속·오프라인 재파싱 재현용)

    for name, g in grouped.items():
        if g["type"] != "법령":          # 자치법규간 참조는 제외
            continue
        # 이 법령을 인용한 조례 조문(법명only 위치) — 법령단위 finding 의 정비 위치
        law_loc = ", ".join(g.get("law_articles", []))
        law_seq = g.get("law_seq", 0)
        naked_any = g.get("naked_any", False)
        naked_only = g.get("naked_only", False)
        law_id = link_index.get(name.replace(" ", "")) or src.resolve_law_id(name)
        if law_id:
            resolved_ids[name] = law_id   # 조문 없는 법명-only 인용도 매핑 보존(재파싱 재현)
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
            law_xml = src.get_law_body(law_id)
            law_body_cache[law_id] = moleg.parse_law_articles(law_xml)
            law_name_cache[law_id] = moleg.law_name_of(law_xml)
            # 원본 XML도 함께 넘겨 영속 → 파싱 규칙이 바뀌어도 재수집 없이 오프라인 재파싱
            fetched_laws[law_id] = {"name": name, "articles": law_body_cache[law_id],
                                    "body_xml": law_xml or ""}
        cur_arts = law_body_cache[law_id]

        # 제명변경: 인용 법령명 ≠ 현행 법령명(개칭/통폐합). 법령 자체가 바뀌었으면 조문번호·
        # 내용이 전면 재편됐을 수 있어 조문별 비교는 오히려 오해를 부른다 → 조문별 판정은
        # 건너뛰고 '인용 법령명 현행화 + 관련 조문 전면 검토' 1건으로 대체(조례 조문별).
        cur_law_name = law_name_cache.get(law_id, "")
        renamed_to = (cur_law_name if cur_law_name
                      and _name_key(cur_law_name) != _name_key(name) else "")
        if renamed_to:
            arts_cited = sorted({o["ord_article"] for o in g.get("occurrences", [])
                                 if o["ord_article"]} | set(g.get("law_articles", [])),
                                key=lambda a: int(re.search(r"\d+", a).group()) if a and re.search(r"\d+", a) else 0)
            for oa in (arts_cited or [law_loc]):
                findings.append({
                    "law_id": law_id, "law_name": name, "clause_label": "",
                    "clause_detail": "", "category": "status", "severity": "review",
                    "change_type": "제명변경", "renamed_to": renamed_to,
                    "detail": f"인용한 「{name}」이(가) 현행 「{renamed_to}」(으)로 제명변경"
                              f"(개칭·통폐합)됨 — 법령 자체가 바뀌었으니 인용 법령명을 현행화하고 "
                              f"관련 조문을 전면 검토. (조문번호·내용이 재편됐을 수 있어 개별 조문 "
                              f"비교는 생략) [개칭 아니면 인용 오기 확인]",
                    "ord_enforce": ord_enforce, "old_enforce": "", "clause_enforce": "",
                    "evidence": "", "ord_clause": oa, "ord_seq": law_seq,
                    "cite_naked": 0})   # 제명변경은 명칭 정정 이슈 — 꺽쇠와 무관
            continue   # 이 법령은 조문별 판정 생략(전면 검토로 대체)

        old_arts = None
        old_enforce = ""
        if deep:
            if law_id not in version_cache:
                version_cache[law_id] = src.list_versions(name, law_id)
            vsel = history.as_of(version_cache[law_id], ord_enforce)
            if vsel:
                old_enforce = vsel["enforce_date"]      # 당시 시행본 법령 일자
                if vsel["mst"] not in old_cache:
                    arts, vxml = src.body_with_xml_by_mst(vsel["mst"])
                    old_cache[vsel["mst"]] = arts
                    # 원본 XML 영속 → 다음 파서 변경 시 deep 근거도 재수집 없이 재파싱
                    fetched_versions[vsel["mst"]] = {
                        "law_id": law_id, "enforce_date": old_enforce, "body_xml": vxml}
                old_arts = old_cache[vsel["mst"]]

        # 조례 조문별 인용 1건씩 판정(조례 기준) — 같은 상위법 조라도 인용한 조례 조문·
        # 호/목이 다르면 별개 finding. 인용한 그 호/목만 비교한다.
        for occ in g.get("occurrences", []):
            label = occ["label"]
            subspec = (occ["hang"], occ["ho"], occ["mok"])
            if deep and old_arts is not None:
                f = checks.diff_clause(old_arts, cur_arts, label, ord_enforce,
                                       name, law_id, old_enforce, subspec=subspec)
            else:
                f = checks.check_clause(cur_arts, label, ord_enforce, name, law_id)
            f["ord_clause"] = occ["ord_article"]
            f["ord_seq"] = occ["ord_seq"]
            # 꺽쇠 권고는 '전체 법령명을 꺽쇠 없이' 쓴 그 인용에만(약칭 '법'·'같은 법'은 정상)
            f["cite_naked"] = 1 if occ.get("naked") else 0
            findings.append(f)

    # 인용 refs에 해소된 law_id를 새겨 영속 — 조문 없는 법명-only 인용까지 매핑이 남아
    # 오프라인 재파싱(reparse)이 라이브 해소 결과를 그대로 재현한다.
    for r in refs:
        r["law_id"] = resolved_ids.get(r.get("name", ""), "")

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
