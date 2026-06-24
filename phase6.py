# -*- coding: utf-8 -*-
"""
Phase 6: 조항 단위 시점 검증 + 조항 토큰화 (v2)
==================================================
- check_clause_freshness: 단일 조항 시점 검증
- tokenize_clauses:       "제30조부터 제32조까지" 등을 전개
- check_clause_batch:     references 전체를 한 번에 검증
- get_old_and_new:        신구법 비교 본문
"""
import xml.etree.ElementTree as ET
from datetime import datetime
import re
import time


# ============================================================
# 조항 토큰화 (정규식 v2)
# ============================================================
# "제30조"           -> [(30, 0)]
# "제15조의2"        -> [(15, 2)]
# "제30조부터 제32조까지" -> [(30,0),(31,0),(32,0)]
# "제12조·제19조"    -> [(12,0),(19,0)]

JO_SINGLE_RE = re.compile(r'제(\d+)조(?:의(\d+))?')
JO_RANGE_RE = re.compile(
    r'제(\d+)조(?:의(\d+))?\s*부터\s*제(\d+)조(?:의(\d+))?\s*까지'
)
HANG_HO_RE = re.compile(r'제(\d+)항(?:제(\d+)호)?')


def _to_label(jo, ga):
    """(30, 0) -> '제30조', (15, 2) -> '제15조의2'"""
    return f"제{jo}조" if not ga else f"제{jo}조의{ga}"


def tokenize_clauses(clause_text):
    """조항 표현 문자열을 개별 조항 라벨 리스트로 전개.

    Args:
        clause_text: 예) "제30조부터 제32조까지", "제30조제1항", "제12조·제19조"

    Returns:
        [{"label":"제30조", "jo":30, "ga":0, "hang":None, "ho":None}, ...]
    """
    if not clause_text:
        return []

    tokens = []
    consumed_spans = []

    # 1) 범위 표현 우선 처리 ("제N조부터 제M조까지")
    for m in JO_RANGE_RE.finditer(clause_text):
        n1 = int(m.group(1))
        s1 = int(m.group(2) or 0)
        n2 = int(m.group(3))
        s2 = int(m.group(4) or 0)
        # 양끝 + 사이는 본번만
        if n1 == n2:
            # 같은 본번에서 가지 범위 (희귀)
            for g in range(s1, s2 + 1):
                tokens.append({"label": _to_label(n1, g), "jo": n1, "ga": g,
                               "hang": None, "ho": None})
        else:
            tokens.append({"label": _to_label(n1, s1), "jo": n1, "ga": s1,
                           "hang": None, "ho": None})
            for k in range(n1 + 1, n2):
                tokens.append({"label": _to_label(k, 0), "jo": k, "ga": 0,
                               "hang": None, "ho": None})
            tokens.append({"label": _to_label(n2, s2), "jo": n2, "ga": s2,
                           "hang": None, "ho": None})
        consumed_spans.append(m.span())

    # 2) 범위 외 영역에서 단일 조 + 항/호 추출
    pos = 0
    rest_ranges = []
    for s, e in sorted(consumed_spans):
        if pos < s:
            rest_ranges.append((pos, s))
        pos = e
    if pos < len(clause_text):
        rest_ranges.append((pos, len(clause_text)))

    seen = set()
    for s, e in rest_ranges:
        sub = clause_text[s:e]
        for m in JO_SINGLE_RE.finditer(sub):
            jo = int(m.group(1))
            ga = int(m.group(2) or 0)
            # 직후 30자 이내 항/호 확인
            tail = sub[m.end(): m.end() + 30]
            hh = HANG_HO_RE.match(tail)
            hang = int(hh.group(1)) if hh else None
            ho = int(hh.group(2)) if (hh and hh.group(2)) else None
            label = _to_label(jo, ga)
            key = (label, hang, ho)
            if key in seen:
                continue
            seen.add(key)
            tokens.append({"label": label, "jo": jo, "ga": ga,
                           "hang": hang, "ho": ho})

    # 중복 제거 (label 단위) - 시점 검증은 label만 필요
    uniq = {}
    for t in tokens:
        if t["label"] not in uniq:
            uniq[t["label"]] = t
    return list(uniq.values())


# ============================================================
# 법령 본문 파싱
# ============================================================
def _parse_date(s):
    if not s:
        return None
    s = re.sub(r"[^\d]", "", str(s))[:8]
    try:
        return datetime.strptime(s, "%Y%m%d")
    except Exception:
        return None


def parse_law_articles(xml_data):
    """법령 본문 XML -> {조라벨: 조문 메타} 딕셔너리"""
    if not xml_data:
        return {}
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        return {}

    arts = {}
    for unit in root.findall(".//조문단위"):
        if (unit.findtext("조문여부") or "").strip() != "조문":
            continue
        key = (unit.findtext("조문키") or "").strip()
        jo_num = (unit.findtext("조문번호") or "").strip()
        if not jo_num:
            continue
        # 조문키 마지막 3자리: 001=본조, 002=조의1, 003=조의2
        try:
            ga = int(key[-3:]) - 1 if key else 0
            if ga < 0:
                ga = 0
        except ValueError:
            ga = 0
        label = _to_label(int(jo_num), ga)

        body_parts = [(unit.findtext("조문내용") or "").strip()]
        for hang in unit.findall("항"):
            body_parts.append((hang.findtext("항내용") or "").strip())
            for ho in hang.findall("호"):
                body_parts.append((ho.findtext("호내용") or "").strip())
        body = "\n".join(p for p in body_parts if p)

        arts[label] = {
            "jo_num": jo_num,
            "ga": ga,
            "label": label,
            "title": (unit.findtext("조문제목") or "").strip(),
            "enforce_date": (unit.findtext("조문시행일자") or "").strip(),
            "moved_from": (unit.findtext("조문이동이전") or "").strip(),
            "moved_to": (unit.findtext("조문이동이후") or "").strip(),
            "changed": (unit.findtext("조문변경여부") or "").strip() == "Y",
            "content": body,
        }
    return arts


# ============================================================
# 법령ID 조회 (검색 + 캐시)
# ============================================================
def resolve_law_id(law_name, call_law_api, cache_get, cache_set):
    """법령명 -> 법령ID. 정확매칭 우선."""
    ck = f"lawid_v6:{law_name}"
    cached = cache_get(ck)
    if cached is not None:
        return cached or None

    xml = call_law_api("lawSearch.do", {
        "target": "law", "type": "XML",
        "query": law_name, "display": "5"
    })
    if not xml:
        cache_set(ck, "")
        return None

    law_id = None
    try:
        root = ET.fromstring(xml)
        qn = law_name.replace(" ", "")
        # 정확매칭
        for el in root:
            if len(list(el)) == 0:
                continue
            nm = (el.findtext("법령명한글") or "").strip()
            if nm.replace(" ", "") == qn:
                law_id = (el.findtext("법령ID") or "").strip()
                break
        # 첫 결과 fallback
        if not law_id:
            for el in root:
                if len(list(el)) > 0:
                    law_id = (el.findtext("법령ID") or "").strip()
                    if law_id:
                        break
    except ET.ParseError:
        pass

    cache_set(ck, law_id or "")
    return law_id


def get_law_body(law_id, call_law_api, cache_get, cache_set):
    """법령 본문 XML 조회 (캐시 적용)"""
    if not law_id:
        return None
    ck = f"lb_v6:{law_id}"
    cached = cache_get(ck)
    if cached is not None:
        return cached or None
    xml = call_law_api("lawService.do", {
        "target": "law", "type": "XML", "ID": str(law_id)
    })
    cache_set(ck, xml or "")
    return xml


# ============================================================
# 단일 조항 시점 검증
# ============================================================
def check_clause_freshness(ord_enforce_date, law_name, clause_label,
                           call_law_api, search_law_first,
                           cache_get, cache_set):
    """조항 단위 시점 검증.

    Args:
        ord_enforce_date: 조례 시행일자 (YYYYMMDD)
        law_name:         인용된 법령명
        clause_label:     '제30조' 또는 '제15조의2'
        나머지:             file1.py 함수 주입
    """
    hit = search_law_first(law_name, "law")
    if not hit:
        return {"status": "law_not_found",
                "detail": f"법령 「{law_name}」 미발견",
                "law_name": law_name, "clause_label": clause_label}

    law_id = resolve_law_id(law_name, call_law_api, cache_get, cache_set)
    if not law_id:
        return {"status": "law_id_not_found",
                "detail": "법령ID 추출 실패",
                "law_name": hit["name"], "clause_label": clause_label}

    law_xml = get_law_body(law_id, call_law_api, cache_get, cache_set)
    arts = parse_law_articles(law_xml)
    if not arts:
        return {"status": "law_body_empty",
                "detail": "법령 본문 파싱 실패",
                "law_name": hit["name"], "law_id": law_id,
                "clause_label": clause_label}

    art = arts.get(clause_label)
    if not art:
        # 조문이동 추적
        for other in arts.values():
            if other["moved_from"] and clause_label in other["moved_from"]:
                return {
                    "status": "moved",
                    "detail": f"{clause_label} -> {other['label']} (조문 이동됨)",
                    "law_name": hit["name"], "law_id": law_id,
                    "clause_label": clause_label,
                    "current_label": other["label"],
                    "clause_enforce_date": other["enforce_date"],
                    "content": other["content"][:400],
                }
        return {
            "status": "clause_not_found",
            "detail": f"현행 법령에 {clause_label} 없음 (삭제 의심)",
            "law_name": hit["name"], "law_id": law_id,
            "clause_label": clause_label,
        }

    od = _parse_date(ord_enforce_date)
    cd = _parse_date(art["enforce_date"])
    if not od or not cd:
        return {
            "status": "unknown", "detail": "시행일자 비교 불가",
            "law_name": hit["name"], "law_id": law_id,
            "clause_label": clause_label,
            "clause_title": art["title"],
            "clause_enforce_date": art["enforce_date"],
            "content": art["content"][:400],
        }

    diff = (cd - od).days
    base = {
        "law_name": hit["name"], "law_id": law_id,
        "clause_label": clause_label,
        "clause_title": art["title"],
        "clause_enforce_date": art["enforce_date"],
        "ord_enforce_date": ord_enforce_date,
        "diff_days": diff,
        "content": art["content"][:600],
        "changed_flag": art["changed"],
    }
    if diff > 365:
        base.update({"status": "clause_outdated_critical",
                     "detail": f"인용 조항이 조례 제정 후 {diff}일 뒤 개정됨 (구법 인용 위험)"})
    elif diff > 30:
        base.update({"status": "clause_outdated_warning",
                     "detail": f"인용 조항이 조례 제정 후 {diff}일 뒤 개정됨 (검토 권고)"})
    elif diff > 0:
        base.update({"status": "clause_recently_updated",
                     "detail": f"조항이 조례 제정 직후 {diff}일 내 개정됨 (참조 확인 권장)"})
    else:
        base.update({"status": "clause_current",
                     "detail": f"조항 시행일이 조례 제정 이전 ({-diff}일 전) — 최신화 양호"})
    return base


# ============================================================
# 일괄 검증
# ============================================================
def check_clauses_batch(ord_enforce_date, law_name, clause_labels,
                        call_law_api, search_law_first,
                        cache_get, cache_set, sleep=0.15):
    """한 법령의 여러 조항을 일괄 검증.
    같은 법령에 대해 본문 조회는 1회만 발생 (캐시)."""
    results = []
    for label in clause_labels:
        r = check_clause_freshness(
            ord_enforce_date, law_name, label,
            call_law_api, search_law_first, cache_get, cache_set
        )
        results.append(r)
        time.sleep(sleep)
    return {
        "law_name": law_name,
        "ord_enforce_date": ord_enforce_date,
        "clauses": results,
        "summary": _summarize(results),
    }


def _summarize(results):
    cnt = {"critical": 0, "warning": 0, "current": 0,
           "not_found": 0, "moved": 0, "other": 0}
    for r in results:
        st = r.get("status", "")
        if st == "clause_outdated_critical":
            cnt["critical"] += 1
        elif st in ("clause_outdated_warning", "clause_recently_updated"):
            cnt["warning"] += 1
        elif st == "clause_current":
            cnt["current"] += 1
        elif st in ("clause_not_found", "law_not_found", "law_id_not_found"):
            cnt["not_found"] += 1
        elif st == "moved":
            cnt["moved"] += 1
        else:
            cnt["other"] += 1
    return cnt


# ============================================================
# 신구법 비교 본문 조회
# ============================================================
def get_old_and_new(law_id, call_law_api, cache_get, cache_set):
    if not law_id:
        return None
    ck = f"on_v6:{law_id}"
    cached = cache_get(ck)
    if cached:
        return cached
    xml_data = call_law_api("lawService.do", {
        "target": "oldAndNew", "type": "XML", "ID": str(law_id)
    })
    if not xml_data:
        return None
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        return None

    def info(node):
        if node is None:
            return None
        return {
            "mst": (node.findtext("법령일련번호") or "").strip(),
            "enforce_date": (node.findtext("시행일자") or "").strip(),
            "promulg_date": (node.findtext("공포일자") or "").strip(),
            "promulg_no": (node.findtext("공포번호") or "").strip(),
            "status": (node.findtext("제개정구분명") or "").strip(),
            "is_current": (node.findtext("현행여부") or "").strip() == "Y",
        }

    old_meta = root.find("구조문_기본정보")
    new_meta = root.find("신조문_기본정보")
    old_arts = [(a.get("no"), (a.text or "").strip())
                for a in root.findall("구조문목록/조문")]
    new_arts = [(a.get("no"), (a.text or "").strip())
                for a in root.findall("신조문목록/조문")]

    result = {
        "old": info(old_meta),
        "new": info(new_meta),
        "old_articles": old_arts,
        "new_articles": new_arts,
    }
    cache_set(ck, result)
    return result
