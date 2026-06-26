#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
군포시 조례-상위법령 정합성 검토 시스템 v3.2
==========================================================
Phase 1~4: 전수수집 + 본문파싱 + 인용추출 + 최신화점검
Phase 6:   조항 단위 시점 검증 + 토큰화 + 일괄검증 + 신구법비교

실행: python file1.py
브라우저: http://localhost:8765
"""

import http.server
import json
import os
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import re
import socket
import ssl
import sqlite3
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import phase6  # 조항 단위 시점 검증 모듈

# STEP3 권고 뷰: 사전 배치한 gunpolaw.db findings 를 조례별 행동지시로 렌더(report.py 재사용).
# 패키지/DB 없으면 권고 뷰만 비활성(서버 기동·기존 기능엔 영향 없음).
try:
    from gunpolaw.report import recommend_fragment as _recommend_fragment
except Exception as _gl_err:
    _recommend_fragment = None
    print(f"  [권고뷰 비활성] gunpolaw 임포트 실패: {_gl_err}")

# ============================================================
# 설정
# ============================================================
OC = os.environ.get("LAW_OC_KEY", "").strip()  # 하드코딩 금지: 환경변수 또는 기동 시 입력
PORT = 8765
CACHE_TTL = 300
GYEONGGI_ORG = "6410000"
GUNPO_SBORG = "4020000"
DB_PATH = Path(__file__).parent / "gunpo_ordinances.db"

KND_SEARCH_CODES = {
    "30001": "조례", "30002": "규칙", "30003": "훈령",
    "30004": "예규", "30010": "고시", "30011": "의회규칙",
}
KND_BODY_CODES = {
    "C0001": "조례", "C0002": "규칙", "C0003": "훈령",
    "C0004": "예규", "C0006": "기타", "C0010": "고시", "C0011": "의회규칙",
}

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

_cache = {}


def cache_get(key):
    if key in _cache:
        ts, val = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return val
        del _cache[key]
    return None


def cache_set(key, val):
    _cache[key] = (time.time(), val)


# ============================================================
# 법제처 API
# ============================================================
def call_law_api(endpoint, params):
    params["OC"] = OC
    qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    urls = [
        f"http://www.law.go.kr/DRF/{endpoint}?{qs}",
        f"https://www.law.go.kr/DRF/{endpoint}?{qs}",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; GunpoLawChecker/3.2)"
            })
            if url.startswith("https"):
                resp = urllib.request.urlopen(req, timeout=20, context=ssl_ctx)
            else:
                resp = urllib.request.urlopen(req, timeout=20)
            raw = resp.read()
            for enc in ["utf-8", "euc-kr", "cp949"]:
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  [API FAIL] {url[:70]}... -> {e}")
    return None


# ============================================================
# 조문번호 디코딩
# ============================================================
def decode_article_no(code):
    code = (code or "").strip()
    if len(code) < 6 or not code.isdigit():
        return code
    main, sub = int(code[:4]), int(code[4:6])
    return f"제{main}조" if sub == 0 else f"제{main}조의{sub}"


# ============================================================
# Phase 1: 자치법규 검색
# ============================================================
def search_ordinances_official(knd_code, page=1, display=100, sort="efasc"):
    key = f"so_v3:{knd_code}:{page}:{display}:{sort}"
    cached = cache_get(key)
    if cached:
        return cached
    xml_data = call_law_api("lawSearch.do", {
        "target": "ordin", "type": "XML",
        "knd": knd_code, "nw": "1",
        "org": GYEONGGI_ORG, "sborg": GUNPO_SBORG,
        "display": str(display), "page": str(page),
        "sort": sort,
    })
    if not xml_data:
        return {"error": "API 호출 실패", "items": [], "totalCount": 0}
    result = parse_search_xml(xml_data, knd_code)
    cache_set(key, result)
    return result


def parse_search_xml(xml_data, knd_search_code=""):
    result = {"items": [], "totalCount": 0}
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        result["error"] = "XML 파싱 실패"
        result["raw_sample"] = xml_data[:500]
        return result

    tc = root.find(".//totalCnt")
    if tc is not None and tc.text:
        try:
            result["totalCount"] = int(tc.text)
        except ValueError:
            pass

    knd_label = KND_SEARCH_CODES.get(knd_search_code, "")
    for elem in root:
        if len(list(elem)) == 0:
            continue
        item = {
            "MST": elem.findtext("자치법규일련번호", "").strip(),
            "자치법규ID": elem.findtext("자치법규ID", "").strip(),
            "자치법규명": elem.findtext("자치법규명", "").strip(),
            "자치법규종류": elem.findtext("자치법규종류", "").strip() or knd_label,
            "지자체기관명": elem.findtext("지자체기관명", "").strip(),
            "공포일자": elem.findtext("공포일자", "").strip(),
            "공포번호": elem.findtext("공포번호", "").strip(),
            "시행일자": elem.findtext("시행일자", "").strip(),
            "제개정구분명": elem.findtext("제개정구분명", "").strip(),
            "자치법규분야명": elem.findtext("자치법규분야명", "").strip(),
            "자치법규상세링크": elem.findtext("자치법규상세링크", "").strip(),
        }
        if item["MST"]:
            result["items"].append(item)
    return result


# ============================================================
# Phase 2: 본문 파싱
# ============================================================
def get_detail_parsed(mst):
    if not mst:
        return {"error": "MST 누락"}
    key = f"d_v3:{mst}"
    cached = cache_get(key)
    if cached:
        return cached

    # 수집 때 저장한 본문이 있으면 API 없이 로컬에서 파싱(오프라인·즉시).
    # API 재호출은 본문이 아직 없을 때만 — 키·네트워크 장애에도 본문이 뜬다.
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT body_xml FROM ordinances WHERE mst=?", (str(mst),)).fetchone()
        conn.close()
        if row and row["body_xml"]:
            parsed = parse_body_xml(row["body_xml"])
            if "error" not in parsed:
                cache_set(key, parsed)
                return parsed
    except Exception:
        pass

    xml_data = call_law_api("lawService.do", {
        "target": "ordin", "MST": str(mst), "type": "XML",
    })
    if not xml_data:
        return {"error": "본문 조회 실패"}
    parsed = parse_body_xml(xml_data)
    if "error" not in parsed:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
                UPDATE ordinances
                SET body_xml=?, dept=?, reason_text=?, revision_text=?, body_fetched_at=?
                WHERE mst=?
            """, (xml_data, parsed.get("meta", {}).get("dept", ""),
                  parsed.get("reason_text", ""),
                  parsed.get("revision_text", ""),
                  datetime.now().isoformat(), str(mst)))
            conn.commit()
            conn.close()
        except Exception:
            pass
    cache_set(key, parsed)
    return parsed


def parse_body_xml(xml_data):
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        return {"error": f"XML 파싱 실패: {e}", "raw_preview": xml_data[:300]}

    info = root.find("자치법규기본정보")
    if info is None:
        err = (root.text or "").strip() or "본문 정보 없음"
        return {"error": err}

    knd_code = (info.findtext("자치법규종류") or "").strip()
    meta = {
        "lid": (info.findtext("자치법규ID") or "").strip(),
        "mst": (info.findtext("자치법규일련번호") or "").strip(),
        "name": (info.findtext("자치법규명") or "").strip(),
        "knd_code": knd_code,
        "knd": KND_BODY_CODES.get(knd_code, knd_code or "(미상)"),
        "promulg_date": (info.findtext("공포일자") or "").strip(),
        "promulg_no": (info.findtext("공포번호") or "").strip(),
        "enforce_date": (info.findtext("시행일자") or "").strip(),
        "org": (info.findtext("지자체기관명") or "").strip(),
        "initiator": (info.findtext("자치법규발의종류") or "").strip(),
        "dept": (info.findtext("담당부서명") or "").strip(),
        "tel": (info.findtext("전화번호") or "").strip(),
        "status": (info.findtext("제개정정보") or "").strip(),
    }

    articles = []
    for jo in root.findall(".//조문/조"):
        code = (jo.findtext("조문번호") or "").strip()
        articles.append({
            "code": code,
            "no": decode_article_no(code),
            "is_article": (jo.findtext("조문여부") or "").strip() == "Y",
            "title": (jo.findtext("조제목") or "").strip(),
            "body": (jo.findtext("조내용") or "").strip(),
        })

    addenda = []
    for ad in root.findall(".//부칙"):
        units = ad.findall("부칙단위")
        if units:
            for u in units:
                addenda.append({
                    "date": (u.findtext("부칙공포일자") or "").strip(),
                    "no": (u.findtext("부칙공포번호") or "").strip(),
                    "body": (u.findtext("부칙내용") or "").strip(),
                })
        else:
            addenda.append({
                "date": (ad.findtext("부칙공포일자") or "").strip(),
                "no": (ad.findtext("부칙공포번호") or "").strip(),
                "body": (ad.findtext("부칙내용") or "").strip(),
            })

    reason_text = ""
    el = root.find(".//제개정이유내용")
    if el is None:
        el = root.find(".//제개정이유")
    if el is not None:
        parts = [(t or "").strip() for t in el.itertext()]
        reason_text = "\n".join(p for p in parts if p).strip()

    revision_text = ""
    el = root.find(".//개정문내용")
    if el is None:
        el = root.find(".//개정문")
    if el is not None:
        parts = [(t or "").strip() for t in el.itertext()]
        revision_text = "\n".join(p for p in parts if p).strip()

    full_text = "\n".join(a["body"] for a in articles)
    if addenda:
        full_text += "\n\n[부칙]\n" + "\n".join(a["body"] for a in addenda)

    return {
        "meta": meta,
        "articles": articles,
        "addenda": addenda,
        "reason_text": reason_text,
        "revision_text": revision_text,
        "full_text": full_text,
    }


# ============================================================
# Phase 3: 인용 추출 (조항 토큰화 포함) — v3.3 규칙 엔진 통합
# ============================================================
# 양식 A 규칙 R001~R015 + 양식 B 약칭 사전 + 양식 C 제외 필터
# R003(범위전개) / R004(조의N) / R008(병렬) / 항·호 분해는
# phase6.tokenize_clauses 가 이미 처리 → 본 모듈은 다음만 담당:
#   - 1차 추출 (「법령명」 + 조항 페어링)
#   - 약칭 inline 정의(R006) + 정의 직후 조항(R006X)
#   - carry-over (R001/R015 "같은 법", "같은 법 시행령" 등)
#   - 메타 부착 (quantifier R009 / exclusion R011 / proviso R014 / byeolpyo R010)
#   - 양식 C 제외 필터 (자기참조 / 자치법규간참조 / 일반어 / 별표트랙)
import unicodedata

# ---------- 양식 A: 정규식 ----------
# 조항 표현 (R002 본체) — 범위/병렬은 phase6가 펼치므로 평문으로만 캡처
CLAUSE_INNER = (
    r'제\d+조(?:의\d+)?'
    r'(?:\s*(?:부터|및|또는|,|ㆍ|·)\s*제\d+조(?:의\d+)?)*'
    r'(?:\s*까지)?'
    r'(?:\s*의?\s*규정)?'
    r'(?:\s*제\d+항(?:\s*(?:및|또는|,|ㆍ|·)\s*제\d+항)*)?'
    r'(?:\s*제\d+호(?:\s*(?:및|또는|,|ㆍ|·)\s*제\d+호)*)?'
    r'(?:\s*제\d+목)?'
)

# R002 / R005 / R006 / R006X
# 「법령명」 + (선택)inline정의 + (선택)조항
LAW_CITE_RE = re.compile(
    r'「([^」\n]{2,80})」'
    r'(?:\s*$\s*이하\s*[\"\u201c]([^\"\u201d]+)[\"\u201d]'
    r'\s*(?:이?라\s*한다)?\s*$)?'
    r'(?:\s*(' + CLAUSE_INNER + r'))?'
)

# R001 / R015 carry-over: "같은 법 [시행령|시행규칙] 제○조" / "같은 영 제○조"
SAME_LAW_RE = re.compile(
    r'같은\s*(법\s*시행규칙|법\s*시행령|영|법)\s*(' + CLAUSE_INNER + r')'
)

# R006 inline 약칭 정의 (1패스 사전 구축용 — LAW_CITE_RE와 중복 매칭 무해)
ALIAS_DEF_RE = re.compile(
    r'「([^」]{2,80})」\s*$\s*이하\s*[\"\u201c]([^\"\u201d]+)[\"\u201d]\s*'
    r'(?:이?라\s*한다)?\s*$', re.DOTALL
)

# R010 별표/별지
BYEOLPYO_RE = re.compile(r'별표\s*\d+|별지\s*제\d+호(?:\s*서식)?')

# R009 quantifier
Q_ANY_RE = re.compile(r'각\s*호의\s*어느\s*하나')
Q_ALL_RE = re.compile(r'각\s*호(?!의\s*어느)')

# R011 부정형 어미 → exclusion
EXCLUSION_RE = re.compile(r'(?:외에는?|을?\s*제외(?:한|하고|하는|함))')

# R014 단서 → scope=proviso
PROVISO_RE = re.compile(r'단서(?:규정)?')

# 양식 C 제외 패턴
SELF_REF_RE = re.compile(r'(?:이\s*조례|이\s*규칙|이\s*조례\s*시행\s*전)')
GENERIC_NAMES = {"법령", "다른 법령", "법령이나 조례",
                 "법령이나 다른 조례", "법령 등"}

# 양식 B-1 디폴트 약칭 키 (carry-over로 동적 해소)
DEFAULT_ALIAS_KEYS = {"법", "영", "규칙"}

# 자치법규 판별
LOCAL_PREFIX = (
    "군포시", "경기도", "안양시", "의왕시", "수원시", "성남시", "안산시",
    "서울특별시", "부산광역시", "인천광역시", "대구광역시",
    "광주광역시", "대전광역시", "울산광역시", "세종특별자치시",
)
ORDIN_SUFFIX = ("조례", "규칙")


def classify_ref(name):
    """법령 / 자치법규 / 기타(일반어) 분류."""
    if not name:
        return "기타"
    if name in GENERIC_NAMES:
        return "기타"
    if any(name.startswith(p) for p in LOCAL_PREFIX):
        return "자치법규"
    if any(name.endswith(s) for s in ORDIN_SUFFIX):
        return "자치법규"
    return "법령"


def normalize_text(text):
    """R012/R013: NFC 정규화 + 구분자/공백 통일."""
    if not text:
        return ""
    t = unicodedata.normalize("NFC", text)
    t = t.replace("\u00b7", "·").replace("\u30fb", "·").replace("\u318d", "ㆍ")
    t = t.replace("\u3000", " ")
    return t


def extract_aliases(text):
    """R006: 「법령명」(이하 "X"라 한다) inline 정의 1패스 수집."""
    text = normalize_text(text)
    return {m.group(2).strip(): m.group(1).strip()
            for m in ALIAS_DEF_RE.finditer(text)}


def _clause_meta(cited, around):
    """R009/R011/R014/R010 메타 추출 (조항 표현 + 주변 문맥)."""
    meta = {
        "quantifier": None,           # R009: any|all|None
        "relation_type": "inclusion", # R011: inclusion|exclusion
        "scope": "main",              # R014: main|proviso
        "byeolpyo": False,            # R010
    }
    ctx = (cited or "") + " " + (around or "")
    if Q_ANY_RE.search(ctx):
        meta["quantifier"] = "any"
    elif Q_ALL_RE.search(ctx):
        meta["quantifier"] = "all"
    if EXCLUSION_RE.search(ctx):
        meta["relation_type"] = "exclusion"
    if PROVISO_RE.search(ctx):
        meta["scope"] = "proviso"
    if BYEOLPYO_RE.search(ctx):
        meta["byeolpyo"] = True
    return meta


def find_refs_in_text(text, aliases=None, doc_aliases=None):
    """R001/R002/R005/R006X/R007/R015 1차 추출.

    Returns: [{name, cited, meta, alias_source, raw_text}, ...]
    """
    text = normalize_text(text)
    aliases = aliases or {}
    doc_aliases = dict(doc_aliases or {})
    found = []
    last_law = None  # carry-over 기준점

    # 1) 「법령명」 + (inline정의?) + (조항?)
    for m in LAW_CITE_RE.finditer(text):
        name = m.group(1).strip()
        alias_inline = (m.group(2) or "").strip()
        cited = (m.group(3) or "").strip()
        if alias_inline:
            doc_aliases[alias_inline] = name  # R006 등록
        around = text[max(0, m.start() - 40): m.end() + 40]
        meta = _clause_meta(cited, around)
        cls = classify_ref(name)
        if cls in ("법령", "자치법규"):
            last_law = name  # carry-over 갱신
        found.append({
            "name": name,
            "cited": cited,
            "meta": meta,
            "alias_source": "inline" if alias_inline else None,
            "raw_text": m.group(0)[:200],
        })

    # 2) "같은 법 / 같은 법 시행령 / 같은 법 시행규칙 / 같은 영" (R001/R015)
    for m in SAME_LAW_RE.finditer(text):
        if not last_law:
            continue
        kind = m.group(1).replace(" ", "")
        cited = (m.group(2) or "").strip()
        if "시행규칙" in kind:
            resolved = last_law + " 시행규칙"
        elif "시행령" in kind or kind == "영":
            resolved = last_law + " 시행령"
        else:
            resolved = last_law
        around = text[max(0, m.start() - 40): m.end() + 40]
        meta = _clause_meta(cited, around)
        found.append({
            "name": resolved,
            "cited": cited,
            "meta": meta,
            "alias_source": "carry_over",
            "raw_text": m.group(0)[:200],
        })

    # 3) 약칭 단어 매치 (R007) — 디폴트 사전(B-1) + inline 정의(R006) 머지
    merged = {**aliases, **doc_aliases}
    for alias, full_name in merged.items():
        if not alias:
            continue
        # 짧은 약칭은 디폴트 키(법/영/규칙)일 때만 허용
        if len(alias) < 2 and alias not in DEFAULT_ALIAS_KEYS:
            continue
        pattern = re.compile(
            r'(?<![「\w])' + re.escape(alias) + r'\s*(' + CLAUSE_INNER + r')'
        )
        for m in pattern.finditer(text):
            cited = (m.group(1) or "").strip()
            around = text[max(0, m.start() - 40): m.end() + 40]
            meta = _clause_meta(cited, around)
            found.append({
                "name": full_name,
                "cited": cited,
                "meta": meta,
                "alias_source": "inline" if alias in doc_aliases else "default",
                "raw_text": m.group(0)[:200],
            })

    return found


# ============================================================
# STEP1 상세: 인용 출현 span (약칭/이월/내부참조 규칙 반영)
# ============================================================
# 주의: 위 LAW_CITE_RE/ALIAS_DEF_RE 는 복붙 과정의 '$' 손상으로 inline 약칭을
# 못 잡는다(인계노트 경고). 여기서는 손상 없는 정규식 + 깨진 따옴표(··법”)까지
# 관대하게 처리해 span을 만든다. STEP1 모달이 이 결과를 그대로 하이라이트.
_RS_LAW = re.compile(r'「([^」\n]{2,80})」')
# 「법령명」 바로 뒤의 (이하 "X"라 한다) — 따옴표 자리에 ·/ㆍ/curly/없음 모두 허용
_RS_INLINE_ALIAS = re.compile(
    r'^\s*\(\s*이하\s*[^()가-힣A-Za-z]*'
    r'([가-힣A-Za-z][가-힣A-Za-z0-9 ]*?)\s*'
    r'[^()가-힣A-Za-z]*\s*(?:이?라)\s*한다\s*\)')


def _rs_classify(name):
    if re.search(r'시행(령|규칙)$', name):
        return "법령"
    if re.search(r'(조례|규칙)$', name):
        return "자치법규"
    return "법령"


def ref_spans_for_articles(articles):
    """조문별 인용 출현 span: {조index: [{start,end,kind,law_name,law_type,clause_text}]}.

    규칙:
      - 「법령명」 = law span (법령/자치법규 분류)
      - 「법령명」(이하 "법") inline 약칭 → 문서 전역 사전 등록
      - "법/영/규칙 제○조" 약칭 단독사용 → 정의된(또는 영=시행령·규칙=시행규칙) 법령에 연결
      - "같은 법/영/시행령/시행규칙" 이월
      - 위 단서에 근접(≤5자)한 조항만 인용으로 마킹.
        앞에 아무 법령 단서 없이 나온 조항 = 조례 자기조문 → 제외.
      - "이 규칙/이 조례 …" 자기참조 가드.
    """
    # 1) 전역 inline 약칭 사전
    doc_aliases = {}
    for a in articles:
        body = a.get("body") or ""
        for m in _RS_LAW.finditer(body):
            am = _RS_INLINE_ALIAS.match(body[m.end():m.end() + 90])
            if am:
                doc_aliases[am.group(1).strip()] = m.group(1).strip()

    alias_words = sorted({w for w in list(doc_aliases) + ["영", "규칙"] if w},
                         key=len, reverse=True)
    alias_alt = "|".join(re.escape(w) for w in alias_words)
    token = re.compile(
        r'(「[^」\n]{2,80}」)'
        r'|(같은\s*(?:법\s*시행규칙|법\s*시행령|영|법))'
        + (r'|(?<![가-힣A-Za-z「])(' + alias_alt + r')(?=\s*제\d)' if alias_alt else "")
        + r'|(' + CLAUSE_INNER + r')')

    GAP = 5
    out = {}
    for idx, a in enumerate(articles):
        if not a.get("is_article"):
            continue
        body = a.get("body") or ""
        spans, cur_law, cur_type, base_law, bind_end = [], "", "", "", -999
        for m in token.finditer(body):
            if m.group(1) is not None:                       # 「법령명」
                nm = m.group(1)[1:-1].strip()
                cur_law, cur_type = nm, _rs_classify(nm)
                if cur_type == "법령" and not re.search(r'시행(령|규칙)$', nm):
                    base_law = nm
                bind_end = m.end()
                am = _RS_INLINE_ALIAS.match(body[m.end():m.end() + 90])
                if am:                                       # 약칭 정의 괄호까지 건너뜀
                    bind_end = m.end() + am.end()
                spans.append({"start": m.start(), "end": m.end(), "kind": "law",
                              "law_name": nm, "law_type": cur_type})
            elif m.group(2) is not None:                     # 같은 법/영/시행령/규칙
                if base_law:
                    k = m.group(2).replace(" ", "")
                    if "시행규칙" in k:
                        cur_law = base_law + " 시행규칙"
                    elif "시행령" in k or k.endswith("영"):
                        cur_law = base_law + " 시행령"
                    else:
                        cur_law = base_law
                    cur_type, bind_end = "법령", m.end()
            elif alias_alt and m.lastindex and m.group(3) is not None:  # 약칭 단독
                w = m.group(3)
                pre = body[max(0, m.start() - 3):m.start()]
                if "이" in pre or "같은" in pre:              # 자기참조/이월 가드
                    continue
                if w in doc_aliases:
                    cur_law = doc_aliases[w]
                elif w == "영" and base_law:
                    cur_law = base_law + " 시행령"
                elif w == "규칙" and base_law:
                    cur_law = base_law + " 시행규칙"
                else:
                    continue
                cur_type, bind_end = _rs_classify(cur_law), m.end()
            else:                                            # 조항 표현
                if cur_law and (m.start() - bind_end) <= GAP:
                    spans.append({"start": m.start(), "end": m.end(), "kind": "clause",
                                  "law_name": cur_law, "law_type": cur_type,
                                  "clause_text": m.group(0)})
                    bind_end = m.end()
                # 단서 없는 조항 = 조례 자기조문 → 제외
        if spans:
            out[idx] = spans
    return out


def extract_all_refs(parsed_body):
    """양식 A/B/C 통합 적용.
    출력 호환:
      references[]            : 기존 UI 유지 (clauses는 문자열 배열)
      excluded_refs[]         : 자기참조 / 일반어
      local_ordinance_refs[]  : 자치법규간 참조 (별도 트랙)
      byeolpyo_track[]        : 별표/별지
    """
    if "error" in parsed_body:
        return {"error": parsed_body["error"]}

    full_text = parsed_body.get("full_text", "")
    doc_aliases = extract_aliases(full_text)

    grouped = {}
    excluded_refs = []
    local_ordinance_refs = {}
    byeolpyo_track = []

    def add_ref(rec, where_type, where_info):
        name = rec["name"]
        cited = rec["cited"]
        if not name or len(name) < 2:
            return
        if name in GENERIC_NAMES:
            excluded_refs.append({"name": name, "reason": "일반 카테고리어",
                                  "where": where_type})
            return
        if rec["meta"].get("byeolpyo"):
            byeolpyo_track.append({"name": name, "cited": cited,
                                   "where": where_type})

        cls = classify_ref(name)
        # 양식 C: 자치법규간 참조 → local_ordinance_ref 트랙
        if cls == "자치법규":
            key = name
            if key not in local_ordinance_refs:
                local_ordinance_refs[key] = {
                    "name": name, "count": 0,
                    "locations": [], "clauses": set()
                }
            lo = local_ordinance_refs[key]
            lo["count"] += 1
            if cited and cited not in lo["locations"]:
                lo["locations"].append(cited)
            if cited:
                for tok in phase6.tokenize_clauses(cited):
                    lo["clauses"].add(tok["label"])
            return

        # 정상 적재 (법령)
        if name not in grouped:
            grouped[name] = {
                "name": name, "type": cls,
                "total_count": 0, "locations": [],
                "clause_labels": set(),
                "clauses_meta": {},
                "sources": {"articles": [], "reason": [], "revision": []},
                "alias_sources": set(),
            }
        info = grouped[name]
        info["total_count"] += 1
        if cited and cited not in info["locations"]:
            info["locations"].append(cited)
        if rec.get("alias_source"):
            info["alias_sources"].add(rec["alias_source"])
        if cited:
            for tok in phase6.tokenize_clauses(cited):
                label = tok["label"]
                info["clause_labels"].add(label)
                # 메타 머지 (강한 메타 우선)
                m = info["clauses_meta"].get(label, {
                    "quantifier": None, "relation_type": "inclusion",
                    "scope": "main", "byeolpyo": False,
                    "alias_source": rec.get("alias_source"),
                    "range_expanded": False,
                })
                rm = rec["meta"]
                if rm.get("quantifier"):
                    m["quantifier"] = rm["quantifier"]
                if rm.get("relation_type") == "exclusion":
                    m["relation_type"] = "exclusion"
                if rm.get("scope") == "proviso":
                    m["scope"] = "proviso"
                if rm.get("byeolpyo"):
                    m["byeolpyo"] = True
                if any(k in cited for k in ["부터", "및", "또는", "·", "ㆍ", ","]):
                    m["range_expanded"] = True
                info["clauses_meta"][label] = m

        if where_type == "article":
            entry = {"article": where_info["no"],
                     "title": where_info["title"], "cited": cited}
            if entry not in info["sources"]["articles"]:
                info["sources"]["articles"].append(entry)
        elif where_type == "reason":
            ctx = where_info[:120] + ("..." if len(where_info) > 120 else "")
            if ctx not in info["sources"]["reason"]:
                info["sources"]["reason"].append(ctx)
        elif where_type == "revision":
            ctx = where_info[:120] + ("..." if len(where_info) > 120 else "")
            if ctx not in info["sources"]["revision"]:
                info["sources"]["revision"].append(ctx)

    # 본문 처리
    for art in parsed_body.get("articles", []):
        if not art.get("is_article"):
            continue
        body = art["body"]
        # 자기참조 카운트 (양식 C)
        n_self = len(SELF_REF_RE.findall(body or ""))
        if n_self:
            excluded_refs.append({
                "name": "(자기참조)", "reason": "이 조례/규칙 내부 참조",
                "where": "article", "article": art["no"], "count": n_self
            })
        for rec in find_refs_in_text(body, doc_aliases=doc_aliases):
            add_ref(rec, "article", {"no": art["no"], "title": art["title"]})

    # 제개정이유내용
    reason = parsed_body.get("reason_text", "")
    if reason:
        for rec in find_refs_in_text(reason, doc_aliases=doc_aliases):
            add_ref(rec, "reason", reason)

    # 개정문내용
    revision = parsed_body.get("revision_text", "")
    if revision:
        for rec in find_refs_in_text(revision, doc_aliases=doc_aliases):
            add_ref(rec, "revision", revision)

    # confidence + 정렬 + UI 호환 필드 생성
    for info in grouped.values():
        srcs = info["sources"]
        n_src = sum([bool(srcs["articles"]),
                     bool(srcs["reason"]), bool(srcs["revision"])])
        if n_src >= 2 or info["total_count"] >= 3:
            info["confidence"] = "high"
        elif info["total_count"] >= 2:
            info["confidence"] = "medium"
        else:
            info["confidence"] = "low"
        labels = sorted(
            info["clause_labels"],
            key=lambda l: (
                int(re.search(r"제(\d+)조", l).group(1))
                    if re.search(r"제(\d+)조", l) else 9999,
                int(re.search(r"의(\d+)", l).group(1))
                    if re.search(r"의(\d+)", l) else 0
            )
        )
        info["clauses"] = labels  # 기존 UI 호환 (문자열 배열)
        info["clauses_detail"] = [
            {"label": l, "meta": info["clauses_meta"].get(l, {})}
            for l in labels
        ]  # 신규 메타
        info["alias_sources"] = sorted(info["alias_sources"])
        del info["clause_labels"]
        del info["clauses_meta"]

    references = sorted(grouped.values(),
                        key=lambda x: (-x["total_count"], x["name"]))
    external = [r for r in references if r["type"] == "법령"]
    local = [r for r in references if r["type"] == "자치법규"]

    local_list = []
    for v in local_ordinance_refs.values():
        v["clauses"] = sorted(v.pop("clauses"))
        local_list.append(v)

    return {
        "references": references,
        "aliases": doc_aliases,
        "excluded_refs": excluded_refs,
        "local_ordinance_refs": local_list,
        "byeolpyo_track": byeolpyo_track,
        "stats": {
            "total_refs": sum(r["total_count"] for r in references),
            "unique_laws": len(references),
            "external_laws": len(external),
            "local_ordin": len(local) + len(local_list),
            "has_reason_refs": any(r["sources"]["reason"] for r in references),
            "has_revision_refs": any(r["sources"]["revision"] for r in references),
            "total_clauses": sum(len(r.get("clauses", [])) for r in references),
            "excluded_count": len(excluded_refs),
            "byeolpyo_count": len(byeolpyo_track),
            "local_ordin_refs": len(local_list),
        }
    }


# ============================================================
# Phase 4: 최신화 점검 (법령 단위)
# ============================================================
def search_law_first(query, target="law"):
    key = f"sl_v3:{target}:{query}"
    cached = cache_get(key)
    if cached:
        return cached
    xml_data = call_law_api("lawSearch.do", {
        "target": target, "type": "XML",
        "query": query, "display": "5", "page": "1",
    })
    if not xml_data:
        return None
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        return None
    candidates = []
    for elem in root:
        if len(list(elem)) == 0:
            continue
        nm = (elem.findtext("법령명한글") or elem.findtext("법령명") or
              elem.findtext("자치법규명") or "").strip()
        edate = (elem.findtext("시행일자") or "").strip()
        pdate = (elem.findtext("공포일자") or "").strip()
        if nm:
            candidates.append({"name": nm, "edate": edate, "pdate": pdate})

    if not candidates:
        cache_set(key, None)
        return None

    qn = query.replace(" ", "")
    for c in candidates:
        if c["name"].replace(" ", "") == qn:
            cache_set(key, c)
            return c
    for c in candidates:
        cn = c["name"].replace(" ", "")
        if qn in cn or cn in qn:
            cache_set(key, c)
            return c
    cache_set(key, candidates[0])
    return candidates[0]


def parse_date(s):
    if not s:
        return None
    s = re.sub(r"[^\d]", "", str(s))[:8]
    try:
        return datetime.strptime(s, "%Y%m%d")
    except Exception:
        return None


def check_freshness(ord_enforce_date, ref_name, ref_type="법령"):
    target = "ordin" if ref_type == "자치법규" else "law"
    hit = search_law_first(ref_name, target)
    if not hit:
        return {
            "status": "not_found",
            "detail": "현행 법령 미발견 (폐지/제명변경 의심)",
            "law_name": ref_name, "law_enforce_date": "",
            "diff_days": None,
        }
    od = parse_date(ord_enforce_date)
    ld = parse_date(hit["edate"]) or parse_date(hit["pdate"])
    if not od or not ld:
        return {
            "status": "unknown", "detail": "시행일자 비교 불가",
            "law_name": hit["name"], "law_enforce_date": hit["edate"],
            "diff_days": None,
        }
    diff = (ld - od).days
    if diff > 365:
        return {"status": "outdated_critical",
                "detail": f"법령이 조례보다 {diff}일 늦게 시행 (구법 인용 위험)",
                "law_name": hit["name"], "law_enforce_date": hit["edate"],
                "diff_days": diff}
    elif diff > 30:
        return {"status": "outdated_warning",
                "detail": f"법령이 조례보다 {diff}일 늦게 시행 (검토 권고)",
                "law_name": hit["name"], "law_enforce_date": hit["edate"],
                "diff_days": diff}
    else:
        rn = ref_name.replace(" ", "")
        cn = hit["name"].replace(" ", "")
        if rn == cn:
            return {"status": "current",
                    "detail": "현행 법령과 일치 (최신화 양호)",
                    "law_name": hit["name"], "law_enforce_date": hit["edate"],
                    "diff_days": diff}
        else:
            return {"status": "name_mismatch",
                    "detail": f"이름 차이: 「{hit['name']}」",
                    "law_name": hit["name"], "law_enforce_date": hit["edate"],
                    "diff_days": diff}


# ============================================================
# SQLite DB
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ordinances (
            mst TEXT PRIMARY KEY, lid TEXT, name TEXT NOT NULL,
            knd TEXT, org TEXT, promulg_date TEXT, promulg_no TEXT,
            enforce_date TEXT, field TEXT, status TEXT, detail_url TEXT,
            body_xml TEXT, reason_text TEXT, revision_text TEXT,
            body_fetched_at TEXT, collected_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_knd ON ordinances(knd)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edate ON ordinances(enforce_date)")
    # 담당과(부서) 컬럼 — 본문 API에만 있어 별도 백필로 채운다(기존 DB 호환)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ordinances)")]
    if "dept" not in cols:
        conn.execute("ALTER TABLE ordinances ADD COLUMN dept TEXT")
    conn.commit()
    conn.close()


def upsert_ordinance(item):
    """목록 메타 적재. 본문 컬럼(body_xml/dept/이유/개정문)은 건드리지 않음.

    INSERT OR REPLACE는 행을 통째로 덮어 본문을 날린다 → ON CONFLICT DO UPDATE로
    목록 컬럼만 갱신해 재수집 시에도 이미 받은 본문·담당과를 보존(이어받기).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO ordinances
        (mst, lid, name, knd, org, promulg_date, promulg_no, enforce_date,
         field, status, detail_url, collected_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(mst) DO UPDATE SET
          lid=excluded.lid, name=excluded.name, knd=excluded.knd, org=excluded.org,
          promulg_date=excluded.promulg_date, promulg_no=excluded.promulg_no,
          enforce_date=excluded.enforce_date, field=excluded.field,
          status=excluded.status, detail_url=excluded.detail_url,
          collected_at=excluded.collected_at
    """, (item["MST"], item["자치법규ID"], item["자치법규명"],
          item["자치법규종류"], item["지자체기관명"],
          item["공포일자"], item["공포번호"], item["시행일자"],
          item["자치법규분야명"], item["제개정구분명"],
          item["자치법규상세링크"], datetime.now().isoformat()))
    conn.commit()
    conn.close()


def collect_all_to_db(with_body=True, sleep=0.15):
    """DB 생성: 1) 목록 메타 적재 → 2) 본문 수집(body_xml·담당과·이유·개정문).

    담당과는 목록 API에 없어 본문에서만 나온다. 그래서 수집 단계에서 본문을 함께
    받아 저장하고 거기서 dept를 채운다(별도 백필 불필요). 본문은 1회만 받고
    재수집 시 이미 받은 건 건너뛴다.
    """
    total_collected = 0
    summary = {}
    for knd_code, knd_label in KND_SEARCH_CODES.items():
        first = search_ordinances_official(knd_code, page=1, display=100)
        if not first or first.get("totalCount", 0) == 0:
            summary[knd_label] = 0
            continue
        total = first["totalCount"]
        pages = (total + 99) // 100
        for it in first["items"]:
            upsert_ordinance(it)
        for p in range(2, pages + 1):
            time.sleep(0.2)
            r = search_ordinances_official(knd_code, page=p, display=100)
            if r and r.get("items"):
                for it in r["items"]:
                    upsert_ordinance(it)
        summary[knd_label] = total
        total_collected += total

    result = {"total": total_collected, "by_kind": summary}
    if with_body:
        result["body"] = collect_bodies(sleep=sleep)
    return result


def collect_bodies(sleep=0.15, force=False):
    """각 자치법규 본문을 받아 body_xml·dept·이유·개정문을 채운다.

    저장은 get_detail_parsed가 담당(이미 body_xml/dept UPDATE 포함). body_fetched_at
    이 있는 건은 건너뛰어 이어받기 가능. force=True면 전건 재수집.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    q = "SELECT mst FROM ordinances"
    if not force:
        q += " WHERE body_fetched_at IS NULL OR body_fetched_at = ''"
    msts = [r["mst"] for r in conn.execute(q + " ORDER BY mst").fetchall()]
    conn.close()

    processed, dept_filled = 0, 0
    for mst in msts:
        parsed = get_detail_parsed(mst)
        processed += 1
        if "error" not in parsed and parsed.get("meta", {}).get("dept"):
            dept_filled += 1
        time.sleep(sleep)
    return {"processed": processed, "dept_filled": dept_filled, "targets": len(msts)}


def query_db(filters=None, limit=100, offset=0):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    where = ["1=1"]
    params = []
    if filters:
        if filters.get("knd"):
            where.append("knd = ?")
            params.append(filters["knd"])
        if filters.get("dept"):
            where.append("dept = ?")
            params.append(filters["dept"])
        if filters.get("status"):
            where.append("status = ?")
            params.append(filters["status"])
        if filters.get("query"):
            where.append("name LIKE ?")
            params.append(f"%{filters['query']}%")
    where_sql = " AND ".join(where)
    count = conn.execute(
        f"SELECT COUNT(*) FROM ordinances WHERE {where_sql}", params
    ).fetchone()[0]
    sql = f"""SELECT * FROM ordinances
              WHERE {where_sql}
              ORDER BY enforce_date ASC
              LIMIT ? OFFSET ?"""
    rows = conn.execute(sql, params + [limit, offset]).fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows], "totalCount": count}


def db_stats():
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM ordinances").fetchone()[0]
    by_knd = dict(conn.execute(
        "SELECT knd, COUNT(*) FROM ordinances GROUP BY knd"
    ).fetchall())
    by_status = dict(conn.execute(
        "SELECT status, COUNT(*) FROM ordinances GROUP BY status"
    ).fetchall())
    oldest = conn.execute(
        "SELECT name, enforce_date FROM ordinances "
        "WHERE knd='조례' ORDER BY enforce_date LIMIT 5"
    ).fetchall()
    conn.close()
    return {
        "total": total, "by_knd": by_knd, "by_status": by_status,
        "oldest": [{"name": n, "date": d} for n, d in oldest],
    }


def distinct_depts():
    """채워진 담당과 목록 + 건수 (검색 드롭다운용)."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT dept, COUNT(*) FROM ordinances "
        "WHERE dept IS NOT NULL AND dept != '' GROUP BY dept ORDER BY dept"
    ).fetchall()
    filled = conn.execute(
        "SELECT COUNT(*) FROM ordinances WHERE dept IS NOT NULL AND dept != ''"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM ordinances").fetchone()[0]
    conn.close()
    return {"depts": [{"name": d, "count": c} for d, c in rows],
            "filled": filled, "total": total}


# ============================================================
# HTTP 핸들러
# ============================================================
class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  [{self.command}] {args[0] if args else ''}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self.serve_file("gunpo_ui_v2.html", "text/html")
        elif path.startswith("/api/"):
            self.handle_api()
        else:
            super().do_GET()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def serve_file(self, filename, content_type):
        fpath = Path(__file__).parent / filename
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, f"{filename} not found")

    def _send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def handle_api(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))
        result = {}
        try:
            if path == "/api/search":
                stats = db_stats()
                if stats["total"] == 0:
                    knd_code = params.get("knd", "30001")
                    page = int(params.get("page", "1"))
                    display = int(params.get("display", "20"))
                    result = search_ordinances_official(knd_code, page, display)
                else:
                    page = int(params.get("page", "1"))
                    display = int(params.get("display", "20"))
                    filters = {
                        "query": params.get("query", "").strip() or None,
                        "knd": params.get("knd_label", "").strip() or None,
                        "dept": params.get("dept", "").strip() or None,
                    }
                    filters = {k: v for k, v in filters.items() if v}
                    db_result = query_db(filters, limit=display,
                                         offset=(page - 1) * display)
                    items = []
                    for r in db_result["items"]:
                        items.append({
                            "MST": r["mst"],
                            "자치법규ID": r["lid"] or "",
                            "자치법규명": r["name"],
                            "자치법규종류": r["knd"] or "",
                            "지자체기관명": r["org"] or "",
                            "공포일자": r["promulg_date"] or "",
                            "공포번호": r["promulg_no"] or "",
                            "시행일자": r["enforce_date"] or "",
                            "제개정구분명": r["status"] or "",
                            "자치법규분야명": r["field"] or "",
                            "담당과": r["dept"] or "",
                            "자치법규상세링크": r["detail_url"] or "",
                        })
                    result = {"items": items, "totalCount": db_result["totalCount"]}

            elif path == "/api/collect":
                result = collect_all_to_db()

            elif path == "/api/detail":
                mst = params.get("mst") or params.get("id", "")
                result = get_detail_parsed(mst)
                if isinstance(result, dict) and "error" not in result:
                    result["ref_spans"] = ref_spans_for_articles(
                        result.get("articles", []))

            elif path == "/api/extract_refs":
                mst = params.get("mst") or params.get("id", "")
                parsed_body = get_detail_parsed(mst)
                if "error" in parsed_body:
                    result = parsed_body
                else:
                    result = extract_all_refs(parsed_body)
                    result["meta"] = parsed_body.get("meta", {})
                    result["reason_text"] = parsed_body.get("reason_text", "")
                    result["revision_text"] = parsed_body.get("revision_text", "")

            elif path == "/api/search_law":
                q = params.get("query", "").strip()
                target = params.get("target", "law")
                hit = search_law_first(q, target)
                result = {"hit": hit} if hit else {"hit": None}

            elif path == "/api/check_freshness":
                ord_date = params.get("ord_date", "")
                ref_name = params.get("ref_name", "")
                ref_type = params.get("ref_type", "법령")
                result = check_freshness(ord_date, ref_name, ref_type)

            elif path == "/api/recommend":
                # 사전 배치(gunpolaw.db)에서 이 조례의 개정 권고 뷰(조례 조문 → 상위법 변경) 반환
                mst = (params.get("mst") or params.get("id", "")).strip()
                if _recommend_fragment is None:
                    result = {"error": "권고 뷰 비활성(gunpolaw 패키지/배치 필요)"}
                elif not mst:
                    result = {"error": "mst 필요"}
                else:
                    result = _recommend_fragment(mst)

            # ----- Phase 6 -----
            elif path == "/api/check_clause":
                result = phase6.check_clause_freshness(
                    params.get("ord_date", ""),
                    params.get("law_name", ""),
                    params.get("clause", ""),
                    call_law_api, search_law_first, cache_get, cache_set
                )

            elif path == "/api/check_clause_batch":
                # clauses는 콤마 구분 ("제30조,제31조,제32조")
                clauses = [c.strip() for c in
                           params.get("clauses", "").split(",") if c.strip()]
                result = phase6.check_clauses_batch(
                    params.get("ord_date", ""),
                    params.get("law_name", ""),
                    clauses,
                    call_law_api, search_law_first, cache_get, cache_set
                )

            elif path == "/api/clause_view":
                result = phase6.clause_view(
                    params.get("ord_date", ""),
                    params.get("law_name", ""),
                    params.get("clause", ""),
                    call_law_api, search_law_first, cache_get, cache_set)

            elif path == "/api/old_and_new":
                law_id = params.get("law_id", "").strip()
                if not law_id:
                    # law_name으로 폴백
                    nm = params.get("law_name", "").strip()
                    if nm:
                        law_id = phase6.resolve_law_id(
                            nm, call_law_api, cache_get, cache_set) or ""
                if not law_id:
                    result = {"error": "law_id 또는 law_name 필요"}
                else:
                    result = phase6.get_old_and_new(
                        law_id, call_law_api, cache_get, cache_set
                    ) or {"error": "조회 실패"}

            elif path == "/api/tokenize":
                # 디버그/시연용: 조항 표현 -> 라벨 리스트
                ct = params.get("text", "")
                result = {
                    "input": ct,
                    "tokens": phase6.tokenize_clauses(ct)
                }

            elif path == "/api/depts":
                result = distinct_depts()

            elif path == "/api/stats":
                result = db_stats()

            elif path == "/api/health":
                result = {
                    "status": "ok",
                    "version": "v3.2",
                    "oc": OC[:3] + "***",
                    "cache_size": len(_cache),
                    "db_path": str(DB_PATH),
                    "phase6_loaded": hasattr(phase6, "check_clause_freshness"),
                    "phase6_functions": [
                        n for n in dir(phase6) if not n.startswith("_")
                    ],
                }

            else:
                result = {"error": f"Unknown API path: {path}"}

        except Exception as e:
            import traceback
            traceback.print_exc()
            result = {"error": str(e)}

        self._send_json(result)


# ============================================================
# 서버 시작
# ============================================================
def _port_in_use(port):
    """이미 누군가 듣고 있으면 True (이중 기동 방지용 프리플라이트)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    global OC
    if not OC:
        try:
            OC = input("법제처 OpenAPI OC 키 입력 (환경변수 LAW_OC_KEY 로도 설정 가능): ").strip()
        except EOFError:
            OC = ""
        if not OC:
            print("[!] OC 키가 없어 종료합니다.")
            return
    init_db()
    folder = Path(__file__).parent
    ui_path = folder / "gunpo_ui_v2.html"
    if not ui_path.exists():
        print(f"[!] UI 파일 없음: {ui_path}")
        return

    # 포트 선점 가드: Windows에선 allow_reuse_address로 이중 바인딩이 조용히 되어
    # 구버전 서버가 요청을 가로채는 혼란이 생긴다. 이미 떠 있으면 멈추고 안내한다.
    if _port_in_use(PORT):
        print(f"[!] 포트 {PORT} 가 이미 사용 중입니다 — 다른 서버가 떠 있습니다.")
        print(f"    기존 서버를 종료한 뒤 다시 실행하세요:")
        print(f"      netstat -ano | findstr :{PORT}")
        print(f"      taskkill /F /PID <위에서 본 PID>")
        return

    # 단일 스레드면 외부 API 호출 한 건이 늦어질 때 페이지의 다른 요청까지
    # 전부 줄 서서 먹통이 된다. 요청별 스레드로 분리해 UI 응답성을 보장.
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    stats = db_stats()
    print(f"""
======================================================
  군포시 조례-상위법령 정합성 검토 시스템 v3.2
  Phase 1~4 + Phase 6 (조항 단위 시점 검증)
------------------------------------------------------
  URL       : http://localhost:{PORT}
  API Key   : {OC[:5]}***
  DB        : {DB_PATH.name} (현재 {stats['total']}건)
  Phase 6   : phase6.py 로드 OK
  종료      : Ctrl+C
======================================================
""")
    if stats["total"] == 0:
        print("  [안내] DB가 비어있습니다.")
        print("        브라우저에서 '전수 수집' 버튼을 누르세요.")
    threading.Timer(1.0,
        lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버 종료")
        server.server_close()


if __name__ == "__main__":
    main()
