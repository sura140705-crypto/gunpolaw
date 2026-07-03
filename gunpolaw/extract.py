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

from . import config
from .clauses import tokenize_clauses

# ---------- 조항 표현 (범위/병렬은 tokenize_clauses가 펼침) ----------
_HO = r'제\d+(?:\s*의\s*\d+)?호'         # 호(가지호 제N의M호 포함, '제10의 2호')
# 한 조의 세부단위(항·호·목) — 각 단위 내부도 및/또는/,/ㆍ/· 로 병렬 나열 가능
_SUBUNIT = (
    r'(?:\s*제\d+항(?:\s*(?:및|또는|,|ㆍ|·)\s*제\d+항)*)?'
    r'(?:\s*' + _HO + r'(?:\s*(?:및|또는|,|ㆍ|·)\s*' + _HO + r')*)?'
    r'(?:\s*[가-힣]목)?'
)
# 조 단위 = 제N조[의M] + (항·호·목). 이 단위를 부터/및/또는/,/ㆍ/· 로 이어 나열한다.
_ART_UNIT = r'제\d+조(?:의\d+)?' + _SUBUNIT
# 인용 조항열: 조 단위의 나열(각 뒤 '까지' 허용) + 말미 '규정'.
# 핵심: '제23조제1항제3호 및 제108조'처럼 항·호 뒤에 오는 병렬 조도 흡수한다 — '및'을
# ','와 동일한 병렬 접속으로 취급(과거엔 첫 조의 항/호가 나오면 뒤 병렬 조가 잘렸음).
CLAUSE_INNER = (
    _ART_UNIT + r'(?:\s*까지)?'
    r'(?:\s*(?:부터|및|또는|,|ㆍ|·)\s*' + _ART_UNIT + r'(?:\s*까지)?)*'
    r'(?:\s*의?\s*규정)?'
)

# 「법령명」 + (선택)inline약칭정의 + (선택)조항
#   inline 정의: (이하 "○○"이라 한다). 여는 따옴표가 OCR로 ·· 등으로 깨진 자료가 있어
#   여는/닫는 구분자를 따옴표·가운뎃점·공백 모두 허용(못 잡으면 뒤따르는 조항까지 유실됨).
LAW_CITE_RE = re.compile(
    r'「([^」\n]{2,80})」'
    r'(?:\s*\(\s*이하\s*[\s"“·]*([가-힣]{1,20}?)\s*(?:["”·]\s*)?이?라\s*한다\s*\))?'
    r'(?:\s*(' + CLAUSE_INNER + r'))?'
)

# carry-over: "같은 법 / 같은 법 시행령 / 같은 법 시행규칙 / 같은 영" + 조항
SAME_LAW_RE = re.compile(
    r'같은\s*(법\s*시행규칙|법\s*시행령|영|법)\s*(' + CLAUSE_INNER + r')'
)

# 맨몸 인용: 「」 없이 쓴 '단일토큰 법명(+선택 시행령) + 조항' ("주민등록법 제2조제2항").
# 「」가 인용 경계를 주지 않으므로 신뢰 가능한 경우로 한정 — 공백 없는 단일 한글토큰이
# 법/법률로 끝나는 것만(여러어절·약칭은 경계 모호로 제외). 조항이 반드시 뒤따라야 채택.
# 앞이 「/단어문자면 매칭 안 함(정상 「」 인용·긴 이름 회피). 해소 실패 맨몸은 파이프라인에서 침묵 드롭.
NAKED_LAW_RE = re.compile(
    r'(?<![「\w])'
    r'([가-힣]{2,}(?:법률|법))(\s*시행령)?'
    r'\s*(' + CLAUSE_INNER + r')'
)
# 법명처럼 보이나 단독 법령이 아닌 토큰(멀티어절 꼬리·지시어) — 맨몸 채택 제외
NAKED_STOP = {"같은법", "이법", "그법", "본법", "해당법", "동법", "관계법", "관련법",
              "상위법", "현행법", "신법", "구법", "위반법", "준용법", "적용법",
              "특별법", "기본법", "일반법"}
# 약칭(법 등) 직전이 이 조사로 끝나면 '단어+약칭'(띄어쓰기 오류)로 본다. 실제 법명
# 어간 끝(건축'법'·민'법'·수'도'법…)과 겹치지 않는 보수적 집합만(도/로/의 등 제외).
_JOSA_BEFORE_ALIAS = set("는은를을에")
# 지시어(띄어쓰기 있는 '같은 법'·'이 법'…) 뒤의 약칭은 carry_over(SAME_LAW)나 자기/타 참조라
# 약칭 재스캔 대상이 아니다 — 예: '같은 법 제38조'의 꼬리 '법'이 약칭 '법'으로 오매칭되는 것 방지.
# (붙여쓴 '같은법'은 앞이 \w 라 기존 (?<![「\w]) 로 이미 차단됨 — 띄어쓴 경우만 보강)
_ALIAS_REF_PREFIX = ("같은", "이", "그", "본", "해당", "다른", "동")
_ALIAS_NEG_LB = "".join(f"(?<!{w} )" for w in _ALIAS_REF_PREFIX)

# 분류
GENERIC_NAMES = {"법령", "다른 법령", "법령이나 조례",
                 "법령이나 다른 조례", "법령 등"}
# 자치법규 인식 접두어 — 타 시군 재사용 위해 config(환경변수/region.json)에서 주입.
# 모듈 로드 1회 산정(수집 CLI는 실행 전 env 설정). 기본값/우선순위는 config.load 참조.
LOCAL_PREFIX = tuple(config.load()["local_prefix"])
SELF_REF_RE = re.compile(r'이\s*조례|이\s*규칙')


def normalize_text(text):
    """NFC 정규화 + 구분자/공백 통일."""
    if not text:
        return ""
    t = unicodedata.normalize("NFC", text)
    t = t.replace("·", "·").replace("・", "·").replace("ㆍ", "ㆍ")
    t = t.replace("　", " ")
    return t


# 행정규칙(훈령·예규·고시·지침·요령·규정·기준) 접미어. 지자체가 발령해도 자치법규(조례·
# 규칙)가 아니라 별도 저장소(법제처 target=admrul) 대상 → '법령' 타입으로 흘려 pipeline
# 의 admrul 폴백을 태운다. '규정/기준'은 대통령령(○○규정)과도 겹치나, 그건 애초에
# LOCAL_PREFIX 로 시작하지 않아 아래 지자체 분기에 걸리지 않는다(target=law 로 정상 해소).
ADMRUL_SUFFIX = ("훈령", "예규", "고시", "지침", "요령", "규정", "기준")


def classify(name):
    """법령(상위법령·행정규칙) / 자치법규(조례·규칙) / 기타(일반어).

    주의: '○○법 시행규칙'은 국가법령(법령)이다. '규칙'으로 끝난다고 자치법규로
    보면 시행규칙(상위법)을 통째로 놓친다 → '시행규칙'은 법령, 그 외 '규칙'(지자체
    규칙)·'조례'만 자치법규.
    행정규칙(훈령·지침·규정 등)은 지자체 발령이라도 자치법규가 아니라 '법령' 타입 —
    pipeline 이 target=law 실패 시 target=admrul 로 폴백 조회한다.
    """
    if not name or name in GENERIC_NAMES:
        return "기타"
    if name.endswith("조례"):
        return "자치법규"
    if name.endswith("규칙") and "시행규칙" not in name:
        return "자치법규"
    # 지자체 접두어로 시작해도 행정규칙 접미어면 자치법규 아님(행정규칙 → '법령'으로)
    if any(name.startswith(p) for p in LOCAL_PREFIX) and not name.endswith(ADMRUL_SUFFIX):
        return "자치법규"
    return "법령"


def is_local_admrul(name):
    """지자체 자체 행정규칙(예: '군포시 …기본지침')인가.

    이들은 ELIS(자치법규시스템)에만 있고 법제처 국가법령정보 OpenAPI(law/admrul/ordin)
    엔 없어 자동 조회가 불가능하다 → '폐지 의심'이 아니라 '수기 확인'으로 구분한다.
    """
    return bool(name) and name.endswith(ADMRUL_SUFFIX) and \
        any(name.startswith(p) for p in LOCAL_PREFIX)


def _resolve_same(last_law, kind):
    """'같은 법/영/법 시행령/법 시행규칙' -> 실제 법령명."""
    kind = kind.replace(" ", "")
    if "시행규칙" in kind:
        return last_law + " 시행규칙"
    if "시행령" in kind or kind == "영":
        return last_law + " 시행령"
    return last_law


def _ref(name, clause, alias_source, ord_article="", ord_seq=0, span=None, raw=""):
    toks = tokenize_clauses(clause)
    return {
        "name": name,
        "clause": clause,
        "clause_labels": [t["label"] for t in toks],
        "clause_tokens": toks,              # 항·호·목 포함(호/목 단위 비교용)
        "alias_source": alias_source,
        "type": classify(name),
        "ord_article": ord_article or "",   # 인용이 등장한 조례 조문(제Y조)
        "ord_seq": ord_seq,                 # 조례 본문 내 등장 순서(작을수록 먼저)
        "span": span or (0, 0),             # (start,end) 정규화 조문본문 내 문자 offset — 하이라이트용
        "raw": raw,                         # 매칭된 원문(디버그·표시)
    }


def extract_citations(text):
    """본문 문자열 1개에서 인용 추출 (조례 조문 위치 태깅 없음, 하위호환)."""
    return _extract_segments([("", text)])


def extract_citations_by_article(articles):
    """조례 조문(조)별로 인용 추출 — ref마다 ord_article(인용이 등장한 조례 조문) 태깅.

    조문을 본문 순서대로 단일 패스 처리하므로 carry-over('같은 법')·inline 약칭이
    조문 경계를 넘어도 직전 법령에 정확히 연결된다. articles: get_ordinance_body 의
    [{"no","body",...}] 목록.
    """
    segs = [(a.get("no") or "", a.get("body") or "")
            for a in articles if a.get("body")]
    return _extract_segments(segs)


def _extract_segments(segments):
    """[(article_no, text)] 순서열 → refs.

    위치 기반 단일 패스로 「법령명」과 "같은 법"을 함께 훑어, 각 carry-over를
    바로 앞 법령에 정확히 연결한다. last_law/약칭은 세그먼트(조문) 순서로 이어진다.
    """
    norm = [(no, normalize_text(t)) for no, t in segments]
    refs = []
    doc_aliases = {}
    last_law = None
    seq = 0   # 본문 등장 순서(조문 순서 → 조문 내 위치순)

    # 1) 「법령명」 / "같은 법" 이벤트를 (조문 순서 → 위치순)으로 처리
    for no, text in norm:
        events = []
        for m in LAW_CITE_RE.finditer(text):
            events.append((m.start(), "law", m))
        for m in SAME_LAW_RE.finditer(text):
            events.append((m.start(), "same", m))
        for m in NAKED_LAW_RE.finditer(text):
            events.append((m.start(), "naked", m))
        # 같은 시작위치에 「」(law)와 naked 가 겹치면 law 우선(0), naked 후순위(1)
        events.sort(key=lambda e: (e[0], 0 if e[1] != "naked" else 1))
        for _, typ, m in events:
            seq += 1
            if typ == "law":
                name = m.group(1).strip()
                alias = (m.group(2) or "").strip()
                clause = (m.group(3) or "").strip()
                if alias:
                    doc_aliases[alias] = name          # inline 약칭 등록
                if classify(name) in ("법령", "자치법규"):
                    last_law = name                    # carry-over 기준 갱신
                refs.append(_ref(name, clause, "inline" if alias else None, no, seq,
                                 span=m.span(), raw=m.group(0)))
            elif typ == "same":  # 위치상 바로 앞 법령에 연결
                if not last_law:
                    continue
                resolved = _resolve_same(last_law, m.group(1))
                clause = (m.group(2) or "").strip()
                refs.append(_ref(resolved, clause, "carry_over", no, seq,
                                 span=m.span(), raw=m.group(0)))
            else:  # naked — 「」 없이 쓴 단일토큰 법명(서식 위반이나 인식은 함)
                base = m.group(1).strip()
                if base.replace(" ", "") in NAKED_STOP:
                    seq -= 1                            # 채택 안 함 → 순서 보존
                    continue
                # 띄어쓰기 오류로 약칭이 앞 단어에 붙은 경우("경우에는법"="경우에는"+약칭"법")
                # — 등록 약칭으로 redirect하고 '띄어쓰기 정비' 대상으로(꺽쇠 아님). 하이라이트는
                # 붙은 앞말(위원회는)을 빼고 약칭부터('법 제16조…') 잡도록 span을 당긴다.
                redirect = al_hit = None
                for al, full in doc_aliases.items():
                    if (al and base.endswith(al) and len(base) > len(al)
                            and base[-len(al) - 1] in _JOSA_BEFORE_ALIAS):
                        redirect, al_hit = full, al
                        break
                if redirect:
                    clause = (m.group(3) or "").strip()
                    if classify(redirect) in ("법령", "자치법규"):
                        last_law = redirect
                    plen = len(base) - len(al_hit)        # 붙은 앞말 길이(위원회는=4)
                    s0, s1 = m.span()
                    refs.append(_ref(redirect, clause, "alias_spacing", no, seq,
                                     span=(s0 + plen, s1), raw=m.group(0)[plen:]))
                    continue
                name = (base + (m.group(2) or "")).strip()
                clause = (m.group(3) or "").strip()
                if classify(name) in ("법령", "자치법규"):
                    last_law = name
                refs.append(_ref(name, clause, "naked", no, seq,
                                 span=m.span(), raw=m.group(0)))

    # 2) inline 정의된 약칭의 단독 사용 ("법 제30조") 해소 — 전 약칭 수집 후 전체 재스캔
    for alias, full in doc_aliases.items():
        if not alias:
            continue
        pat = re.compile(r'(?<![「\w])' + _ALIAS_NEG_LB + re.escape(alias) +
                         r'\s*(' + CLAUSE_INNER + r')')
        for no, text in norm:
            for m in pat.finditer(text):
                seq += 1
                refs.append(_ref(full, (m.group(1) or "").strip(), "alias", no, seq,
                                 span=m.span(), raw=m.group(0)))

    return refs


def _clause_sort_key(label):
    jo = re.search(r"제(\d+)조", label)
    ga = re.search(r"의(\d+)", label)
    return (int(jo.group(1)) if jo else 9999,
            int(ga.group(1)) if ga else 0)


def _resolve_specs(specs_by_label):
    """{label: set((hang,ho,mok))} → {label: (hang,ho,mok)}.

    한 조에 '단일한 세부단위'만 인용됐을 때만 그 단위로 좁힌다. 여러 호/목이 섞이거나
    조 전체 인용이 함께 있으면 None(=조 전체 비교) — 좁혀서 일부를 놓치는 일이 없도록.
    """
    out = {}
    for label, specs in specs_by_label.items():
        nontrivial = {s for s in specs if any(s)}
        out[label] = next(iter(nontrivial)) if len(nontrivial) == 1 and len(specs) == 1 else None
    return out


def _dedup_occurrences(occs):
    """(조례조문, 상위법조, 항, 호, 목)별 1건으로 dedup(최소 등장순서 보존), 순서대로 정렬."""
    by = {}
    for o in occs:
        k = (o["ord_article"], o["label"], o["hang"], o["ho"], o["mok"])
        if k not in by or o["ord_seq"] < by[k]["ord_seq"]:
            by[k] = o
    return sorted(by.values(), key=lambda o: (o["ord_seq"], _clause_sort_key(o["label"])))


def group_by_law(refs):
    """추출 ref들을 법령명 단위로 묶어 조 라벨을 합친다.

    Returns: {법령명: {"type", "clause_labels":[...], "alias_sources":[...],
                       "clause_articles": {상위법조라벨: [조례조문...]},
                       "law_articles": [법명only로 인용한 조례조문...],
                       "clause_seq": {상위법조라벨: 등장순서}, "law_seq": 등장순서}}
    clause_articles/law_articles 로 "상위법 제X조 → 조례 제Y조" 역추적,
    clause_seq/law_seq 로 조례 본문 등장 순서 정렬이 가능하다.
    자기참조/일반어(기타)는 제외.
    """
    grouped = {}
    for r in refs:
        if r["type"] == "기타":
            continue
        g = grouped.setdefault(r["name"], {
            "name": r["name"], "type": r["type"],
            "clause_labels": set(), "alias_sources": set(),
            "clause_articles": {}, "law_articles": set(),
            "clause_seq": {}, "law_seq": None, "sources": set(),
            "clause_specs": {},   # {label: set((hang,ho,mok))}
            "occurrences": [],    # 조례 조문별 인용 1건씩 (조례 기준 finding 단위)
        })
        _oa, _sq = r.get("ord_article") or "", r.get("ord_seq") or 0
        _naked = r.get("alias_source") == "naked"   # 꺽쇠 없는 '전체 법령명' 인용만 True
        _spacing = r.get("alias_source") == "alias_spacing"   # 약칭 붙여쓰기 오류
        for t in r.get("clause_tokens", []):
            g["clause_specs"].setdefault(t["label"], set()).add(
                (t.get("hang"), t.get("ho"), t.get("mok")))
            g["occurrences"].append({
                "label": t["label"], "ord_article": _oa, "ord_seq": _sq,
                "naked": _naked, "spacing": _spacing,
                "hang": t.get("hang"), "ho": t.get("ho"), "mok": t.get("mok")})
        # 출처 추적: 평이한 「」 인용(alias_source None)은 'bracket' 으로 기록.
        g["sources"].add(r.get("alias_source") or "bracket")
        oa = r.get("ord_article") or ""
        sq = r.get("ord_seq") or 0
        if r["clause_labels"]:
            for lbl in r["clause_labels"]:
                g["clause_labels"].add(lbl)
                if oa:
                    g["clause_articles"].setdefault(lbl, set()).add(oa)
                prev = g["clause_seq"].get(lbl)
                if prev is None or sq < prev:
                    g["clause_seq"][lbl] = sq
        else:
            if oa:
                g["law_articles"].add(oa)      # 법명만 인용(조항 없음)
            if g["law_seq"] is None or sq < g["law_seq"]:
                g["law_seq"] = sq
        if r["alias_source"]:
            g["alias_sources"].add(r["alias_source"])

    out = {}
    for name, g in grouped.items():
        out[name] = {
            "name": name,
            "type": g["type"],
            "clause_labels": sorted(g["clause_labels"], key=_clause_sort_key),
            "clause_articles": {
                lbl: sorted(arts, key=_clause_sort_key)
                for lbl, arts in g["clause_articles"].items()},
            "law_articles": sorted(g["law_articles"], key=_clause_sort_key),
            "clause_seq": dict(g["clause_seq"]),
            "law_seq": g["law_seq"] or 0,
            # 조별 인용 세부단위: 그 조에 단일 호/목만 인용됐을 때만 좁힌다(여러 개면 None=조 전체)
            "clause_specs": _resolve_specs(g["clause_specs"]),
            # 조례 조문 × 상위법조 × 호/목 단위로 dedup — finding 1건의 단위(조례 기준)
            "occurrences": _dedup_occurrences(g["occurrences"]),
            "alias_sources": sorted(g["alias_sources"]),
            # 서식 판정: 맨몸 인용 포함 여부 / 맨몸으로만 잡혔는지(해소 실패 시 침묵 드롭 기준)
            "naked_any": "naked" in g["sources"],
            "naked_only": g["sources"] == {"naked"},
        }
    return out
