# -*- coding: utf-8 -*-
"""법제처 OpenAPI 클라이언트 (정비 이식).

legacy file1/phase6의 검증된 호출·파싱을 정리. OC 키는 환경변수만(LAW_OC_KEY).
조문이동 코드(예 '003000')를 라벨('제30조')로 **decode** 하여 저장 — legacy 버그 수정.
"""
import os
import ssl
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .clauses import to_label

OC = os.environ.get("LAW_OC_KEY", "").strip()

_ssl = ssl.create_default_context()
_ssl.check_hostname = False
_ssl.verify_mode = ssl.CERT_NONE


def _require_oc():
    if not OC:
        raise RuntimeError("OC 키 없음 — 환경변수 LAW_OC_KEY 를 설정하세요.")


def call(endpoint, params):
    """DRF 호출 -> 응답 문자열(없으면 None)."""
    _require_oc()
    params = {**params, "OC": OC}
    qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    for scheme in ("http", "https"):
        url = f"{scheme}://www.law.go.kr/DRF/{endpoint}?{qs}"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (GunpoLaw/0.1)"})
            ctx = _ssl if scheme == "https" else None
            raw = urllib.request.urlopen(req, timeout=20, context=ctx).read()
            for enc in ("utf-8", "euc-kr", "cp949"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="replace")
        except Exception as e:
            last = e
    print(f"  [API FAIL] {endpoint} -> {last}")
    return None


# ------------------------------------------------------------------
# 조문번호 코드 디코딩
# ------------------------------------------------------------------
def decode_article_code(code):
    """'003000' -> '제30조', '001502' -> '제15조의2', 'null/000000' -> ''."""
    code = (code or "").strip()
    if len(code) < 6 or not code.isdigit():
        return ""
    main, sub = int(code[:4]), int(code[4:6])
    if main == 0:
        return ""
    return to_label(main, sub)


# ------------------------------------------------------------------
# 자치법규 (target=ordin)
# ------------------------------------------------------------------
def search_ordinances(org, sborg, knd, page=1, display=100):
    xml = call("lawSearch.do", {
        "target": "ordin", "type": "XML", "nw": "1",
        "org": org, "sborg": sborg, "knd": knd,
        "display": str(display), "page": str(page), "sort": "efasc",
    })
    if not xml:
        return {"items": [], "totalCount": 0}
    root = ET.fromstring(xml)
    tc = root.findtext(".//totalCnt")
    items = []
    for e in root:
        if len(list(e)) == 0:
            continue
        mst = (e.findtext("자치법규일련번호") or "").strip()
        if mst:
            items.append({
                "mst": mst,
                "lid": (e.findtext("자치법규ID") or "").strip(),
                "name": (e.findtext("자치법규명") or "").strip(),
                "knd": (e.findtext("자치법규종류") or "").strip(),
                "enforce_date": (e.findtext("시행일자") or "").strip(),
                "promulg_date": (e.findtext("공포일자") or "").strip(),
            })
    return {"items": items, "totalCount": int(tc) if tc and tc.isdigit() else 0}


def get_ordinance_body(mst):
    """자치법규 본문 -> {meta, articles, full_text}."""
    xml = call("lawService.do", {"target": "ordin", "MST": str(mst), "type": "XML"})
    if not xml:
        return {"error": "본문 조회 실패"}
    root = ET.fromstring(xml)
    info = root.find("자치법규기본정보")
    if info is None:
        return {"error": (root.text or "본문 정보 없음").strip()}
    meta = {
        "mst": (info.findtext("자치법규일련번호") or "").strip(),
        "name": (info.findtext("자치법규명") or "").strip(),
        "enforce_date": (info.findtext("시행일자") or "").strip(),
        "promulg_date": (info.findtext("공포일자") or "").strip(),
        # 담당과(담당부서명)·전화번호는 목록 API엔 없고 본문에만 있다 → 여기서 추출해 영속화.
        # dept = 과별 리포트 라우팅 축, phone = 통지 연락처.
        "dept": (info.findtext("담당부서명") or "").strip(),
        "phone": (info.findtext("전화번호") or "").strip(),
    }
    articles = []
    for jo in root.findall(".//조문/조"):
        articles.append({
            "no": decode_article_code(jo.findtext("조문번호")),
            "is_article": (jo.findtext("조문여부") or "").strip() == "Y",
            "title": (jo.findtext("조제목") or "").strip(),
            "body": (jo.findtext("조내용") or "").strip(),
        })
    parts = [a["body"] for a in articles if a["body"]]
    for tag in (".//개정문내용", ".//제개정이유내용"):
        el = root.find(tag)
        if el is not None:
            parts.append("\n".join(t.strip() for t in el.itertext() if t.strip()))
    # xml(원본)도 함께 반환 — 배치가 ordinances.body_xml 로 영속(서빙은 DB만 읽음).
    return {"meta": meta, "articles": articles, "full_text": "\n".join(parts), "xml": xml}


# ------------------------------------------------------------------
# 조례 ↔ 법령 공식 연계 (lnkOrg) — 법령ID 직접
# ------------------------------------------------------------------
def get_org_links(org, max_pages=10):
    """지자체 org 코드 -> [{mst, name, law_id, law_name}, ...] (전 페이지)."""
    out = []
    for page in range(1, max_pages + 1):
        xml = call("lawSearch.do", {
            "target": "lnkOrg", "type": "XML", "org": org,
            "display": "100", "page": str(page)})
        if not xml:
            break
        root = ET.fromstring(xml)
        rows = [e for e in root if len(list(e)) > 0 and e.findtext("자치법규일련번호")]
        if not rows:
            break
        for e in rows:
            out.append({
                "mst": (e.findtext("자치법규일련번호") or "").strip(),
                "name": (e.findtext("자치법규명") or "").strip(),
                "law_id": (e.findtext("법령ID") or "").strip(),
                "law_name": (e.findtext("법령명한글") or "").strip(),
            })
        total = root.findtext(".//totalCnt")
        if total and total.isdigit() and len(out) >= int(total):
            break
        time.sleep(0.1)
    return out


# ------------------------------------------------------------------
# 법령 (target=law)
# ------------------------------------------------------------------
def resolve_law_id(law_name):
    """법령명 -> 법령ID (정확매칭 우선, 없으면 첫 결과)."""
    xml = call("lawSearch.do", {
        "target": "law", "type": "XML", "query": law_name, "display": "5"})
    if not xml:
        return None
    root = ET.fromstring(xml)
    qn = law_name.replace(" ", "")
    first = None
    for e in root:
        if len(list(e)) == 0:
            continue
        nm = (e.findtext("법령명한글") or "").strip()
        lid = (e.findtext("법령ID") or "").strip()
        if not lid:
            continue
        first = first or lid
        if nm.replace(" ", "") == qn:
            return lid
    return first


def get_law_body(law_id):
    return call("lawService.do", {"target": "law", "type": "XML", "ID": str(law_id)})


# 조문 본문을 이루는 하위 단위와 그 내용 태그. 호/목까지 담아야 '정의' 같은
# 호 구조 조문의 실제 내용이 보존된다(머리글만 남으면 diff가 개정날짜만 보임).
_SUBUNIT_CONTENT = {"항": "항내용", "호": "호내용", "목": "목내용"}


def _collect_subunits(el, parts):
    """el(조문단위/항/호) 아래의 항·호·목 내용을 문서 순서대로 parts에 모은다(재귀)."""
    for child in el:
        tag = child.tag
        if tag in _SUBUNIT_CONTENT:
            txt = (child.findtext(_SUBUNIT_CONTENT[tag]) or "").strip()
            if txt:
                parts.append(txt)
            _collect_subunits(child, parts)   # 항>호, 호>목 중첩까지


def article_text(u):
    """조문단위 -> 조문내용 + 항·호·목 내용을 줄바꿈으로 이은 전체 본문.

    기존엔 조문내용·항내용만 담아 호(號)로 된 정의·열거 조문이 머리글만 남았다.
    그 결과 당시/현행 diff에 비교할 알맹이가 없어 <개정 …> 날짜만 차이로 보였다.
    """
    parts = [(u.findtext("조문내용") or "").strip()]
    _collect_subunits(u, parts)
    return "\n".join(p for p in parts if p)


def parse_law_articles(xml):
    """법령 본문 XML -> {라벨: 조문메타}. 조문이동 코드는 라벨로 decode."""
    if not xml:
        return {}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    arts = {}
    for u in root.findall(".//조문단위"):
        if (u.findtext("조문여부") or "").strip() != "조문":
            continue
        jo_num = (u.findtext("조문번호") or "").strip()
        if not jo_num:
            continue
        # 가지조문(제58조의2) 가지번호는 <조문가지번호>('2'). 키가 비는 경우가 많아
        # 과거 <조문키> 추정은 의N을 본조로 뭉갰음 → 조문가지번호 우선.
        ga_raw = (u.findtext("조문가지번호") or "").strip()
        if ga_raw.isdigit():
            ga = int(ga_raw)
        else:
            key = (u.findtext("조문키") or "").strip()
            try:
                ga = max(int(key[-3:]) - 1, 0) if key else 0
            except ValueError:
                ga = 0
        label = to_label(int(jo_num), ga)
        arts[label] = {
            "label": label,
            "enforce_date": (u.findtext("조문시행일자") or "").strip(),
            "moved_from": decode_article_code(u.findtext("조문이동이전")),
            "moved_to": decode_article_code(u.findtext("조문이동이후")),
            "changed": (u.findtext("조문변경여부") or "").strip() == "Y",
            "title": (u.findtext("조문제목") or "").strip(),
            "content": article_text(u),
        }
    return arts
