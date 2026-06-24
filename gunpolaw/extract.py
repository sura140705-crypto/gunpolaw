# -*- coding: utf-8 -*-
"""조례 본문 인용추출 (재작성).

legacy file1.py의 약점 2가지를 교정:
  1. carry-over("같은 법") 오해소 — legacy는 본문 전체의 *마지막* 법령으로
     모든 "같은 법"을 묶었음(버그). 여기서는 **위치 기반 단일 패스**로
     각 "같은 법"을 바로 앞 「법령명」에 연결.
  2. inline 약칭 정의 정규식 깨짐 — legacy는 복붙 과정에서 괄호가 '$'로
     깨져 「법」(이하 "법"이라 한다) 를 못 잡았음. 여기서는 정상 괄호로 복원.

핵심 API: extract_citations(text) -> [ref...], group_by_law(refs)
"""
import re
import unicodedata

from .clauses import tokenize_clauses

# ---------- 조항 표현 (범위/병렬은 tokenize_clauses가 펼침) ----------
CLAUSE_INNER = (
    r'제\d+조(?:의\d+)?'
    r'(?:\s*(?:부터|및|또는|,|ㆍ|·)\s*제\d+조(?:의\d+)?)*'
    r'(?:\s*까지)?'
    r'(?:\s*의?\s*규정)?'
    r'(?:\s*제\d+항(?:\s*(?:및|또는|,|ㆍ|·)\s*제\d+항)*)?'
    r'(?:\s*제\d+호(?:\s*(?:및|또는|,|ㆍ|·)\s*제\d+호)*)?'
    r'(?:\s*제\d+목)?'
)

# 「법령명」 + (선택)inline약칭정의 + (선택)조항
#   inline 정의: (이하 "○○"이라 한다)  ← legacy의 깨진 '$' 를 정상 괄호로 복원
LAW_CITE_RE = re.compile(
    r'「([^」\n]{2,80})」'
    r'(?:\s*\(\s*이하\s*["“]([^"”]+)["”]\s*(?:이?라\s*한다)?\s*\))?'
    r'(?:\s*(' + CLAUSE_INNER + r'))?'
)

# carry-over: "같은 법 / 같은 법 시행령 / 같은 법 시행규칙 / 같은 영" + 조항
SAME_LAW_RE = re.compile(
    r'같은\s*(법\s*시행규칙|법\s*시행령|영|법)\s*(' + CLAUSE_INNER + r')'
)

# 분류
GENERIC_NAMES = {"법령", "다른 법령", "법령이나 조례",
                 "법령이나 다른 조례", "법령 등"}
LOCAL_PREFIX = (
    "군포시", "경기도", "안양시", "의왕시", "수원시", "성남시", "안산시",
    "서울특별시", "부산광역시", "인천광역시", "대구광역시",
    "광주광역시", "대전광역시", "울산광역시", "세종특별자치시",
)
ORDIN_SUFFIX = ("조례", "규칙")
SELF_REF_RE = re.compile(r'이\s*조례|이\s*규칙')


def normalize_text(text):
    """NFC 정규화 + 구분자/공백 통일."""
    if not text:
        return ""
    t = unicodedata.normalize("NFC", text)
    t = t.replace("·", "·").replace("・", "·").replace("ㆍ", "ㆍ")
    t = t.replace("　", " ")
    return t


def classify(name):
    """법령 / 자치법규 / 기타(일반어)."""
    if not name or name in GENERIC_NAMES:
        return "기타"
    if any(name.startswith(p) for p in LOCAL_PREFIX):
        return "자치법규"
    if any(name.endswith(s) for s in ORDIN_SUFFIX):
        return "자치법규"
    return "법령"


def _resolve_same(last_law, kind):
    """'같은 법/영/법 시행령/법 시행규칙' -> 실제 법령명."""
    kind = kind.replace(" ", "")
    if "시행규칙" in kind:
        return last_law + " 시행규칙"
    if "시행령" in kind or kind == "영":
        return last_law + " 시행령"
    return last_law


def _ref(name, clause, alias_source):
    return {
        "name": name,
        "clause": clause,
        "clause_labels": [t["label"] for t in tokenize_clauses(clause)],
        "alias_source": alias_source,
        "type": classify(name),
    }


def extract_citations(text):
    """본문에서 (법령명, 조항) 인용을 추출.

    위치 기반 단일 패스로 「법령명」과 "같은 법"을 함께 훑어,
    각 carry-over를 바로 앞 법령에 정확히 연결한다.
    """
    text = normalize_text(text)

    # 1) 「법령명」 / "같은 법" 이벤트를 위치순으로 병합
    events = []
    for m in LAW_CITE_RE.finditer(text):
        events.append((m.start(), "law", m))
    for m in SAME_LAW_RE.finditer(text):
        events.append((m.start(), "same", m))
    events.sort(key=lambda e: e[0])

    refs = []
    doc_aliases = {}
    last_law = None
    for _, typ, m in events:
        if typ == "law":
            name = m.group(1).strip()
            alias = (m.group(2) or "").strip()
            clause = (m.group(3) or "").strip()
            if alias:
                doc_aliases[alias] = name          # inline 약칭 등록
            if classify(name) in ("법령", "자치법규"):
                last_law = name                    # carry-over 기준 갱신
            refs.append(_ref(name, clause, "inline" if alias else None))
        else:  # same — 위치상 바로 앞 법령에 연결
            if not last_law:
                continue
            resolved = _resolve_same(last_law, m.group(1))
            clause = (m.group(2) or "").strip()
            refs.append(_ref(resolved, clause, "carry_over"))

    # 2) inline 정의된 약칭의 단독 사용 ("법 제30조") 해소
    for alias, full in doc_aliases.items():
        if not alias:
            continue
        pat = re.compile(r'(?<![「\w])' + re.escape(alias) +
                         r'\s*(' + CLAUSE_INNER + r')')
        for m in pat.finditer(text):
            refs.append(_ref(full, (m.group(1) or "").strip(), "alias"))

    return refs


def group_by_law(refs):
    """추출 ref들을 법령명 단위로 묶어 조 라벨을 합친다.

    Returns: {법령명: {"type","clause_labels":[...],"alias_sources":[...]}}
    자기참조/일반어(기타)는 제외.
    """
    grouped = {}
    for r in refs:
        if r["type"] == "기타":
            continue
        g = grouped.setdefault(r["name"], {
            "name": r["name"], "type": r["type"],
            "clause_labels": set(), "alias_sources": set(),
        })
        g["clause_labels"].update(r["clause_labels"])
        if r["alias_source"]:
            g["alias_sources"].add(r["alias_source"])

    def sort_key(label):
        jo = re.search(r"제(\d+)조", label)
        ga = re.search(r"의(\d+)", label)
        return (int(jo.group(1)) if jo else 9999,
                int(ga.group(1)) if ga else 0)

    out = {}
    for name, g in grouped.items():
        out[name] = {
            "name": name,
            "type": g["type"],
            "clause_labels": sorted(g["clause_labels"], key=sort_key),
            "alias_sources": sorted(g["alias_sources"]),
        }
    return out
