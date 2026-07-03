# -*- coding: utf-8 -*-
"""변경 탐지 (1단계).

조례가 인용한 상위법 조항이 조례 시행일 이후로 바뀌었는지 판정.
A단계 결론 반영:
  - 조문이동 메타는 희소·불안정 → 1차 신호는 "조문시행일 vs 조례시행일" + "부재".
  - 조문이동 메타는 decode 후 보조 신호로만 사용(있으면 이동처 안내).

등급(severity):
  mechanical : 🔧 기계적 개정(번호 이동 등 — 자동 수정안 후보)
  review     : ⚠️ 검토 필요(내용 변경 의심)
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

# 당시/현행 본문을 diff·표시하기 위한 1조문당 최대 보존 글자수.
# 너무 작으면(구 400) 두 시점이 서로 다른 지점에서 잘려 '진짜 변경'이 아니라
# 잘린 꼬리가 diff로 보인다 → 조문 전체가 들어가도록 넉넉히.
EVIDENCE_CHARS = 3000


def amend_dates(content):
    """조내용 안의 <개정/신설/전문개정 YYYY.M.D ...> 태그의 모든 개정일(YYYYMMDD) 오름차순.

    조문시행일자는 전부개정 시 일괄 갱신돼 오탐을 만들지만, 이 태그는 '해당 조항'이
    실제 개정된 날이라 정밀하다. 태그 없으면 빈 리스트.
    """
    out = set()
    for seg in _AMEND_TAG.findall(content or ""):
        for y, mo, d in _AMEND_DATE.findall(seg):
            out.add(f"{int(y):04d}{int(mo):02d}{int(d):02d}")
    return sorted(out)


def latest_amend_date(content):
    """조내용의 최신 개정일(YYYYMMDD) 또는 None."""
    dates = amend_dates(content)
    return dates[-1] if dates else None


def _ymd(s):
    return re.sub(r"[^\d]", "", str(s or ""))[:8]


def basis_dates(content, ord_enforce):
    """현행 조문 내용의 <개정> 태그로 (당시기준, 현행기준, amended_after) 산출.

    - 현행기준 = 최신 개정일.
    - 당시기준 = 조례 시행일 <= 인 최대 개정일(끼인 구간의 개정일). 모든 개정일보다
      조례가 앞서면 '원제정'.
    - amended_after = 조례 시행 이후 개정이 있었는가(= 정비 검토 필요 여부의 1차 신호).
    개정 태그가 없으면 (None, None, None) — 본문 비교로 폴백해야 함.
    """
    dates = amend_dates(content)
    if not dates:
        return (None, None, None)
    od = _ymd(ord_enforce)
    now = dates[-1]
    le = [d for d in dates if od and d <= od]
    was = le[-1] if le else "원제정"
    amended_after = bool(od) and now > od
    return (was, now, amended_after)


def check_clause(articles, clause_label, ord_enforce, law_name="", law_id="", subspec=None):
    """단일 인용 조항 판정 -> finding dict (현행이면 category=current).

    subspec=(항,호,목) 지정 시 그 하위단위 텍스트로 좁혀 개정 태그를 본다 — 정의 조항처럼
    조 전체 <개정> 태그가 딸려 있어도, 인용한 호(예: 제2조제3호)엔 개정 표기가 없으면
    조 전체 '내용변경'으로 오판하지 않는다(당시 시행본 없이도 오탐 억제)."""
    base = {
        "law_id": law_id, "law_name": law_name,
        "clause_label": clause_label, "ord_enforce": ord_enforce,
        "clause_enforce": "", "old_enforce": "", "evidence": "", "clause_detail": "",
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

    # 인용이 특정 호/목이면 그 단위 텍스트로 좁혀 개정 태그를 본다(조 전체 오탐 방지).
    content = art["content"]
    det = ""
    if subspec and any(subspec):
        sub = extract_subunit(art["content"], *subspec)
        if sub is not None:
            det = subspec_label(*subspec)
            base["clause_detail"] = det
            base["evidence"] = sub[:200]      # 근거도 인용한 호로 좁혀 보여줌(조 전체 앞부분 아님)
            content = sub

    # 1차 신호: (좁힌) 조항의 inline 개정일 (전부개정 일괄 갱신 노이즈 회피)
    was, now, amended_after = basis_dates(content, ord_enforce)
    if now is not None:
        base["clause_enforce"] = now
        if was:
            base["old_enforce"] = was
        if amended_after:
            ad, od2 = _date(now), _date(ord_enforce)
            diff = (ad - od2).days if (ad and od2) else 0
            gap = f"개정 {was}→{now}" if (was and was != now) else f"개정일 {now}"
            return {**base, "category": "timing", "severity": "review",
                    "detail": f"인용 조항{det}이 조례 시행({ord_enforce}) 이후 개정됨 "
                              f"({gap}, {diff}일 차) — 내용 변경, 검토 필요"}
        return {**base, "category": "current", "severity": "current",
                "detail": f"해당 조{det} 최종 개정일({now})이 조례 시행 이전 — 현행 정합"}

    # 호/목으로 좁혔는데 그 단위엔 개정 태그가 없지만 조 전체는 조례 이후 개정된 경우 —
    # 이 호의 변경 여부는 당시 시행본 없이 확정 불가 → '내용변경' 오탐 대신 '확인 필요'.
    if det:
        _, art_now, art_after = basis_dates(art["content"], ord_enforce)
        if art_now is not None and art_after:
            base["clause_enforce"] = art_now
            return {**base, "category": "timing", "severity": "check", "change_type": "호미확인",
                    "detail": f"{clause_label}은 조례 시행 이후 개정(개정일 {art_now})됐으나 "
                              f"인용한 {det}엔 개정 표기 없음 — 이 호의 변경 여부 확인 필요"}

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


# ---------- 인용된 항·호·목만 잘라내기(호/목 단위 비교) ----------
_HANG_RE = re.compile(r"^\s*([①-⑳])")        # ①②③…
_HO_RE = re.compile(r"^\s*(\d+(?:의\d+)?)\.")           # 1.  5의2.
_MOK_RE = re.compile(r"^\s*([가-힣])\.")                # 가.  나.


def _circled(n):
    return chr(0x2460 + n - 1) if 1 <= n <= 20 else ""


def subspec_label(hang=None, ho=None, mok=None):
    """(항,호,목) → '제3항제5호나목' 식 라벨(표시·정렬용)."""
    s = ""
    if hang:
        s += f"제{hang}항"
    if ho:
        s += f"제{ho}호"
    if mok:
        s += f"{mok}목"
    return s


def extract_subunit(content, hang=None, ho=None, mok=None):
    """조문 본문(조문내용\\n항\\n호\\n목 형태)에서 인용된 항·호·목만 잘라낸다.

    못 찾으면 None → 호출측이 조 전체로 폴백(좁히다 놓치는 일 방지). 항/호/목은
    바깥→안 순으로 좁힌다(항 범위 → 그 안 호 → 그 안 목).
    """
    if not content or not (hang or ho or mok):
        return None
    block = content.split("\n")

    def narrow(lines, rx, key, *stop):
        start = None
        for i, ln in enumerate(lines):
            m = rx.match(ln)
            if m and m.group(1) == key:
                start = i
                break
        if start is None:
            return None
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if rx.match(lines[j]) or any(s.match(lines[j]) for s in stop):
                end = j
                break
        return lines[start:end]

    if hang:
        block = narrow(block, _HANG_RE, _circled(hang))
        if block is None:
            return None
    if ho:
        block = narrow(block, _HO_RE, str(ho), _HANG_RE)
        if block is None:
            return None
    if mok:
        block = narrow(block, _MOK_RE, mok, _HO_RE, _HANG_RE)
        if block is None:
            return None
    return "\n".join(block).strip() or None


def diff_clause(old_arts, cur_arts, clause_label, ord_enforce, law_name="", law_id="",
                old_enforce="", subspec=None):
    """2단계: 현행 조문의 <개정> 태그(꺽쇠 날짜)를 1차 기준으로 변경 여부를 판정하고,
    당시 시행본 내용이 있으면 diff 근거를 덧붙인다.

    핵심: '조례 시행 이후 그 조항이 개정됐는가'는 현행 조문의 개정 태그가 가장 정밀한
    신호다(시행본 선택 오차·전부개정 일괄갱신에 흔들리지 않음). 즉 조례 시행일이
    최종 개정일보다 뒤면 — 본문 비교가 달라 보여도 — 현행유지로 본다.
    개정 태그가 없을 때만 당시/현행 본문 직접 비교로 폴백한다.
    old_enforce: 당시 시행본 일자(태그로 더 정밀한 당시기준이 잡히면 그것으로 대체).
    """
    base = {"law_id": law_id, "law_name": law_name, "clause_label": clause_label,
            "ord_enforce": ord_enforce, "category": "timing", "clause_enforce": "",
            "old_enforce": old_enforce, "evidence": "", "clause_detail": ""}
    old = old_arts.get(clause_label)
    cur = cur_arts.get(clause_label)

    # 현행에 라벨이 없음 → 이동/삭제 (당시 본문으로 추적)
    if cur is None:
        if old:
            on = _norm(old["content"])
            for lbl, ca in cur_arts.items():
                if _norm(ca["content"]) == on:
                    return {**base, "category": "status", "severity": "review",
                            "change_type": "번호이동", "evidence": old["content"][:160],
                            "detail": f"{clause_label} 내용이 현행 {lbl} 로 이동(번호 변경) — 검토 필요"}
            return {**base, "category": "status", "severity": "review",
                    "change_type": "삭제", "evidence": old["content"][:160],
                    "detail": f"{clause_label} 가 현행 법령에서 사라짐(삭제/통합 의심) — 검토 필요"}
        return {**base, "category": "status", "severity": "check", "change_type": "미확인",
                "detail": f"{clause_label} 를 당시·현행 어디서도 못 찾음 — 확인 필요"}

    # 현행 존재 → 개정 태그(꺽쇠) 기준 1차 판정
    was, now, amended_after = basis_dates(cur["content"], ord_enforce)
    if now:
        base["clause_enforce"] = now
    elif cur.get("enforce_date"):
        base["clause_enforce"] = cur["enforce_date"]
    if was:
        base["old_enforce"] = was            # '원제정' 또는 끼인 구간 개정일

    def _evidence():
        if old:
            return (f"[당시] {old['content'][:EVIDENCE_CHARS]}\n"
                    f"[현행] {cur['content'][:EVIDENCE_CHARS]}")
        return f"[현행] {cur['content'][:EVIDENCE_CHARS]}"

    # 인용이 특정 항·호·목이면 그 부분만 당시↔현행 비교(조 전체 개정 대신 정밀 판정).
    # 좁히기 성공 시 호·목 단위로 변경 여부를 확정한다(놓치면 아래 조 전체 판정으로 폴백).
    if subspec and old and any(subspec):
        hang, ho, mok = subspec
        old_sub = extract_subunit(old["content"], hang, ho, mok)
        cur_sub = extract_subunit(cur["content"], hang, ho, mok)
        if old_sub is not None and cur_sub is not None:
            det = subspec_label(hang, ho, mok)
            base["clause_detail"] = det
            if _norm(old_sub) == _norm(cur_sub):
                return {**base, "category": "current", "severity": "current",
                        "change_type": "동일",
                        "detail": f"{clause_label}{det} 는 조례 시행 이후 변경 없음"
                                  f"(인용한 호·목 동일) — 현행 정합"}
            return {**base, "severity": "review", "change_type": "내용변경",
                    "evidence": f"[당시] {old_sub[:EVIDENCE_CHARS]}\n[현행] {cur_sub[:EVIDENCE_CHARS]}",
                    "detail": f"{clause_label}{det} 가 조례 시행({_ymd(ord_enforce)}) 이후 개정됨 — 검토 필요"}

    if amended_after is True:
        gap = f" (개정 {was}→{now})" if (was and now and was != now) else ""
        return {**base, "severity": "review", "change_type": "내용변경",
                "evidence": _evidence(),
                "detail": f"{clause_label} 가 조례 시행({_ymd(ord_enforce)}) 이후 개정됨{gap} — 검토 필요"}

    if amended_after is False:
        # 조례 시행 이후 개정 이력 없음 → 현행유지(시행본 선택 오차로 인한 가짜 변경 제거)
        return {**base, "category": "current", "severity": "current", "change_type": "동일",
                "detail": f"{clause_label} 는 조례 시행 이후 개정 이력 없음(최종 개정 {now}) — 현행 정합"}

    # 개정 태그 없음 → 당시/현행 본문 직접 비교로 폴백
    if old:
        if _norm(old["content"]) == _norm(cur["content"]):
            return {**base, "category": "current", "severity": "current",
                    "change_type": "동일", "detail": "당시 조문과 현행 내용 동일 — 검토 불필요"}
        return {**base, "severity": "review", "change_type": "내용변경",
                "evidence": _evidence(),
                "detail": f"{clause_label} 내용이 조례 제정 당시와 달라짐(개정이력 표기 없음) — 검토 필요"}

    # 제정 당시 시행본엔 없던 조문이나 현행엔 존재하고(예고법령 반영 등) 개정 태그도 없음 —
    # '당시 어땠는지'는 무의미하다. 현 시점에 유효하면 정비 대상 아님 → 현행 정합(확인 불필요).
    return {**base, "category": "current", "severity": "current", "change_type": "동일",
            "detail": f"{clause_label} 는 현행 법령에 존재(제정 당시본엔 없었으나 현재 유효) — 현행 정합"}


SEV_ORDER = {"mechanical": 0, "review": 1, "check": 2, "current": 3}
SEV_LABEL = {"mechanical": "🔧기계적개정", "review": "⚠️검토필요",
             "check": "📋확인", "current": "✅현행"}


def summarize(findings):
    cnt = {}
    for f in findings:
        cnt[f["severity"]] = cnt.get(f["severity"], 0) + 1
    return cnt
