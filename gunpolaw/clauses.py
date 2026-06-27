# -*- coding: utf-8 -*-
"""조항 표현 토큰화.

"제30조부터 제32조까지" -> [제30조, 제31조, 제32조]
"제12조ㆍ제19조"        -> [제12조, 제19조]
"제15조의2"             -> [제15조의2]
legacy phase6.tokenize_clauses 를 정비해 이식.
"""
import re

JO_SINGLE_RE = re.compile(r'제(\d+)조(?:의(\d+))?')
JO_RANGE_RE = re.compile(
    r'제(\d+)조(?:의(\d+))?\s*부터\s*제(\d+)조(?:의(\d+))?\s*까지'
)
# 조 뒤의 항(단일)·호 나열(가지호 포함)·목(단일). "제3호, 제4호 및 제10의 2호" 같은 a,b,c,d.
_HANG_P = re.compile(r'\s*제(\d+)항')
_HO_ENUM_P = re.compile(
    r'\s*(제\d+(?:\s*의\s*\d+)?호(?:\s*(?:,|ㆍ|·|및|또는)\s*제\d+(?:\s*의\s*\d+)?호)*)')
_HO_P = re.compile(r'제(\d+(?:\s*의\s*\d+)?)호')
_MOK_P = re.compile(r'\s*([가-힣])목')


def to_label(jo, ga=0):
    """(30, 0) -> '제30조', (15, 2) -> '제15조의2'."""
    return f"제{jo}조" if not ga else f"제{jo}조의{ga}"


def tokenize_clauses(clause_text):
    """조항 표현 문자열을 개별 조 라벨 리스트로 전개.

    Returns: [{"label","jo","ga","hang","ho","mok"}, ...] (label 기준 중복 제거)
    hang/ho/mok = 인용된 항·호·목(있으면) — 호/목 단위로 좁혀 비교하기 위함.
    """
    if not clause_text:
        return []

    tokens = []
    consumed = []

    # 1) 범위 표현 "제N조부터 제M조까지"
    for m in JO_RANGE_RE.finditer(clause_text):
        n1, s1 = int(m.group(1)), int(m.group(2) or 0)
        n2, s2 = int(m.group(3)), int(m.group(4) or 0)
        if n1 == n2:
            for g in range(s1, s2 + 1):
                tokens.append({"label": to_label(n1, g), "jo": n1, "ga": g,
                               "hang": None, "ho": None, "mok": None})
        else:
            tokens.append({"label": to_label(n1, s1), "jo": n1, "ga": s1,
                           "hang": None, "ho": None, "mok": None})
            for k in range(n1 + 1, n2):
                tokens.append({"label": to_label(k, 0), "jo": k, "ga": 0,
                               "hang": None, "ho": None, "mok": None})
            tokens.append({"label": to_label(n2, s2), "jo": n2, "ga": s2,
                           "hang": None, "ho": None, "mok": None})
        consumed.append(m.span())

    # 2) 범위 밖 영역에서 단일 조 + 항/호
    pos, rest = 0, []
    for s, e in sorted(consumed):
        if pos < s:
            rest.append((pos, s))
        pos = e
    if pos < len(clause_text):
        rest.append((pos, len(clause_text)))

    for s, e in rest:
        sub = clause_text[s:e]
        for m in JO_SINGLE_RE.finditer(sub):
            jo, ga = int(m.group(1)), int(m.group(2) or 0)
            label = to_label(jo, ga)
            tail = sub[m.end(): m.end() + 80]
            pos = 0
            hm = _HANG_P.match(tail, pos)
            hang = int(hm.group(1)) if hm else None
            if hm:
                pos = hm.end()
            em = _HO_ENUM_P.match(tail, pos)
            mok = None
            if em:
                hos = [re.sub(r"\s+", "", h) for h in _HO_P.findall(em.group(1))]
                mk = _MOK_P.match(tail, em.end()) if len(hos) == 1 else None
                mok = mk.group(1) if mk else None
            else:
                hos = []
            # 호 나열은 각 호를 별도 토큰으로(조례 조문별·호별 판정). 호 없으면 항/조 단위.
            subs = [(hang, h, (mok if len(hos) == 1 else None)) for h in hos] \
                or [(hang, None, None)]
            for hg, h, mk in subs:
                tokens.append({"label": label, "jo": jo, "ga": ga,
                               "hang": hg, "ho": h, "mok": mk})

    # (label, 항, 호, 목) 전체 키로 중복 제거 — 같은 조의 서로 다른 호는 보존
    uniq = {}
    for t in tokens:
        uniq.setdefault((t["label"], t["hang"], t["ho"], t["mok"]), t)
    return list(uniq.values())
