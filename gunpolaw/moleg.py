# -*- coding: utf-8 -*-
"""법제처 OpenAPI 클라이언트 (네트워크/OC키 경계).

모든 라이브 API 호출은 이 모듈로만 흐른다(call). OC 키는 환경변수(LAW_OC_KEY).
본문 XML 파싱은 순수 모듈 parse.py 로 분리 — 배포 제품(serve/report)은 parse 만 쓰고
이 모듈(네트워크 의존)은 불러오지 않는다. '수집'(batch/pipeline/history)만 이 모듈을 쓴다.
"""
import os
import ssl
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .parse import parse_ordinance_body

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
    """자치법규 본문 -> {meta, articles, full_text}. (라이브: API 호출 후 parse)"""
    xml = call("lawService.do", {"target": "ordin", "MST": str(mst), "type": "XML"})
    return parse_ordinance_body(xml)


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
