# -*- coding: utf-8 -*-
"""법령 연혁(시행일자별) 조회 — 2단계 내용 비교용.

조례 제정 당시 시행본을 받아 현행과 직접 비교하기 위함.
경로(실호출 검증됨):
  eflaw lawSearch  -> 시행일자별 버전 목록(법령일련번호 MST + 시행일자)
  target=law&MST   -> 그 시점 본문 (efYd는 본문에 무력이라 MST로 받는다)
"""
import xml.etree.ElementTree as ET

from . import moleg


def list_versions(law_name, law_id=None):
    """[{mst, enforce_date, law_id}] 시행일자별 버전 목록 (law_id로 동명이법 필터)."""
    xml = moleg.call("lawSearch.do", {
        "target": "eflaw", "type": "XML", "query": law_name, "display": "100"})
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for e in root:
        if len(list(e)) == 0:
            continue
        lid = (e.findtext("법령ID") or "").strip()
        if law_id and lid != law_id:
            continue
        mst = (e.findtext("법령일련번호") or "").strip()
        ed = (e.findtext("시행일자") or "").strip()
        if mst and ed:
            out.append({"mst": mst, "enforce_date": ed, "law_id": lid})
    return out


def as_of(versions, ord_date):
    """조례 시행일 시점에 시행 중이던 버전(시행일 <= ord_date 중 최신)."""
    cand = [v for v in versions if v["enforce_date"] and v["enforce_date"] <= ord_date]
    return max(cand, key=lambda v: v["enforce_date"]) if cand else None


def body_articles_by_mst(mst):
    """특정 시행본(MST)의 조문 dict (target=law&MST)."""
    return moleg.parse_law_articles(
        moleg.call("lawService.do", {"target": "law", "MST": str(mst), "type": "XML"}))
