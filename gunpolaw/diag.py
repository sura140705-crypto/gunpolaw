# -*- coding: utf-8 -*-
"""판정 근거 추적(관리자 진단) — 저장된 finding 을 만든 검토 로직을 DB 원본으로
그대로 재실행하고 모든 중간 신호를 펼쳐 보인다(라이브 API 0).

목적: 검수자가 "왜 이렇게 판정했나"를 단계별(현행 조문 → 개정일 파싱 → basis_dates
→ 당시 시행본 선택 → 항·호·목 좁히기 → 당시↔현행 비교 → 최종 등급)로 확인.
재실행 결과를 저장값과 대조해 '일치 여부'도 낸다 — 불일치면 파서/로직이 배치 이후
바뀐 신호(reparse 필요)라 로직 점검에 직접 쓰인다.

경계: 검증·서빙과 같은 DB-only. 현행 본문(laws.body_xml)·당시본(law_versions.body_xml)
원본을 그 자리에서 다시 파싱하므로, 미리 파싱된 law_articles 가 아니라 '지금 파서'의
결과로 재판정한다(파서 변경까지 포착).
"""
import re

from . import db
from . import history
from .parse import parse_law_articles
from .reparse import _DBSource
from .checks import (amend_dates, basis_dates, extract_subunit, subspec_label,
                     check_clause, diff_clause, _norm)


def _parse_subspec(detail):
    """clause_detail('제2항제7호'·'제5호'·'가목') → (hang:int, ho:str, mok:str). 없으면 None들."""
    if not detail:
        return (None, None, None)
    mh = re.search(r"제(\d+)항", detail)
    mo = re.search(r"제(\d+(?:의\d+)?)호", detail)
    mk = re.search(r"([가-힣])목", detail)
    return (int(mh.group(1)) if mh else None,
            mo.group(1) if mo else None,
            mk.group(1) if mk else None)


def list_findings(db_path=db.DEFAULT_DB, severity=None, change_type=None,
                  q=None, limit=300):
    """진단용 finding 목록(필터: 등급·변경유형·법령/조례명 부분일치). 등급 위중 순."""
    conn = db.connect(db_path)
    where, args = [], []
    if severity:
        where.append("f.severity=?"); args.append(severity)
    if change_type:
        where.append("COALESCE(f.change_type,'')=?"); args.append(change_type)
    if q:
        where.append("(f.law_name LIKE ? OR o.name LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"""SELECT f.id, f.mst, o.name AS ord_name, o.dept, f.law_name, f.clause_label,
                   f.clause_detail, f.ord_clause, f.severity, f.change_type, f.detail
            FROM findings f JOIN ordinances o ON o.mst=f.mst
            {wsql}
            ORDER BY CASE f.severity WHEN 'mechanical' THEN 0 WHEN 'review' THEN 1
                     WHEN 'check' THEN 2 ELSE 3 END, o.name, f.ord_seq
            LIMIT ?""", (*args, limit)).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM findings f JOIN ordinances o ON o.mst=f.mst {wsql}",
        args).fetchone()[0]
    conn.close()
    return {"total": total, "shown": len(rows), "findings": [dict(r) for r in rows]}


def trace_finding(db_path=db.DEFAULT_DB, finding_id=None):
    """단일 finding 의 판정 로직을 DB 원본으로 재실행 → 단계별 신호 + 저장값 대조."""
    conn = db.connect(db_path)
    fr = conn.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
    if not fr:
        conn.close()
        return {"error": f"finding id={finding_id} 없음"}
    f = dict(fr)
    o = conn.execute("SELECT name, enforce_date, dept FROM ordinances WHERE mst=?",
                     (f["mst"],)).fetchone()
    mrow = conn.execute("SELECT deep FROM batch_meta WHERE id=1").fetchone()
    deep = bool(mrow["deep"]) if mrow else False
    src = _DBSource(conn)

    law_id, clause, ord_enf = f["law_id"], f["clause_label"], f["ord_enforce"]
    steps = []
    out = {"finding": f, "ordinance": (dict(o) if o else {}), "deep": deep, "steps": steps}

    # 조문 비교 함수(check/diff_clause) 밖에서 난 판정(제명변경·법령미해결 등)은 재실행 대상 아님
    if f["change_type"] == "제명변경" or (f["category"] == "status" and not clause):
        steps.append({"k": "분류", "detail":
                      "파이프라인 단계 판정(조문 단위 비교 이전) — 저장된 detail 이 근거."})
        out["recomputed"] = None
        out["match"] = None
        conn.close()
        return out

    # 1) 현행 조문 원본 재파싱
    cur_xml = src.get_law_body(law_id)
    cur_arts = parse_law_articles(cur_xml) if cur_xml else {}
    cur = cur_arts.get(clause)
    steps.append({"k": "현행 조문 원본", "label": clause, "found": cur is not None,
                  "content": (cur["content"] if cur else ""),
                  "clause_enforce": (cur.get("enforce_date") if cur else ""),
                  "note": "" if cur_xml else "laws.body_xml 미적재(슬림 DB?) — 재실행 불가"})

    # 2) 개정일 파싱 + 3) basis_dates
    if cur:
        ad = amend_dates(cur["content"])
        was, now, after = basis_dates(cur["content"], ord_enf)
        steps.append({"k": "개정일 파싱(amend_dates)", "detail":
                      "현행 조문의 <개정/신설/전문개정 날짜> 태그 = 그 조항이 실제 바뀐 날",
                      "dates": ad})
        steps.append({"k": "basis_dates", "ord_enforce": ord_enf,
                      "was": was, "now": now, "amended_after": after, "detail":
                      "amended_after=조례 시행일 이후 개정 존재 → 검토(review), 없으면 현행정합"})

    # 4) deep: 당시 시행본 선택 + 5) 항·호·목 좁히기.
    #   old_arts=None 을 유지해 analyze_ordinance 와 동일 분기(deep이라도 당시본 없으면
    #   check_clause 로 폴백)를 재현한다 — 안 그러면 change_type 이 어긋난다.
    subspec = _parse_subspec(f["clause_detail"])
    old_arts, old, old_enforce_in = None, None, ""
    if deep:
        versions = src.list_versions(f["law_name"], law_id)
        vsel = history.as_of(versions, ord_enf)
        old_enforce_in = vsel["enforce_date"] if vsel else ""
        steps.append({"k": "당시 시행본 선택(as_of)", "n_versions": len(versions),
                      "selected_mst": (vsel["mst"] if vsel else None),
                      "selected_enforce": old_enforce_in,
                      "fallback": vsel is None, "detail":
                      "조례 시행일 <= 시행본 중 최신본을 '당시본'으로. 없으면 현행 태그만으로 판정(check)"})
        if vsel:
            old_arts, _ = src.body_with_xml_by_mst(vsel["mst"])
            old = old_arts.get(clause)
        if any(subspec) and old and cur:
            os_ = extract_subunit(old["content"], *subspec)
            cs_ = extract_subunit(cur["content"], *subspec)
            steps.append({"k": "항·호·목 좁히기(extract_subunit)",
                          "subspec": subspec_label(*subspec),
                          "narrowed": (os_ is not None and cs_ is not None),
                          "old_sub": os_, "cur_sub": cs_,
                          "equal": (os_ is not None and cs_ is not None
                                    and _norm(os_) == _norm(cs_)), "detail":
                          "인용한 그 호·목만 잘라 당시↔현행 비교(못 자르면 조 전체로 폴백)"})

    # 6) 실제 판정 함수 재실행(원본 분기 그대로) → 7) 저장값 대조
    if deep and old_arts is not None:
        rf = diff_clause(old_arts, cur_arts, clause, ord_enf, f["law_name"], law_id,
                         old_enforce=old_enforce_in,
                         subspec=subspec if any(subspec) else None)
    else:
        rf = check_clause(cur_arts, clause, ord_enf, f["law_name"], law_id)
    out["recomputed"] = {"severity": rf["severity"],
                         "change_type": rf.get("change_type", ""),
                         "detail": rf["detail"]}
    out["match"] = (rf["severity"] == f["severity"]
                    and rf.get("change_type", "") == (f["change_type"] or ""))
    conn.close()
    return out
