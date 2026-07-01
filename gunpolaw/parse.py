# -*- coding: utf-8 -*-
"""법령·자치법규 본문 XML 파서 (순수 — 네트워크/OC키 0).

수집(moleg API)과 오프라인 재파싱(DB의 body_xml)이 같은 파서를 공유한다. 배포 제품
(serve/report)은 DB만 읽으므로 이 모듈만 쓰고 moleg(API 클라이언트)는 불러오지 않는다.
"""
import re
import xml.etree.ElementTree as ET

from .clauses import to_label

# 행정규칙(훈령·예규·고시·지침 등)은 법령(target=law)과 별개 저장소지만, 조문 존재·현행
# 판정은 법령과 동일한 파이프라인을 태운다. 구분을 위해 law_id 에 이 접두어를 붙여
# 네임스페이스를 나눈다("ADM:1234567"). 기존 laws/law_articles/findings 테이블을 그대로
# 재사용(스키마 변경 0) — resolve_law_id(법령) 인덱스는 이 접두어를 배제한다.
ADM_PREFIX = "ADM:"


def is_admrul_id(law_id):
    return str(law_id or "").startswith(ADM_PREFIX)


# 담당부서명이 여러 과로 오는 경우('건설과, 위생자원과, 지역경제과') 구분자.
_DEPT_SPLIT = re.compile(r"\s*[,，、/;]\s*")


def primary_dept(s):
    """담당부서명이 여러 과면 맨 앞(대표과)만 취한다.

    '건설과, 위생자원과, 지역경제과' → '건설과'. 과별 리포트·대시보드 그룹핑이
    전체 문자열을 한 과로 오인하지 않도록 라우팅 축을 대표과로 좁힌다.
    """
    s = (s or "").strip()
    if not s:
        return ""
    return _DEPT_SPLIT.split(s)[0].strip() or s


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
# 자치법규 본문
# ------------------------------------------------------------------
def parse_ordinance_body(xml):
    """자치법규 본문 XML -> {meta, articles, full_text, xml}.

    수집(라이브)과 오프라인 재파싱(DB의 body_xml)이 같은 파서를 쓰도록 분리.
    """
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
        "dept": primary_dept(info.findtext("담당부서명")),
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
# 법령 본문
# ------------------------------------------------------------------
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


def law_name_of(xml):
    """법령 본문 XML의 현행 법령명(<법령명_한글>). 제명변경 탐지용. 없으면 ''."""
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    el = root.find(".//법령명_한글")
    return (el.text or "").strip() if el is not None else ""


def law_version_key(xml):
    """법령 본문 XML → 버전 식별 정보 dict. 공포일자+공포번호가 그 법령의 한 '버전'을
    유일하게 식별한다(개정되면 새 공포일자/번호 발급). 시행일자는 그 개정의 효력일.
    없으면 빈 dict."""
    if not xml:
        return {}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    bi = root.find(".//기본정보")
    if bi is None:
        return {}
    g = lambda t: (bi.findtext(t) or "").strip()
    return {"promulg_date": g("공포일자"), "promulg_no": g("공포번호"),
            "enforce_date": g("시행일자"), "revise_type": g("제개정구분"),
            "name": g("법령명_한글")}


def law_version_sig(xml):
    """버전 비교용 시그니처 — '공포일자|공포번호'. 스냅샷 간 이 값이 바뀌면 개정."""
    k = law_version_key(xml)
    if not k:
        return ""
    return f"{k.get('promulg_date', '')}|{k.get('promulg_no', '')}"


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


# ------------------------------------------------------------------
# 행정규칙 본문 (target=admrul)
# ------------------------------------------------------------------
# 행정규칙 XML은 법령(조문단위)과 자치법규(조문/조) 두 표기가 섞여 있고, 조문 구조 없이
# 조문내용을 한 덩어리로 주는 자료도 있다. 세 전략을 순서대로 시도해 {라벨: 조문메타}로
# 정규화한다(법령 파서와 동일한 dict 모양 → checks.check_clause 를 그대로 재사용).
_JO_HEAD_RE = re.compile(r"제(\d+)조(?:의(\d+))?")


def _admrul_arts_from_flat(root):
    """조문 구조가 없는 행정규칙: 조문내용 텍스트를 '제N조' 머리 경계로 분해.

    각 조는 다음 '제N조' 머리 전까지를 자기 본문으로 가진다(조별 <개정> 태그가
    엉뚱한 조에 붙지 않도록). 라벨 파싱만 되면 존재·현행 판정엔 충분.
    """
    chunks = [(el.text or "").strip()
              for el in root.iter("조문내용") if (el.text or "").strip()]
    if not chunks:
        # 조문내용 태그조차 없으면 본문 전체 텍스트를 한 덩어리로
        whole = "\n".join(t.strip() for t in root.itertext() if t.strip())
        chunks = [whole] if whole else []
    arts, cur = {}, None
    for text in chunks:
        for line in text.split("\n"):
            m = _JO_HEAD_RE.match(line.strip())
            if m:
                label = to_label(int(m.group(1)), int(m.group(2) or 0))
                cur = arts.setdefault(label, {
                    "label": label, "enforce_date": "", "moved_from": "",
                    "moved_to": "", "changed": False, "title": "", "_lines": []})
            if cur is not None:
                cur["_lines"].append(line)
    for a in arts.values():
        a["content"] = "\n".join(a.pop("_lines")).strip()
    return arts


def parse_admrul_articles(xml):
    """행정규칙 본문 XML -> {라벨: 조문메타}. 법령/자치법규/평문 표기를 모두 흡수.

    법령 파서(parse_law_articles)와 같은 dict 모양을 돌려주므로 존재·현행 판정은
    checks.check_clause 를 그대로 태운다. 못 파싱하면 빈 dict(→ 인용 조문 '부재'로 판정).
    """
    if not xml:
        return {}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    # 1) 법령식(조문단위) 우선
    arts = parse_law_articles(xml)
    if arts:
        return arts
    # 2) 자치법규식(조문/조)
    for jo in root.findall(".//조문/조"):
        if (jo.findtext("조문여부") or "").strip() and \
                (jo.findtext("조문여부") or "").strip() != "Y":
            continue
        label = decode_article_code(jo.findtext("조문번호"))
        if not label:
            m = _JO_HEAD_RE.match((jo.findtext("조내용") or "").strip())
            if m:
                label = to_label(int(m.group(1)), int(m.group(2) or 0))
        if not label:
            continue
        arts[label] = {
            "label": label, "enforce_date": "", "moved_from": "", "moved_to": "",
            "changed": False, "title": (jo.findtext("조제목") or "").strip(),
            "content": (jo.findtext("조내용") or "").strip()}
    if arts:
        return arts
    # 3) 조문 구조 없는 평문 폴백
    return _admrul_arts_from_flat(root)


def admrul_name_of(xml):
    """행정규칙 본문 XML의 현행 행정규칙명(<행정규칙명>). 없으면 ''."""
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    for tag in ("행정규칙명", "법령명_한글"):
        el = root.find(".//" + tag)
        if el is not None and (el.text or "").strip():
            return el.text.strip()
    return ""
