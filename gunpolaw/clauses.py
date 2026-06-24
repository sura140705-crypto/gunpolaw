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
HANG_HO_RE = re.compile(r'제(\d+)항(?:제(\d+)호)?')


def to_label(jo, ga=0):
    """(30, 0) -> '제30조', (15, 2) -> '제15조의2'."""
    return f"제{jo}조" if not ga else f"제{jo}조의{ga}"


def tokenize_clauses(clause_text):
    """조항 표현 문자열을 개별 조 라벨 리스트로 전개.

    Returns: [{"label","jo","ga","hang","ho"}, ...] (label 기준 중복 제거)
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
                               "hang": None, "ho": None})
        else:
            tokens.append({"label": to_label(n1, s1), "jo": n1, "ga": s1,
                           "hang": None, "ho": None})
            for k in range(n1 + 1, n2):
                tokens.append({"label": to_label(k, 0), "jo": k, "ga": 0,
                               "hang": None, "ho": None})
            tokens.append({"label": to_label(n2, s2), "jo": n2, "ga": s2,
                           "hang": None, "ho": None})
        consumed.append(m.span())

    # 2) 범위 밖 영역에서 단일 조 + 항/호
    pos, rest = 0, []
    for s, e in sorted(consumed):
        if pos < s:
            rest.append((pos, s))
        pos = e
    if pos < len(clause_text):
        rest.append((pos, len(clause_text)))

    seen = set()
    for s, e in rest:
        sub = clause_text[s:e]
        for m in JO_SINGLE_RE.finditer(sub):
            jo, ga = int(m.group(1)), int(m.group(2) or 0)
            tail = sub[m.end(): m.end() + 30]
            hh = HANG_HO_RE.match(tail)
            hang = int(hh.group(1)) if hh else None
            ho = int(hh.group(2)) if (hh and hh.group(2)) else None
            label = to_label(jo, ga)
            key = (label, hang, ho)
            if key in seen:
                continue
            seen.add(key)
            tokens.append({"label": label, "jo": jo, "ga": ga,
                           "hang": hang, "ho": ho})

    # label 기준 중복 제거 (시점 검증은 label만 필요)
    uniq = {}
    for t in tokens:
        uniq.setdefault(t["label"], t)
    return list(uniq.values())
