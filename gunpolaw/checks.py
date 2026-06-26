# -*- coding: utf-8 -*-
"""변경 탐지 (1단계).

조례가 인용한 상위법 조항이 조례 시행일 이후로 바뀌었는지 판정.
A단계 결론 반영:
  - 조문이동 메타는 희소·불안정 → 1차 신호는 "조문시행일 vs 조례시행일" + "부재".
  - 조문이동 메타는 decode 후 보조 신호로만 사용(있으면 이동처 안내).

등급(severity):
  mechanical : 🔧 기계적 개정(번호 이동 등 — 자동 수정안 후보)
  review     : ⚠️ 실질 검토(내용 변경 의심)
  check      : 📋 확인(삭제 의심 등 사람 판단)
  current    : ✅ 현행 유지
"""
from datetime import datetime
import re


def _date(s):
    s = re.sub(r"[^\d]", "", str(s or ""))[:8]
    try:
        return datetime.strptime(s, "%Y%m%d")
    except Exception:
        return None


_AMEND_TAG = re.compile(r"<(?:개정|신설|전문개정)([^>]*)>")
_AMEND_DATE = re.compile(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})")


def latest_amend_date(content):
    """조내용 안의 <개정/신설/전문개정 YYYY.M.D.> 태그에서 최신 개정일(YYYYMMDD).

    조문시행일자는 전부개정 시 모든 조문이 일괄 갱신돼 오탐을 만들지만,
    이 태그는 '해당 조항'이 실제 개정된 날이라 정밀하다. 태그 없으면 None.
    """
    best = None
    for seg in _AMEND_TAG.findall(content or ""):
        for y, mo, d in _AMEND_DATE.findall(seg):
            s = f"{int(y):04d}{int(mo):02d}{int(d):02d}"
            if best is None or s > best:
                best = s
    return best


def check_clause(articles, clause_label, ord_enforce, law_name="", law_id=""):
    """단일 인용 조항 판정 -> finding dict (현행이면 category=current)."""
    base = {
        "law_id": law_id, "law_name": law_name,
        "clause_label": clause_label, "ord_enforce": ord_enforce,
        "clause_enforce": "", "evidence": "",
    }
    art = articles.get(clause_label)

    if not art:
        # 다른 조문이 이 라벨에서 이동해 왔는지(=번호 변경) 추적
        for other in articles.values():
            if other["moved_from"] and other["moved_from"] == clause_label:
                return {**base, "category": "status", "severity": "mechanical",
                        "clause_enforce": other["enforce_date"],
                        "detail": f"{clause_label} → {other['label']} 로 이동(번호 변경). 인용 조문번호 수정 필요",
                        "evidence": other["content"][:200]}
        return {**base, "category": "status", "severity": "check",
                "detail": f"현행 법령에 {clause_label} 없음 (삭제/이동 의심) — 확인 필요"}

    base["evidence"] = art["content"][:200]
    od = _date(ord_enforce)
    if not od:
        return {**base, "category": "timing", "severity": "check",
                "clause_enforce": art["enforce_date"], "detail": "조례 시행일자 비교 불가"}

    # 1차 신호: 해당 조항의 inline 개정일 (전부개정 일괄 갱신 노이즈 회피)
    amend = latest_amend_date(art["content"])
    if amend:
        ad = _date(amend)
        base["clause_enforce"] = amend
        if ad and ad > od:
            diff = (ad - od).days
            return {**base, "category": "timing", "severity": "review",
                    "detail": f"인용 조항이 조례 시행({ord_enforce}) 이후 개정됨 "
                              f"(해당 조 개정일 {amend}, {diff}일 차) — 내용 변경, 검토 필요"}
        return {**base, "category": "current", "severity": "current",
                "detail": f"해당 조 최종 개정일({amend})이 조례 시행 이전 — 현행 정합"}

    # 태그 없음: 조문시행일자로 전부개정 가능성만 보조 판정(확정 변경엔 미포함)
    cd = _date(art["enforce_date"])
    base["clause_enforce"] = art["enforce_date"]
    if cd and cd > od:
        return {**base, "category": "timing", "severity": "check",
                "detail": f"개정이력 표기 없음이나 조문시행일({art['enforce_date']})이 "
                          f"조례 이후 — 전부개정 가능성, 확인 필요"}
    return {**base, "category": "current", "severity": "current",
            "detail": "개정이력 없음·조문시행일 조례 이전 — 현행 정합"}


def _norm(s):
    return re.sub(r"\s+", "", s or "")


def diff_clause(old_arts, cur_arts, clause_label, ord_enforce, law_name="", law_id="",
                old_enforce=""):
    """2단계: 조례 당시 시행본(old) vs 현행(cur) 조문 내용을 직접 비교해 확정.

    시스템 역할 = "검토 대상"을 유형·근거와 함께 알려주는 것. 수정 방법은 담당자 몫.
    old_enforce: 당시 시행본 법령의 시행일자(판단 기준 날짜 표기용).
    """
    base = {"law_id": law_id, "law_name": law_name, "clause_label": clause_label,
            "ord_enforce": ord_enforce, "category": "timing", "clause_enforce": "",
            "old_enforce": old_enforce, "evidence": ""}
    old = old_arts.get(clause_label)
    cur = cur_arts.get(clause_label)

    if old and cur:
        if _norm(old["content"]) == _norm(cur["content"]):
            return {**base, "category": "current", "severity": "current",
                    "change_type": "동일", "detail": "당시 조문과 현행 내용 동일 — 검토 불필요"}
        return {**base, "severity": "review", "change_type": "내용변경",
                "clause_enforce": cur["enforce_date"],
                "evidence": f"[당시] {old['content'][:400]}\n[현행] {cur['content'][:400]}",
                "detail": f"{clause_label} 내용이 조례 제정 당시와 달라짐 — 검토 필요"}

    if old and not cur:
        on = _norm(old["content"])
        for lbl, ca in cur_arts.items():
            if _norm(ca["content"]) == on:
                return {**base, "category": "status", "severity": "review",
                        "change_type": "번호이동", "evidence": old["content"][:160],
                        "detail": f"{clause_label} 내용이 현행 {lbl} 로 이동(번호 변경) — 검토 필요"}
        return {**base, "category": "status", "severity": "review",
                "change_type": "삭제", "evidence": old["content"][:160],
                "detail": f"{clause_label} 가 현행 법령에서 사라짐(삭제/통합 의심) — 검토 필요"}

    if cur and not old:
        return {**base, "severity": "check", "change_type": "당시부재",
                "detail": f"{clause_label} 가 조례 당시 시행본에 없음 — 확인 필요"}

    return {**base, "category": "status", "severity": "check", "change_type": "미확인",
            "detail": f"{clause_label} 를 당시·현행 어디서도 못 찾음 — 확인 필요"}


SEV_ORDER = {"mechanical": 0, "review": 1, "check": 2, "current": 3}
SEV_LABEL = {"mechanical": "🔧기계적개정", "review": "⚠️실질검토",
             "check": "📋확인", "current": "✅현행"}


def summarize(findings):
    cnt = {}
    for f in findings:
        cnt[f["severity"]] = cnt.get(f["severity"], 0) + 1
    return cnt
