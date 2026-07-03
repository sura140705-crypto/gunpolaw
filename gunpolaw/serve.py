# -*- coding: utf-8 -*-
"""3단계: SERVE — 읽기전용 총괄 대시보드.

원칙(시스템_설계.md §5): **배치본 DB만 SELECT한다. 라이브 API 호출 0.**
배치(`--batch`)가 채워둔 gunpolaw.db를 그대로 서빙하므로 OC 키도 불필요.

엔드포인트(전부 GET):
  /                     총괄 대시보드(단일 HTML)
  /api/overview         batch_meta + 등급 집계 + 담당과별 집계
  /api/ordinances?dept=&action=1   담당과/정비대상 필터 조례 목록
  /api/ordinance/<mst>  조례 1건 상세(메타 + 권고 조각)

실행:  python -m gunpolaw --serve [포트]   (기본 8765)
"""
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

from . import db
from .parse import parse_ordinance_body
from .extract import normalize_text, is_local_admrul
from .report import (GRADE_KEYS, GRADE_META, finding_grade, recommend_fragment,
                     build_model, render_html, model_to_csv, grade_to_csv)


# ---------- DB 읽기(읽기전용 집계) ----------
def _scan(db_path):
    """조례 전건 × findings → 조례별 등급 분포. (현행만인 조례도 포함)

    반환: [{mst, name, dept, enforce_date, grades:{등급:수}, items_count}, …]
    items_count = current 아닌 정비 항목 수.
    """
    conn = db.connect(db_path)
    fcols = [r[1] for r in conn.execute("PRAGMA table_info(findings)")]
    naked = "f.cite_naked" if "cite_naked" in fcols else "0"
    spacing = "f.cite_spacing" if "cite_spacing" in fcols else "0"
    rows = conn.execute(
        f"""SELECT o.mst, o.name, o.dept, o.enforce_date,
                   f.severity, f.change_type, {naked} AS cite_naked,
                   {spacing} AS cite_spacing
            FROM ordinances o
            LEFT JOIN findings f ON f.mst = o.mst""").fetchall()
    conn.close()

    by = {}
    for r in rows:
        o = by.setdefault(r["mst"], {
            "mst": r["mst"],
            "name": r["name"] or "(조례명 미상)",
            "dept": r["dept"] or "(미지정)",
            "enforce_date": r["enforce_date"] or "",
            "grades": {k: 0 for k in GRADE_KEYS},
            "items_count": 0,
        })
        if r["severity"] is None:        # 인용/판정이 없는 조례
            continue
        g = finding_grade(r["severity"], r["change_type"], r["cite_naked"], r["cite_spacing"])
        o["grades"][g] += 1
        if g != "current":
            o["items_count"] += 1
    return list(by.values())


# 소관업무별(편) 분류 — ELIS 자치법규 소관업무별 분류 + 군포시 행정조직도 기준.
# 담당과를 실·국(편) 아래로 묶어 공식 조직 순서로 표출한다(정비량 정렬 대신 조직 체계).
# region_name 으로 매칭 — 정의 없는 지자체는 None 반환 → 기존 평면(정비량순) 폴백.
# 타 시군은 여기 자기 조직표를 추가하면 동일 동작(추후 config/DB화 여지).
DEPT_GROUPS_BY_REGION = {
    "군포시": [
        ("01", "의회", ["의회사무과"]),
        ("02", "기획예산실", ["기획예산실"]),
        ("03", "홍보실", ["홍보실"]),
        ("04", "감사실", ["감사실"]),
        ("05", "행정지원국", ["행정지원과", "자치분권과", "민원봉사과",
                              "스마트정보과", "교육체육과", "문화예술과"]),
        ("06", "기업재정국", ["기업정책과", "지역경제과", "회계과", "세정과", "세원관리과"]),
        ("07", "도시주택국", ["도시계획과", "도시개발과", "주택정책과", "건설과", "건축과"]),
        ("08", "안전환경국", ["안전총괄과", "환경과", "위생자원과", "교통행정과", "차량관리과"]),
        ("09", "복지국", ["복지정책과", "노인장애인과", "여성가족과", "아동청소년과",
                          "중앙도서관", "산본도서관"]),
        ("10", "직속기관(보건소)", ["보건행정과", "산본보건지소"]),
        ("11", "사업소(수도녹지)", ["수도과", "하수과", "생태공원녹지과"]),
        ("12", "하부행정기관", ["도시환경과", "군포1동", "군포2동", "산본1동", "산본2동",
                                "금정동", "재궁동", "오금동", "수리동", "궁내동",
                                "광정동", "대야동", "송부동"]),
    ],
}


def _dept_groups(region_name, depts_by_name):
    """담당과 집계를 소관업무별(편) 구조로 묶는다. 정의 없으면 None(→ 평면 폴백).

    각 편은 정의된 과 순서를 따르고, 데이터 있는 과만 포함한다(편 전체가 비면 편 생략).
    어느 편에도 안 든 과는 말미 '기타'로 모은다.
    """
    spec = DEPT_GROUPS_BY_REGION.get((region_name or "").strip())
    if not spec:
        return None
    used, groups = set(), []
    for no, name, members in spec:
        rows = [depts_by_name[m] for m in members if m in depts_by_name]
        used.update(m for m in members if m in depts_by_name)
        if not rows:
            continue
        groups.append({
            "no": no, "name": name,
            "total": sum(r["total"] for r in rows),
            "action": sum(r["action"] for r in rows),
            "grades": {k: sum(r["grades"][k] for r in rows) for k in GRADE_KEYS},
            "depts": rows})
    etc = [d for nm, d in depts_by_name.items() if nm not in used]
    if etc:
        etc.sort(key=lambda d: (-d["action"], d["dept"]))
        groups.append({
            "no": "", "name": "기타",
            "total": sum(d["total"] for d in etc),
            "action": sum(d["action"] for d in etc),
            "grades": {k: sum(d["grades"][k] for d in etc) for k in GRADE_KEYS},
            "depts": etc})
    return groups


def overview(db_path=db.DEFAULT_DB):
    """대시보드 요약: 스냅샷 메타 + 전체 등급 집계 + 담당과별 집계."""
    conn = db.connect(db_path)
    meta = conn.execute("SELECT * FROM batch_meta WHERE id=1").fetchone()
    conn.close()

    ords = _scan(db_path)
    grades = {k: 0 for k in GRADE_KEYS}
    depts = {}
    for o in ords:
        for k in GRADE_KEYS:
            grades[k] += o["grades"][k]
        d = depts.setdefault(o["dept"], {
            "dept": o["dept"], "total": 0, "action": 0,
            "grades": {k: 0 for k in GRADE_KEYS}})
        d["total"] += 1
        if o["items_count"] > 0:
            d["action"] += 1
        for k in GRADE_KEYS:
            d["grades"][k] += o["grades"][k]

    # 정비 대상이 많은 과를 위로(평면 폴백·드롭다운·검색용). 소관업무별 편 구조는 별도.
    dept_list = sorted(depts.values(), key=lambda d: (-d["action"], d["dept"]))
    region = (dict(meta).get("region_name") if meta else "") or ""
    return {
        "batch": (dict(meta) if meta else {}),
        "grades": grades,
        "grade_meta": {k: {"emoji": GRADE_META[k]["emoji"],
                           "label": GRADE_META[k]["label"],
                           "desc": GRADE_META[k].get("desc", "")} for k in GRADE_KEYS},
        "grade_order": list(GRADE_KEYS),
        "totals": {"ordinances": len(ords),
                   "action": sum(1 for o in ords if o["items_count"] > 0),
                   "depts": len(dept_list)},
        "depts": dept_list,
        # 소관업무별(편) 그룹 — 정의된 지자체면 조직 순서 구조, 아니면 None(평면 폴백)
        "dept_groups": _dept_groups(region, depts),
    }


def list_ordinances(db_path=db.DEFAULT_DB, dept=None, action_only=False, q=None):
    """담당과/정비대상 필터 조례 목록. 정비 항목 많은 순 → 이름 순.

    q(자유검색): 조례명 또는 '인용한 상위법령명'에 q가 포함된 조례. 담당과는 무시(교차검색).
    인용 법령으로 걸린 경우 law_hits 에 매치된 법령명을 담아 표시한다.
    """
    ords = _scan(db_path)
    q = (q or "").strip()
    if q:
        qn = q.replace(" ", "")
        conn = db.connect(db_path)
        law_hits = {}                      # mst -> [매치된 인용 법령명…]
        for r in conn.execute(
                "SELECT DISTINCT mst, law_name FROM citations WHERE law_name LIKE ?",
                (f"%{q}%",)):
            law_hits.setdefault(r["mst"], []).append(r["law_name"])
        conn.close()
        sel = []
        for o in ords:
            in_name = qn in (o["name"] or "").replace(" ", "")
            hits = law_hits.get(o["mst"], [])
            if in_name or hits:
                sel.append({**o, "law_hits": hits if not in_name else []})
        ords = sel
    elif dept:
        ords = [o for o in ords if o["dept"] == dept]
    if action_only:
        ords = [o for o in ords if o["items_count"] > 0]
    ords.sort(key=lambda o: (-o["items_count"], o["name"]))
    return ords


def _esc(s):
    return html.escape(str(s or ""))


def _cite_actionable(article_no, cite, actionable):
    """이 인용이 정비 대상인가. 인용의 clause_label 은 '제148조,제149조'처럼 여러 조를
    합쳐 오지만 findings 는 조 단위(제148조/제149조 각각)라, 콤마로 쪼개 하나라도
    정비 대상 집합에 있으면 강조 대상으로 본다(조라벨 단위 불일치 방어).
    법령단위 finding(법령미해결·지자체행정규칙 등 clause_label='')은 조라벨과 무관하게
    그 법령 인용 전체가 대상이므로 (조문, 법령, '') 폴백으로도 매칭한다."""
    law = cite.get("law_name") or ""
    parts = [p.strip() for p in (cite.get("clause_label") or "").split(",")] or [""]
    return (any((article_no, law, p) in actionable for p in parts)
            or (article_no, law, "") in actionable)


def _highlight_article(no, body, cites, actionable=None):
    """조례 조문 본문(정규화)에 인용 span을 <mark>로 감싼 HTML.

    span은 normalize_text(조문본문) 기준 offset이므로 같은 정규화 텍스트에 적용한다.
    겹치거나 범위를 벗어난 span은 건너뛴다(안전).
    actionable(set) 지정 시 그 안의 (조문, 법령, 조라벨) 인용은 정비 대상으로 색칠·강조,
    나머지 인용(변경 없음/현행)은 옅은 밑줄로 표기 — '인식·검토했고 변경 없음'을 드러낸다
    (검토 흔적). None이면 전부 정비 대상처럼 강조.
    """
    text = normalize_text(body)
    spans = sorted((c for c in cites),
                   key=lambda c: (c["span_start"] or 0, c["span_end"] or 0))
    out, pos, n = [], 0, len(text)
    for c in spans:
        s, e = c["span_start"] or 0, c["span_end"] or 0
        if s < pos or e > n or s >= e:      # 겹침/이상치 방어
            continue
        out.append(_esc(text[pos:s]))
        act = actionable is None or _cite_actionable(no, c, actionable)
        # 타 조례(자치법규)는 상위법령 정합성 검토 대상이 아니라 회색으로 구분(우측 검토에 없음)
        if not act and c.get("cite_type") != "자치법규":
            cls = "cite-current"           # 변경 없음 — 옅은 밑줄만(검토함 표시)
        elif c.get("cite_type") == "자치법규":
            cls = "cite-local"
        elif c.get("cite_naked"):
            cls = "cite-naked"
        else:
            cls = "cite-law"
        out.append(
            f'<mark class="{cls}" tabindex="0" data-oc="{_esc(no)}" data-law="{_esc(c["law_name"])}"'
            f' data-clause="{_esc(c["clause_label"])}">{_esc(text[s:e])}</mark>')
        pos = e
    out.append(_esc(text[pos:]))
    return "".join(out)


def ordinance_detail(db_path=db.DEFAULT_DB, mst=None):
    """조례 1건 상세(통합 뷰용): 메타 + 본문(인용 하이라이트) + 권고 조각.

    좌(본문 하이라이트)·우(권고)를 한 응답에. 라이브 API 0 — 본문은 ordinances.body_xml,
    하이라이트는 citations(span)에서 서빙.
    """
    conn = db.connect(db_path)
    o = conn.execute(
        """SELECT mst, name, dept, phone, knd, promulg_date, enforce_date, body_xml
           FROM ordinances WHERE mst = ?""", (str(mst),)).fetchone()
    cits = conn.execute(
        """SELECT article_no, law_name, law_id, clause_label, span_start, span_end,
                  cite_naked, cite_type FROM citations WHERE mst = ?""",
        (str(mst),)).fetchall()
    # 타 조례(자치법규) 인용 → 우리 DB의 해당 조례로 해소(이름 매칭, 공백 무시).
    # 좌측 회색 인용 클릭 시 우측에 그 조례 미니 정보를 띄우기 위함. 키=공백 제거 법령명.
    local_refs = {}
    for c in cits:
        if c["cite_type"] == "자치법규" and c["law_name"]:
            key = c["law_name"].replace(" ", "")
            if key not in local_refs:
                ref = conn.execute(
                    """SELECT mst, name, dept, enforce_date FROM ordinances
                       WHERE REPLACE(name,' ','') = ? AND mst != ? LIMIT 1""",
                    (key, str(mst))).fetchone()
                local_refs[key] = (dict(ref) if ref else None)

    # 상위법령 정보 카드용 — 인용한 법령별: ID·인용 조문·판정 등급 요약(좌측 클릭 시 우측 표시)
    law_refs = {}
    for c in cits:
        if c["cite_type"] != "법령" or not c["law_name"]:
            continue
        key = c["law_name"].replace(" ", "")
        d = law_refs.setdefault(key, {"name": c["law_name"], "law_id": c["law_id"] or "",
                                      "clauses": [], "counts": {},
                                      # 지자체 자체 행정규칙(국가법령정보 미수록) — 자치법규 포털로 링크
                                      "local_admrul": is_local_admrul(c["law_name"])})
        if not d["law_id"] and c["law_id"]:
            d["law_id"] = c["law_id"]
        cl = (c["clause_label"] or "").strip()
        if cl and cl not in d["clauses"]:
            d["clauses"].append(cl)
    # 정비 대상(판정 등급 current 아님) 인용 키 집합 — 좌측 본문은 이것만 강조.
    # 키 = (조례조문, 법령명, 상위법 조라벨) — mark·검토항목과 동일 매칭축.
    actionable = set()
    for fr in conn.execute(
            """SELECT law_name, severity, change_type, clause_label, ord_clause,
                      cite_naked, cite_spacing FROM findings WHERE mst=?""", (str(mst),)):
        g = finding_grade(fr["severity"], fr["change_type"],
                          fr["cite_naked"] or 0, fr["cite_spacing"] or 0)
        key = (fr["law_name"] or "").replace(" ", "")
        if key in law_refs:
            law_refs[key]["counts"][g] = law_refs[key]["counts"].get(g, 0) + 1
        if g != "current":
            oc = (fr["ord_clause"] or "").split(",")[0].strip()
            actionable.add((oc, fr["law_name"] or "", fr["clause_label"] or ""))
    # 공식 연계(lnkOrg)엔 있으나 본문 인용이 검출되지 않은 법령 = 근거·연계 후보(위임 관계 등)
    cited_ids = {c["law_id"] for c in cits if c["law_id"]}
    linked_refs, _lseen = [], set()
    for r in conn.execute(
            "SELECT law_id, law_name FROM ord_law_links WHERE mst=? AND law_id!='' "
            "ORDER BY law_name", (str(mst),)):
        lid = r["law_id"]
        if lid in cited_ids or lid in _lseen:
            continue
        _lseen.add(lid)
        linked_refs.append({"law_id": lid, "name": r["law_name"] or ""})
    conn.close()

    by_art = {}
    for c in cits:
        by_art.setdefault(c["article_no"] or "", []).append(dict(c))

    articles = []
    if o and o["body_xml"]:
        parsed = parse_ordinance_body(o["body_xml"])
        if "error" not in parsed:
            for a in parsed["articles"]:
                if not a.get("body"):
                    continue
                no = a.get("no") or ""
                ac = by_art.get(no, [])
                act = sum(1 for c in ac if _cite_actionable(no, c, actionable))
                articles.append({
                    "no": no, "title": a.get("title", ""),
                    "html": _highlight_article(no, a["body"], ac, actionable),
                    "cites": len(ac), "act": act,
                })

    # 이 조례의 특징 — 인용 구성(상위법령/행정규칙/타조례 대상 수)
    n_law = n_adm = 0
    for d in law_refs.values():
        if str(d.get("law_id") or "").startswith("ADM:") or d.get("local_admrul"):
            n_adm += 1
        else:
            n_law += 1
    features = {"law": n_law, "admrul": n_adm, "local": len(local_refs)}

    meta = {k: o[k] for k in o.keys() if k != "body_xml"} if o else {}
    return {"meta": meta, "articles": articles, "local_refs": local_refs,
            "law_refs": law_refs, "features": features, "linked_refs": linked_refs,
            "recommend": recommend_fragment(mst, db_path, collapsible=True)}


# ---------- HTTP(읽기전용) ----------
class _Handler(BaseHTTPRequestHandler):
    db_path = db.DEFAULT_DB
    server_version = "gunpolaw-serve/1.0"

    def _send(self, body, status=200, ctype="application/json; charset=utf-8",
              headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, obj, status=200):
        # API 응답도 no-cache — 분석 산출물(본문 하이라이트 등)이 바뀌면 즉시 반영되도록
        # (브라우저가 옛 상세 JSON을 캐시해 '달라진 게 없어 보이는' 현상 방지)
        self._send(json.dumps(obj, ensure_ascii=False),
                   status=status, ctype="application/json; charset=utf-8",
                   headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        try:
            # HTML은 no-cache — 재배포/수정이 브라우저 캐시에 막히지 않도록
            nocache = {"Cache-Control": "no-cache, no-store, must-revalidate"}
            if path == "/" or path == "/index.html":
                return self._send(DASHBOARD_HTML, ctype="text/html; charset=utf-8",
                                  headers=nocache)
            if path == "/admin":
                return self._send(ADMIN_HTML, ctype="text/html; charset=utf-8",
                                  headers=nocache)
            if path == "/api/admin/findings":
                from .diag import list_findings
                q = parse_qs(u.query)
                g = lambda k: (q.get(k) or [None])[0] or None
                return self._json(list_findings(
                    self.db_path, severity=g("severity"),
                    change_type=g("change_type"), q=g("q"),
                    limit=int((q.get("limit") or ["300"])[0])))
            if path == "/api/admin/trace":
                from .diag import trace_finding
                q = parse_qs(u.query)
                fid = (q.get("id") or [None])[0]
                if not fid or not fid.isdigit():
                    return self._json({"error": "id(숫자) 필요"}, status=400)
                return self._json(trace_finding(self.db_path, int(fid)))
            if path == "/api/overview":
                return self._json(overview(self.db_path))
            if path == "/api/ordinances":
                q = parse_qs(u.query)
                dept = (q.get("dept") or [None])[0] or None
                action = (q.get("action") or ["0"])[0] in ("1", "true", "yes")
                query = (q.get("q") or [None])[0] or None
                return self._json(list_ordinances(self.db_path, dept=dept,
                                                  action_only=action, q=query))
            if path.startswith("/api/ordinance/"):
                mst = path.rsplit("/", 1)[-1]
                return self._json(ordinance_detail(self.db_path, mst))
            if path == "/api/changes":
                from .batch import law_changes_report
                q = parse_qs(u.query)
                allf = (q.get("all") or ["0"])[0] in ("1", "true", "yes")
                return self._json(law_changes_report(self.db_path, include_acked=allf))
            if path == "/api/dept_report":
                q = parse_qs(u.query)
                dept = (q.get("dept") or [""])[0]
                fmt = (q.get("format") or ["html"])[0]
                return self._dept_report(dept, fmt)
            if path == "/api/ordinance_report":
                q = parse_qs(u.query)
                mst = (q.get("mst") or [""])[0]
                fmt = (q.get("format") or ["html"])[0]
                return self._ordinance_report(mst, fmt)
            if path == "/api/grade_report":
                q = parse_qs(u.query)
                grade = (q.get("grade") or [""])[0]
                if grade not in GRADE_KEYS or grade == "current":
                    return self._json({"error": "grade(mechanical/review/check/format) 필요"}, status=400)
                fn = quote(f"{GRADE_META[grade]['label']}_정비목록.csv")
                return self._send(grade_to_csv(self.db_path, grade),
                                  ctype="text/csv; charset=utf-8",
                                  headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})
            return self._json({"error": "not found", "path": path}, status=404)
        except Exception as e:  # 서빙은 죽지 않게 — 오류도 JSON으로
            return self._json({"error": type(e).__name__, "detail": str(e)},
                              status=500)

    def do_POST(self):
        """운영자 액션(로컬 DB 쓰기) — 라이브 API·키 불요. 현재 개정 검토완료 표시뿐.

        읽기전용 서빙 원칙은 '라이브 API/재수집 없음'을 뜻하며, 총괄이 자기 DB에 다는
        검토완료 표시는 그 경계를 넘지 않는다(네트워크 0, 키 0).
        """
        u = urlparse(self.path)
        try:
            if u.path == "/api/ack":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw or b"{}")
                law_id = body.get("law_id")
                if not law_id:
                    return self._json({"error": "law_id 필요"}, status=400)
                from .batch import ack_law_change
                n = ack_law_change(law_id, new_key=body.get("new_key"),
                                   acked=bool(body.get("acked", True)),
                                   db_path=self.db_path)
                return self._json({"ok": True, "updated": n})
            return self._json({"error": "not found", "path": u.path}, status=404)
        except Exception as e:
            return self._json({"error": type(e).__name__, "detail": str(e)},
                              status=500)

    def _dept_report(self, dept, fmt):
        """담당과 단위 정비 권고 산출물(통지용) — HTML(인쇄/PDF) 또는 CSV."""
        if not dept:
            return self._json({"error": "dept 필요"}, status=400)
        model = build_model(self.db_path, dept=dept)
        if fmt == "csv":
            fn = quote(f"{dept}_정비권고.csv")
            return self._send(
                model_to_csv(model, dept=dept), ctype="text/csv; charset=utf-8",
                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})
        htmltext = render_html(model, title=f"{dept} 자치법규 정비 권고")
        return self._send(htmltext, ctype="text/html; charset=utf-8")

    def _ordinance_report(self, mst, fmt):
        """조례 1건 분석·정비 권고(담당자 확인용). 현행(변경 없음)까지 포함해 '인용 전건을
        검토했고 어떻게 판정했는지'를 통째로 보여준다 — 로직을 사람이 눈으로 확인."""
        if not mst:
            return self._json({"error": "mst 필요"}, status=400)
        model = build_model(self.db_path, mst=mst, include_current=True)
        name = model["ordinances"][0]["name"] if model["ordinances"] else mst
        if fmt == "csv":
            fn = quote(f"{name}_정비권고.csv")
            return self._send(
                model_to_csv(model), ctype="text/csv; charset=utf-8",
                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})
        htmltext = render_html(model, title=f"{name} — 정비 권고(분석 결과)")
        return self._send(htmltext, ctype="text/html; charset=utf-8")

    do_HEAD = do_GET

    def log_message(self, fmt, *args):   # 콘솔 소음 줄이기(요청 줄만)
        try:
            print(f"  {self.command} {self.path}")
        except Exception:
            pass


def serve(port=8765, db_path=db.DEFAULT_DB, host="127.0.0.1"):
    """읽기전용 대시보드 서버 기동(Ctrl+C 종료)."""
    _Handler.db_path = db_path
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"총괄 대시보드 서빙 → http://{host}:{port}/  (DB={db_path}, 읽기전용)")
    print("  종료: Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        httpd.server_close()
    return 0


# ---------- 대시보드(단일 HTML, 의존성 0) ----------
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>자치법규 정비 — 총괄 대시보드</title>
<style>
:root{--mech:#2563eb;--rev:#d97706;--chk:#6b7280;--fmt:#7c3aed;--cur:#16a34a;}
*{box-sizing:border-box;}
body{font-family:"Malgun Gothic","맑은 고딕",system-ui,sans-serif;color:#1f2937;
     margin:0;background:#f3f4f6;}
.wrap{max-width:1180px;margin:0 auto;padding:24px 22px 64px;}
h1{font-size:22px;margin:0 0 2px;}
.banner{color:#6b7280;font-size:13px;margin-bottom:18px;}
.banner b{color:#374151;}
.cards{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 22px;}
.card{flex:1 1 150px;border:1px solid #e5e7eb;border-radius:10px;padding:13px 15px;
      background:#fff;}
.card .n{font-size:26px;font-weight:700;line-height:1;}
.card .t{font-size:12.5px;color:#6b7280;margin-top:6px;}
.g-mech .n{color:var(--mech);}.g-rev .n{color:var(--rev);}.g-chk .n{color:var(--chk);}
.g-fmt .n{color:var(--fmt);}.g-cur .n{color:var(--cur);}
.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:0 0 14px;}
.controls #searchBox{font-size:14px;padding:6px 12px;border:1px solid #d1d5db;border-radius:8px;
  min-width:260px;outline:none;}
.controls #searchBox:focus{border-color:#2563eb;box-shadow:0 0 0 2px #bfdbfe;}
td .lawhit{display:block;font-size:11.5px;color:#2563eb;margin-top:2px;}
.controls select{font-size:14px;padding:6px 10px;border:1px solid #d1d5db;border-radius:8px;
                 background:#fff;min-width:220px;}
.controls label{font-size:13px;color:#374151;display:flex;gap:5px;align-items:center;}
.controls .crumb{display:inline-flex;align-items:center;gap:10px;font-size:13px;color:#6b7280;flex-wrap:wrap;}
.controls .crumb a{color:#2563eb;cursor:pointer;text-decoration:none;}
/* 뒤로가기 버튼 — 작은 텍스트 링크 대신 또렷한 버튼(클릭 영역 확대) */
.backbtn{font-size:13.5px;padding:7px 16px;border-radius:8px;background:#eff6ff;
  border:1px solid #bfdbfe;color:#1e40af;font-weight:700;cursor:pointer;line-height:1;
  display:inline-flex;align-items:center;}
.backbtn:hover{background:#dbeafe;border-color:#93c5fd;}
/* ← 뒤로: 한 단계 상위 화면으로(주 동작이라 솔리드로 또렷하게) */
.backbtn.upbtn{background:#1e40af;color:#fff;border-color:#1e40af;}
.backbtn.upbtn:hover{background:#1e3a8a;border-color:#1e3a8a;}
/* 🏠 첫페이지: 홈 앵커(보조 동작이라 아웃라인) */
.homebtn{background:#fff;color:#1e40af;border-color:#bfdbfe;}
.homebtn:hover{background:#eff6ff;border-color:#93c5fd;}
.controls .crumb .crumb-cur{color:#374151;font-size:14px;}
.controls .crumb .crumb-link{font-size:12.5px;}
/* 등급 필터 배지(현재 걸린 등급 + × 해제) */
.gradebadge{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;
  color:#fff;border-radius:999px;padding:2px 6px 2px 10px;}
.gradebadge.g-mech{background:var(--mech);}.gradebadge.g-rev{background:var(--rev);}
.gradebadge.g-chk{background:var(--chk);}.gradebadge.g-fmt{background:var(--fmt);}
.gradebadge.g-cur{background:var(--cur);}
.gradebadge .gx{color:#fff;font-weight:700;line-height:1;cursor:pointer;text-decoration:none;
  background:rgba(255,255,255,.25);border-radius:999px;width:16px;height:16px;
  display:inline-flex;align-items:center;justify-content:center;font-size:13px;}
.gradebadge .gx:hover{background:rgba(255,255,255,.45);}
.gradebadge .gdl{color:#fff;font-weight:700;font-size:11px;text-decoration:none;
  background:rgba(255,255,255,.25);border-radius:999px;padding:1px 8px;white-space:nowrap;}
.gradebadge .gdl:hover{background:rgba(255,255,255,.45);}
.layout{display:grid;grid-template-columns:1fr;gap:16px;}
.panel{background:#fff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;}
table{width:100%;border-collapse:collapse;font-size:13.5px;}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid #f1f3f5;}
th{background:#f9fafb;color:#6b7280;font-weight:600;font-size:12px;position:sticky;top:0;}
tbody tr{cursor:pointer;}
tbody tr:hover{background:#f8fafc;}
/* 소관업무별(편) 그룹 헤더(클릭 접이) + 하위 과 들여쓰기 */
tr.pyeon{cursor:pointer;background:#eef2f7;}
tr.pyeon:hover{background:#e2e9f2;}
tr.pyeon.open{background:#e2e9f2;}
tr.pyeon td{font-weight:700;color:#1e3a8a;border-bottom:1px solid #d3ddea;font-size:13px;}
tr.pyeon .parr{display:inline-block;width:14px;color:#64748b;font-size:11px;}
tr.subrow:hover{background:#f8fafc;}
td.subdept{padding-left:24px;position:relative;}
td.subdept::before{content:"└";position:absolute;left:10px;color:#c0cad6;}
td.num{text-align:right;font-variant-numeric:tabular-nums;}
td.rep a{font-size:12px;margin-right:7px;color:#2563eb;text-decoration:none;}
.dist{white-space:nowrap;}
.pill{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;
      margin-right:4px;color:#fff;}
.p-mech{background:var(--mech);}.p-rev{background:var(--rev);}.p-chk{background:var(--chk);}
.p-fmt{background:var(--fmt);}.p-cur{background:#9ca3af;}
.detail{padding:0;}
.detail .dhd{padding:14px 16px;border-bottom:1px solid #eef0f3;}
.detail .dhd h2{font-size:16px;margin:0 0 3px;}
.detail .dhd .m{font-size:12px;color:#6b7280;}
.detail .dfeat{margin-top:10px;display:flex;flex-direction:column;gap:5px;}
.dfeat .frow{display:flex;flex-wrap:wrap;align-items:center;gap:6px;}
.dfeat .flabel{font-size:11px;font-weight:700;color:#6b7280;min-width:52px;}
.dfeat .flabel .flabel-sub{font-weight:400;color:#9ca3af;}
.dfeat .fchip,.dfeat .gchip{font-size:11.5px;padding:2px 9px;border-radius:999px;
  background:#f1f5f9;color:#334155;border:1px solid #e2e8f0;white-space:nowrap;}
/* 근거·연계(공식 연계지만 본문 인용 미검출) — 인디고 계열, 링크 */
.dfeat a.fchip-lnk{background:#e0e7ff;color:#3730a3;border-color:#c7d2fe;text-decoration:none;}
.dfeat a.fchip-lnk:hover{background:#c7d2fe;}
.dfeat .gchip.g-review{background:#fef3e2;color:#b45309;border-color:#fcd9a8;}
.dfeat .gchip.g-mechanical{background:#e7edff;color:#1d4ed8;border-color:#c7d6fe;}
.dfeat .gchip.g-format{background:#f3ecff;color:#6d28d9;border-color:#ddd0fb;}
.dfeat .gchip.g-check{background:#f1f1f4;color:#52525b;border-color:#dedee3;}
.dfeat .gchip.g-current{background:#e9f7ee;color:#15803d;border-color:#c3ebd0;}
.detail .body{padding:8px 14px 18px;}
.empty{padding:40px 16px;color:#9ca3af;text-align:center;font-size:14px;}
.btn{font-size:12px;padding:4px 10px;border:1px solid #d1d5db;border-radius:7px;
     background:#fff;cursor:pointer;color:#374151;}
/* 분석 권고서(인쇄용) — 잘 안 보인다는 피드백 → 또렷한 색 버튼 */
.btn.reportbtn{font-size:13px;font-weight:700;padding:6px 14px;background:#1e40af;color:#fff;
     border-color:#1e40af;}
.btn.reportbtn:hover{background:#1e3a8a;}
.muted{color:#9ca3af;}
/* 단계4 통합 상세: 좌 본문(하이라이트) | 우 권고 */
.layout.detail-open #listPanel{display:none;}
.layout.detail-open{grid-template-columns:1fr;}
.dsplit{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);}
.dbody{padding:12px 16px;border-right:1px solid #eef0f3;max-height:76vh;overflow:auto;}
.drec{padding:6px 14px;max-height:76vh;overflow:auto;}
.dcolhd{font-size:13px;color:#475569;font-weight:600;margin:2px 0 10px;line-height:1.55;}
.drec .curbtn{margin:0 0 10px;width:100%;font-size:12.5px;padding:8px 12px;
  border:1px dashed #cbd5e1;border-radius:9px;background:#f8fafc;color:#475569;
  cursor:pointer;font-weight:600;transition:background .15s,border-color .15s;}
.drec .curbtn:hover{background:#f1f5f9;border-color:#94a3b8;}
.drec .curbtn.open{border-style:solid;background:#eef2ff;color:#3730a3;border-color:#c7d2fe;}
.drec .diffbtn{border-style:solid;border-color:#e2e8f0;background:#fff;}
.drec .diffbtn:hover{background:#f1f5f9;border-color:#94a3b8;}
.art{margin:0 0 10px;scroll-margin-top:8px;border:1px solid #eef0f3;border-radius:9px;
     padding:9px 13px;background:#fff;}
.art.cited{border-left:3px solid #2563eb;}
.art.nocite{background:#fafafa;opacity:.78;}
.art .ahd{font-size:13px;font-weight:700;color:#1e3a8a;margin-bottom:6px;
          display:flex;align-items:center;gap:7px;}
.art.nocite .ahd{color:#9ca3af;}
.art .ahd .ct{font-size:10.5px;font-weight:600;background:#dbeafe;color:#1e40af;
              border-radius:999px;padding:1px 8px;}
.art .atext{font-size:15px;line-height:1.85;white-space:pre-wrap;color:#111827;
            word-break:keep-all;overflow-wrap:anywhere;}
.dbody mark:focus-visible{outline:2px solid #f59e0b;outline-offset:1px;}
mark.cite-law{background:#dbeafe;color:#1e3a8a;border-radius:3px;padding:0 2px;cursor:pointer;
              transition:background .15s;}
mark.cite-law:hover{background:#bfdbfe;}
mark.cite-naked{background:#ede9fe;color:#5b21b6;border-radius:3px;padding:0 2px;cursor:pointer;}
mark.cite-naked:hover{background:#ddd6fe;}
mark.cite-local{background:#f1f5f9;color:#64748b;border-radius:3px;padding:0 2px;
                border-bottom:1px dotted #94a3b8;cursor:pointer;}
mark.cite-local:hover{background:#e2e8f0;}
/* 변경 없음(현행) 인용 — 밑줄 없이 진한 검정+굵게로 구분: '검토했고 변경 없음' 흔적 */
mark.cite-current{background:transparent;color:#0f172a;font-weight:700;padding:0;border-radius:0;cursor:pointer;}
mark.cite-current:hover{background:#f1f5f9;}
mark.cite-focus{outline:2px solid #f59e0b;outline-offset:1px;}
/* 타 조례(자치법규) 인용 클릭 시 우측 미니 정보 카드 */
.lref{position:relative;border:1px solid #c7d2fe;background:#eef2ff;border-radius:9px;
      padding:10px 28px 10px 12px;margin:0 0 12px;}
.lref .h{font-size:13px;font-weight:600;color:#3730a3;}
.lref .tag{font-size:10.5px;background:#e0e7ff;color:#4338ca;border-radius:999px;
           padding:1px 7px;font-weight:600;margin-left:4px;}
.lref .m{font-size:12px;color:#6366f1;margin:4px 0 8px;}
.lref .x{position:absolute;top:7px;right:10px;cursor:pointer;color:#94a3b8;font-size:16px;}
/* 상위법령 정보 카드 — 자치법규(인디고)와 구분해 navy 계열 */
.lref.law{border-color:#bfdbfe;background:#eff6ff;}
.lref.law .h{color:#1e3a8a;}
.lref.law .m{color:#334155;}
.lref .tag.tlaw{background:#dbeafe;color:#1e40af;}
.lref .rcl{display:inline-block;font-size:11px;background:#fff;border:1px solid #bfdbfe;
           color:#1e40af;border-radius:5px;padding:0 6px;margin:1px 3px 1px 0;}
.lref .rsum{font-weight:600;font-size:11.5px;}
.lref.law .btn{display:inline-block;text-decoration:none;font-size:12px;margin-top:2px;}
/* 카드 내 앵커형 버튼(국가법령정보센터 링크) — 법령·행정규칙·자치법규 카드 공통 */
.lref a.btn{display:inline-block;text-decoration:none;margin-top:2px;}
.artsec{scroll-margin-top:8px;}
.flash{animation:flash 1.4s ease;}
@keyframes flash{0%{background:#fde68a;}70%{background:#fef3c7;}100%{background:transparent;}}
/* 법령 개정 알림(상위법 개정 → 영향 조례 역추적) */
.changes{margin:0 0 20px;border:1px solid #fca5a5;background:#fef2f2;border-radius:10px;overflow:hidden;}
.changes .chd{padding:11px 15px;font-size:14px;color:#991b1b;font-weight:600;cursor:pointer;
              display:flex;align-items:center;gap:8px;}
.changes .chd .badge{background:#dc2626;color:#fff;font-size:12px;border-radius:999px;
                     padding:1px 9px;font-weight:700;}
.changes .chd .arr{margin-left:auto;color:#dc2626;transition:transform .15s;}
.changes.open .chd .arr{transform:rotate(90deg);}
.changes .clist{padding:0 15px 12px;display:none;}
.changes.open .clist{display:block;}
.changes .law{border-top:1px solid #fecaca;padding:9px 0;}
.changes .law .lname{font-size:13.5px;color:#7f1d1d;font-weight:600;text-decoration:none;}
a.lname:hover{text-decoration:underline;}
.changes .law .lmeta{font-size:12px;color:#b91c1c;font-weight:400;margin-left:6px;}
.changes .grp{display:flex;align-items:flex-start;gap:8px;margin:6px 0 0;}
.changes .lbl{flex:0 0 auto;font-size:11px;font-weight:700;border-radius:999px;padding:2px 9px;
              margin-top:2px;white-space:nowrap;}
.changes .lbl.hit{background:#dc2626;color:#fff;}
.changes .lbl.unc{background:#fde68a;color:#92400e;}
.changes .lbl.lnk{background:#e0e7ff;color:#3730a3;cursor:help;}
.changes .ords{display:flex;flex-wrap:wrap;gap:6px;}
.changes .ords a{font-size:12.5px;background:#fff;border:1px solid #fecaca;border-radius:7px;
                 padding:3px 9px;color:#991b1b;cursor:pointer;text-decoration:none;}
.changes .ords a:hover{background:#fee2e2;}
.changes .ords a .cl{color:#dc2626;font-weight:600;font-size:11px;}
.changes .none{font-size:12px;color:#9ca3af;margin:6px 0 0;}
.changes .chall{margin-left:14px;font-weight:400;font-size:12px;cursor:pointer;display:inline-flex;align-items:center;gap:4px;}
.changes .law.acked{opacity:.55;}
.changes .ackb{float:right;font-size:11.5px;border:1px solid #dc2626;color:#dc2626;background:#fff;
  border-radius:7px;padding:2px 10px;cursor:pointer;font-weight:600;}
.changes .ackb:hover{background:#fee2e2;}
.changes .ackb.done{border-color:#16a34a;color:#16a34a;}
.changes .ackb.done:hover{background:#dcfce7;}
.changes .ackb:disabled{opacity:.5;cursor:default;}

/* ============ 상단 헤더 밴드(그라데이션) ============ */
.topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;
  background:linear-gradient(120deg,#13325b 0%,#1d4e89 60%,#2563eb 130%);color:#fff;
  border-radius:16px;padding:20px 24px;margin:0 0 16px;
  box-shadow:0 10px 26px rgba(19,50,91,.20);}
.topbar h1{font-size:22px;margin:0 0 6px;color:#fff;letter-spacing:-.3px;}
.topbar-meta{font-size:12.5px;color:#c9d8ee;line-height:1.6;}
.topbar-meta b{color:#fff;}
.topbar-meta .muted{color:#9fb3d1;}
.topbar-admin{font-size:12.5px;color:#e8f0fb;text-decoration:none;white-space:nowrap;
  background:rgba(255,255,255,.14);padding:7px 13px;border-radius:9px;border:1px solid rgba(255,255,255,.14);}
.topbar-admin:hover{background:rgba(255,255,255,.26);}

/* ============ KPI 카드 그리드 ============ */
.kpis{display:grid;grid-template-columns:1.7fr 3.6fr;gap:12px;margin:0 0 18px;align-items:stretch;}
.kpi-grades{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:12px;}
.kpi{background:#fff;border:1px solid #e8ebef;border-radius:14px;padding:15px 16px;
  position:relative;overflow:hidden;box-shadow:0 1px 2px rgba(16,24,40,.05);
  transition:box-shadow .15s,transform .15s;}
.kpi:hover{box-shadow:0 6px 18px rgba(16,24,40,.09);transform:translateY(-1px);}
.kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:#e5e7eb;}
.kpi .kpi-ic{font-size:17px;line-height:1;}
.kpi .kpi-n{font-size:27px;font-weight:800;color:#111827;line-height:1;margin-top:8px;
  font-variant-numeric:tabular-nums;}
.kpi .kpi-n small{font-size:14px;font-weight:700;color:#6b7280;margin-left:2px;}
.kpi .kpi-l{font-size:12px;color:#374151;font-weight:700;margin-top:6px;}
.kpi .kpi-d{font-size:11px;color:#9ca3af;margin-top:3px;line-height:1.35;word-break:keep-all;}
.kpi.k-mech::before{background:var(--mech);} .kpi.k-mech .kpi-n{color:var(--mech);}
.kpi.k-rev::before{background:var(--rev);}   .kpi.k-rev .kpi-n{color:var(--rev);}
.kpi.k-chk::before{background:var(--chk);}    .kpi.k-chk .kpi-n{color:var(--chk);}
.kpi.k-fmt::before{background:var(--fmt);}    .kpi.k-fmt .kpi-n{color:var(--fmt);}
.kpi.k-cur::before{background:var(--cur);}    .kpi.k-cur .kpi-n{color:var(--cur);}
/* 등급 칩(=필터 버튼): 클릭 가능·선택 시 강조 */
.kpi[data-grade]{cursor:pointer;}
.kpi[data-grade].active{border-color:#0f172a;box-shadow:0 0 0 2px #0f172a;}
/* 주 KPI(정비 필요 조례) — 강조 */
.kpi.primary{background:linear-gradient(135deg,#fff 55%,#fff7ed);border-color:#fcd9a8;}
.kpi.primary::before{background:linear-gradient(#f59e0b,#d97706);width:5px;}
.kpi.primary .kpi-l{font-size:12.5px;color:#92400e;}
.kpi.primary .kpi-n{font-size:44px;color:#b45309;margin-top:4px;}
.kpi.primary .kpi-sub{font-size:12.5px;color:#78716c;margin-top:6px;}
.kpi.primary .kpi-sub b{color:#b45309;}
.hero-bar{height:9px;border-radius:99px;background:#fde7d0;overflow:hidden;margin:10px 0 2px;}
.hero-bar i{display:block;height:100%;background:linear-gradient(90deg,#f59e0b,#d97706);border-radius:99px;
  transition:width .5s ease;}
@media(max-width:920px){.kpis{grid-template-columns:1fr;}}
@media(max-width:520px){.topbar{flex-direction:column;}
  .kpi .kpi-n{font-size:23px;} .kpi.primary .kpi-n{font-size:38px;}}

/* ============ 소관업무 편 카드(아코디언) ============ */
.plist{display:flex;flex-direction:column;gap:10px;}
.pcard{background:#fff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;}
.pcard-hd{display:flex;align-items:center;gap:10px;width:100%;cursor:pointer;background:#fff;
  border:none;padding:14px 16px;text-align:left;}
.pcard.open .pcard-hd{background:#f6f9fc;border-bottom:1px solid #eef0f3;}
.pcard-hd .arr{color:#94a3b8;font-size:12px;width:14px;flex:0 0 auto;}
.pcard-hd .pn{flex:1;font-size:15px;font-weight:700;color:#13325b;}
.pcard-hd .pgrades{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end;}
.pcard-hd .ps{font-size:13px;color:#6b7280;white-space:nowrap;}
.pcard-hd .ps b{color:#b45309;font-size:15px;}
.pcard-body{display:none;padding:6px 10px 10px;}
.pcard.open .pcard-body{display:block;}
/* 편별 등급 비율 5색 미니 막대(접힘 상태에서도 항상 노출) */
.pbar{display:flex;height:6px;border-radius:99px;overflow:hidden;background:#eef2f7;margin:0 14px 10px;}
.pbar i{display:block;height:100%;}
.pbar .s-mech{background:var(--mech);}.pbar .s-rev{background:var(--rev);}
.pbar .s-chk{background:var(--chk);}.pbar .s-fmt{background:var(--fmt);}.pbar .s-cur{background:var(--cur);}
.dcard{display:flex;align-items:center;gap:10px;cursor:pointer;border-radius:9px;padding:11px 12px;}
.dcard:hover{background:#f8fafc;}
.dcard+.dcard{border-top:1px solid #f3f4f6;}
.dcard .dn{flex:1;font-size:14px;color:#374151;font-weight:600;}
.dcard .dd{font-size:12.5px;color:#9ca3af;white-space:nowrap;}
.dcard .dchips{display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end;}

/* ============ 조례 목록 카드 ============ */
.olist{display:flex;flex-direction:column;gap:9px;}
.ocard{background:#fff;border:1px solid #e5e7eb;border-radius:11px;padding:13px 15px;cursor:pointer;}
.ocard:hover{border-color:#bfdbfe;background:#fbfdff;}
.ocard .on{font-size:14.5px;font-weight:600;color:#1f2937;line-height:1.4;}
.ocard .om{font-size:12.5px;color:#9ca3af;margin-top:4px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
.ocard .ochips{display:flex;gap:5px;flex-wrap:wrap;margin-top:8px;}
.ocard .lawhit{color:#2563eb;font-size:12px;}
.listhd{font-size:12.5px;color:#6b7280;margin:2px 2px 10px;font-weight:600;}

/* ============ 반응형(모바일) ============ */
@media (max-width:760px){
  .wrap{padding:14px 12px 60px;}
  h1{font-size:18px;}
  .pcard-hd .pgrades{display:none;}       /* 모바일=간단: 편 등급칩 숨김 */
  .banner{font-size:12px;}
  .hero{padding:15px 15px;border-radius:12px;}
  .hero-num{font-size:38px;}
  .controls{gap:8px;}
  .controls #searchBox{min-width:0;flex:1 1 100%;font-size:16px;}  /* 16px=iOS 확대방지 */
  .controls select{flex:1 1 100%;min-width:0;}
  .controls label{flex:1 1 auto;}
  #homeBtn{flex:0 0 auto;}
  /* 상세: 좌 본문 | 우 검토 → 세로 스택 + 탭 전환 */
  .dsplit{grid-template-columns:1fr;}
  .dbody{border-right:none;max-height:none;}
  .drec{max-height:none;}
  .mtabs{display:flex;gap:6px;margin:4px 0 10px;}
  .mtabs button{flex:1;padding:9px;border:1px solid #d1d5db;border-radius:9px;background:#fff;
    font-size:14px;font-weight:600;cursor:pointer;color:#374151;}
  .mtabs button.on{background:#13325b;color:#fff;border-color:#13325b;}
  .dsplit.show-body .drec{display:none;}
  .dsplit.show-rec .dbody{display:none;}
}
@media (min-width:761px){ .mtabs{display:none;} }
</style></head>
<body><div class="wrap">
  <header class="topbar">
    <div>
      <h1>자치법규 정비 총괄 대시보드</h1>
      <div class="topbar-meta" id="banner">불러오는 중…</div>
    </div>
    <a href="/admin" id="adminLink" class="topbar-admin">🔧 판정 근거 검사</a>
  </header>
  <div class="kpis" id="hero"></div>
  <div class="changes" id="changes" style="display:none"></div>
  <div class="controls">
    <button class="backbtn homebtn" id="homeBtn" onclick="goHome()" title="첫페이지로 돌아가며 최신 데이터를 다시 불러옵니다">🔄 새로고침</button>
    <input type="search" id="searchBox" placeholder="🔍 조례명·인용 법령 검색" autocomplete="off">
    <select id="deptSel"><option value="">담당과 — 전체</option></select>
    <select id="sortSel" title="조례 목록 정렬 기준">
      <option value="action">정비 많은 순</option>
      <option value="old">시행일 오래된 순</option>
      <option value="new">시행일 최신 순</option></select>
    <label><input type="checkbox" id="actChk"> 정비 대상만</label>
    <span class="crumb" id="crumb"></span>
  </div>
  <div class="layout" id="layout">
    <div class="panel" id="listPanel"></div>
    <div class="panel detail" id="detailPanel" style="display:none"></div>
  </div>
</div>
<script>
const GO=[]; const GM={};          // grade_order / grade_meta
const PCLS={mechanical:"p-mech",review:"p-rev",check:"p-chk",format:"p-fmt",current:"p-cur"};
const STATIC=false;                // export-static 가 true 로 치환(서버 없이 정적 파일만)
let OV=null, curDept="", curMst="", curQuery="", curGrade="", curSort="action", cssInjected=false, LOCALREFS={}, LAWREFS={};
let _ALLORDS=null, DEPTIDX={};      // 정적: 전체 조례 캐시·담당과 인덱스(리포트 파일명)

const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function fdate(s){const d=String(s||"").replace(/[^0-9]/g,"").slice(0,8);
  return d.length===8?`${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}`:(s||"—");}
function dist(grades){ // 등급 분포 pill (current 제외, 0은 생략)
  return GO.filter(k=>k!=="current"&&grades[k]>0)
    .map(k=>`<span class="pill ${PCLS[k]}">${GM[k].emoji} ${grades[k]}</span>`).join("")||'<span class="muted">—</span>';}

function _staticPath(u){            // 라이브 엔드포인트 → 정적 파일 경로
  const p=u.split("?")[0];
  if(p==="/api/overview")return "api/overview.json";
  if(p==="/api/changes")return "api/changes.json";
  if(p==="/api/ordinances")return "api/ordinances.json";
  if(p.indexOf("/api/ordinance/")===0)return "api/ordinance/"+p.split("/").pop()+".json";
  return u;
}
async function getJSON(u){const r=await fetch(STATIC?_staticPath(u):u);return r.json();}
function ordReportHref(mst){
  return STATIC?`report/${encodeURIComponent(mst)}.html`
    :`/api/ordinance_report?mst=${encodeURIComponent(mst)}&format=html`;}
function deptReportHref(dept,fmt){
  return STATIC?`dept_report/${DEPTIDX[dept]}.${fmt}`
    :`/api/dept_report?dept=${encodeURIComponent(dept)}&format=${fmt}`;}
function gradeReportHref(grade){    // 등급별 전 부서 정비목록 CSV(의법팀 배포용)
  return STATIC?`grade_report/${grade}.csv`
    :`/api/grade_report?grade=${encodeURIComponent(grade)}&format=csv`;}
function sortOrds(list){            // 정렬 토글: 시행일 오래된/최신 순 or 정비 많은 순(기본)
  const dt=o=>String(o.enforce_date||"").replace(/[^0-9]/g,"").slice(0,8);
  const cnt=(a,b)=>(b.items_count-a.items_count)||String(a.name).localeCompare(b.name);
  const L=list.slice();
  if(curSort==="old")return L.sort((a,b)=>(dt(a)||"99999999").localeCompare(dt(b)||"99999999")||cnt(a,b));
  if(curSort==="new")return L.sort((a,b)=>(dt(b)||"").localeCompare(dt(a)||"")||cnt(a,b));
  return L.sort(cnt);
}
async function fetchOrds(opts){     // 조례 목록(라이브=서버 필터 / 정적=전체 캐시 클라 필터)
  const dept=opts.dept||"", q=(opts.q||"").trim(), action=!!opts.action;
  if(!STATIC){
    const qs=[]; if(q)qs.push("q="+encodeURIComponent(q));
    else if(dept)qs.push("dept="+encodeURIComponent(dept));
    if(action)qs.push("action=1");
    return getJSON("/api/ordinances"+(qs.length?"?"+qs.join("&"):""));
  }
  if(!_ALLORDS)_ALLORDS=await getJSON("/api/ordinances");
  let list=_ALLORDS.slice();
  if(q){const qn=q.replace(/\s/g,"");
    list=list.filter(o=>(o.name||"").replace(/\s/g,"").includes(qn)||(o.laws||[]).some(l=>l.includes(q)))
      .map(o=>{const inName=(o.name||"").replace(/\s/g,"").includes(qn);
        return Object.assign({},o,{law_hits:inName?[]:(o.laws||[]).filter(l=>l.includes(q))});});
  }else if(dept){list=list.filter(o=>o.dept===dept);}
  if(action)list=list.filter(o=>o.items_count>0);
  list.sort((a,b)=>(b.items_count-a.items_count)||String(a.name).localeCompare(b.name));
  return list;
}

function gcls(k){return k==="mechanical"?"mech":k==="review"?"rev":k==="check"?"chk":k==="format"?"fmt":"cur";}
function nf(n){return Number(n||0).toLocaleString();}
function pbar(grades){ // grade_order 순 5색 비율 미니 막대(0 등급 세그먼트 생략)
  const tot=GO.reduce((s,k)=>s+(grades[k]||0),0);
  if(!tot)return "";
  return `<div class="pbar" title="등급 비율">`+GO.filter(k=>grades[k]>0)
    .map(k=>`<i class="s-${gcls(k)}" style="width:${(grades[k]/tot*100).toFixed(1)}%" title="${esc(GM[k].label)} ${grades[k]}"></i>`).join("")
    +`</div>`;}
function renderHero(){
  const g=OV.grades, t=OV.totals;
  const pct=t.ordinances?Math.round(t.action/t.ordinances*100):0;
  // 주 KPI: 정비 필요 조례(건) + 진행바
  const primary=`<div class="kpi primary">
     <div class="kpi-l">🛠 정비 필요 조례</div>
     <div class="kpi-n">${nf(t.action)}<small>건</small></div>
     <div class="kpi-sub">전체 <b>${nf(t.ordinances)}</b>건 중 <b>${pct}%</b></div>
     <div class="hero-bar"><i style="width:${pct}%"></i></div></div>`;
  // 등급별 KPI 카드(판정 건수) — 0건 등급은 숨김(항상 0인 지표 제외). 색은 좌측 액센트 바.
  const cards=GO.filter(k=>g[k]).map(k=>{const m=GM[k];
    return `<div class="kpi k-${gcls(k)}${k===curGrade?" active":""}" data-grade="${k}"
         title="${esc(m.desc||"")}${m.desc?" — ":""}클릭 시 이 등급만 보기">
       <div class="kpi-ic">${m.emoji}</div>
       <div class="kpi-n">${nf(g[k])}</div>
       <div class="kpi-l">${esc(m.label)}</div>
       ${m.desc?`<div class="kpi-d">${esc(m.desc)}</div>`:""}</div>`;}).join("");
  const H=document.getElementById("hero");
  H.innerHTML=primary+`<div class="kpi-grades">${cards}</div>`;
  H.querySelectorAll(".kpi[data-grade]").forEach(c=>c.onclick=()=>selectGrade(c.dataset.grade));
}
function togglePyeon(card){
  const open=card.classList.toggle("open");
  const arr=card.querySelector(".arr"); if(arr)arr.textContent=open?"▾":"▸";
}
function renderBanner(){
  const b=OV.batch||{};
  document.getElementById("banner").innerHTML=
    `<b>${esc(b.region_name||"—")}</b> · 스냅샷 기준일 <b>${esc((b.batch_date||"").slice(0,10)||"—")}</b>`
    +` · 조례 ${b.ordinances_n||OV.totals.ordinances} · 법령 ${b.laws_n||"—"} · 판정 ${b.findings_n||"—"}건`
    +` · 담당과 ${OV.totals.depts}개 · <span class="muted">읽기전용(라이브 API 0)</span>`;
}
let CHANGES_ALL=false;     // 검토완료 포함 보기 토글
async function renderChanges(){
  // 상위법 개정 → 영향 조례 역추적 알림. 미검토 0건이면 숨김.
  const box=document.getElementById("changes");
  let ch=[];
  try{ch=await getJSON("/api/changes"+(CHANGES_ALL?"?all=1":""));}catch(e){ch=[];}
  if(!ch||!ch.length){box.style.display="none";box.innerHTML="";return;}
  let nAff=0,nUnc=0,nLnk=0; ch.forEach(c=>{nAff+=(c.affected||[]).length;nUnc+=(c.uncertain||[]).length;nLnk+=(c.linked||[]).length;});
  const chip=o=>`<a data-mst="${esc(o.mst)}" title="${esc(o.dept||"")}">${esc(o.name)}`
    +(o.clauses&&o.clauses.length?` <span class="cl">${esc(o.clauses.join(","))}</span>`:"")+`</a>`;
  const laws=ch.map(c=>{
    const aff=c.affected||[], unc=c.uncertain||[], lnk=c.linked||[];
    const caTxt=c.changed_articles==="*"?"전부개정"
      :(!c.changed_articles?"바뀐 조문 판별불가":`바뀐 조문 ${esc(c.changed_articles)}`);
    const affHtml=aff.length?`<div class="grp"><span class="lbl hit">해당 ${aff.length}</span>
        <div class="ords">${aff.map(chip).join("")}</div></div>`:"";
    const uncHtml=unc.length?`<div class="grp"><span class="lbl unc">확인 ${unc.length}</span>
        <div class="ords">${unc.map(chip).join("")}</div></div>`:"";
    // 공식 연계(lnkOrg)엔 있으나 본문 인용이 없던 조례 — 개정 시 놓치던 위임·근거 관계(리콜)
    const lnkHtml=lnk.length?`<div class="grp"><span class="lbl lnk" title="국가법령정보 공식 연계 — 조례 본문에 이 법령 인용은 못 찾음(위임·근거 관계 가능). 개정 관련 여부 수기 확인">연계 ${lnk.length}</span>
        <div class="ords">${lnk.map(chip).join("")}</div></div>`:"";
    const empty=(!aff.length&&!unc.length&&!lnk.length)?'<div class="none">바뀐 조문을 인용한 조례 없음(무관)</div>':"";
    const ackBtn=STATIC?"":(c.acked
      ?`<button class="ackb done" data-law="${esc(c.law_id)}" data-key="${esc(c.new_key||"")}" data-ack="0">✓ 검토완료 (해제)</button>`
      :`<button class="ackb" data-law="${esc(c.law_id)}" data-key="${esc(c.new_key||"")}" data-ack="1">검토완료 표시</button>`);
    // 바뀐 법령명도 국가법령정보센터 링크로(법령·행정규칙·자치법규 카드와 동일 방식)
    const cportal=`https://www.law.go.kr/법령/${encodeURIComponent(c.name)}`;
    return `<div class="law${c.acked?" acked":""}">`
      +`<a class="lname" href="${cportal}" target="_blank" rel="noopener" title="국가법령정보센터에서 보기">「${esc(c.name)}」 ↗</a>`
      +`<span class="lmeta">${esc(c.revise_type||"개정")} · 시행 ${fdate(c.new_enforce)} · ${caTxt}</span>
      ${ackBtn}${affHtml}${uncHtml}${lnkHtml}${empty}</div>`;
  }).join("");
  const nOpen=ch.filter(c=>!c.acked).length;
  box.innerHTML=`<div class="chd"><span class="badge">🔔 ${nOpen}</span>
     법령 개정 감지 — 해당 조례 ${nAff}건 · 확인 ${nUnc}건${nLnk?` · 연계 ${nLnk}건`:""}
     <span class="muted" style="font-weight:400">· 조례 클릭 시 상세</span>
     ${STATIC?"":`<label class="muted chall"><input type="checkbox" id="chAll"${CHANGES_ALL?" checked":""}> 검토완료 포함</label>`}
     <span class="arr">▸</span></div>
     <div class="clist">${laws}</div>`;
  box.style.display="block";
  box.classList.add("open");
  box.querySelector(".chd").onclick=e=>{if(e.target.id!=="chAll")box.classList.toggle("open");};
  const _ca=box.querySelector("#chAll");
  if(_ca)_ca.onclick=e=>{e.stopPropagation();CHANGES_ALL=e.target.checked;renderChanges();};
  box.querySelectorAll(".ords a[data-mst]").forEach(a=>
    a.onclick=()=>selectOrd(a.dataset.mst));
  box.querySelectorAll(".ackb").forEach(b=>b.onclick=async e=>{
    e.stopPropagation();
    b.disabled=true;
    try{
      await fetch("/api/ack",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({law_id:b.dataset.law,new_key:b.dataset.key,acked:b.dataset.ack==="1"})});
    }catch(err){}
    renderChanges();
  });
}
function fillDeptSelect(){
  const sel=document.getElementById("deptSel");
  for(const d of OV.depts){const o=document.createElement("option");
    o.value=d.dept;o.textContent=`${d.dept} (정비 ${d.action}/${d.total})`;sel.appendChild(o);}
}

function _deptCard(d){
  return `<div class="dcard" data-dept="${esc(d.dept)}"><span class="dn">${esc(d.dept)}</span>
    <span class="dd">정비 ${d.action}/${d.total}</span><span class="dchips">${dist(d.grades)}</span></div>`;
}
function renderDeptTable(){
  const groups=OV.dept_groups, P=document.getElementById("listPanel");
  if(!groups){   // 폴백(타 시군): 담당과 정비량순 카드
    P.innerHTML=`<div class="listhd">담당과 · 정비 많은 순</div><div class="olist">`
      +OV.depts.map(d=>`<div class="ocard" data-dept="${esc(d.dept)}"><div class="on">${esc(d.dept)}</div>
         <div class="om">정비 ${d.action} · 전체 ${d.total}</div><div class="ochips">${dist(d.grades)}</div></div>`).join("")
      +`</div>`;
    P.querySelectorAll(".ocard[data-dept]").forEach(c=>c.onclick=()=>selectDept(c.dataset.dept));
    return;
  }
  // 소관업무별(편) 카드 — 기본 접힘, 편을 눌러 하위 과 펼침(데스크톱은 편별 등급칩도 표시)
  P.innerHTML=`<div class="listhd">소관업무별 · 편을 눌러 하위 과 보기</div><div class="plist">`
    +groups.map(g=>`<div class="pcard" data-pname="${esc(g.name)}">
        <button class="pcard-hd"><span class="arr">▸</span>
          <span class="pn">${g.no?'제'+esc(g.no)+'편 ':''}${esc(g.name)}</span>
          <span class="pgrades">${dist(g.grades)}</span>
          <span class="ps">정비 <b>${g.action}</b> / ${g.total}</span></button>
        ${pbar(g.grades)}
        <div class="pcard-body">${g.depts.map(_deptCard).join("")}</div></div>`).join("")
    +`</div>`;
  P.querySelectorAll(".pcard-hd").forEach(hd=>hd.onclick=()=>togglePyeon(hd.parentElement));
  P.querySelectorAll(".dcard[data-dept]").forEach(c=>c.onclick=()=>selectDept(c.dataset.dept));
}

function _ordCard(o,showDept){
  const hit=(o.law_hits&&o.law_hits.length)
    ?`<span class="lawhit">↳ ${esc(o.law_hits.slice(0,2).join(", "))}${o.law_hits.length>2?" 외 "+(o.law_hits.length-2):""}</span>`:"";
  return `<div class="ocard" data-mst="${esc(o.mst)}"><div class="on">${esc(o.name)}</div>
    <div class="om">${showDept&&o.dept?esc(o.dept)+" · ":""}시행 ${fdate(o.enforce_date)} · 정비 ${o.items_count}건 ${hit}</div>
    <div class="ochips">${dist(o.grades)}</div></div>`;
}
async function renderOrdList(){
  let list=await fetchOrds({dept:curDept, action:document.getElementById("actChk").checked});
  if(curGrade)list=list.filter(o=>o.grades&&o.grades[curGrade]>0);
  list=sortOrds(list);
  const P=document.getElementById("listPanel");
  if(!list.length){P.innerHTML=`<div class="empty">해당 조건의 조례가 없습니다.</div>`;return;}
  P.innerHTML=`<div class="listhd">${esc(curDept)} · 조례 ${list.length}건</div><div class="olist">`
    +list.map(o=>_ordCard(o,false)).join("")+`</div>`;
  P.querySelectorAll(".ocard[data-mst]").forEach(c=>c.onclick=()=>selectOrd(c.dataset.mst));
}
async function renderSearch(){
  // 자유검색(조례명 또는 인용 법령명 매치, 담당과 교차)
  let list=await fetchOrds({q:curQuery, action:document.getElementById("actChk").checked});
  if(curGrade)list=list.filter(o=>o.grades&&o.grades[curGrade]>0);
  list=sortOrds(list);
  const P=document.getElementById("listPanel");
  if(!list.length){P.innerHTML=`<div class="empty">'${esc(curQuery)}' 검색 결과 없음.</div>`;return;}
  P.innerHTML=`<div class="listhd">검색 “${esc(curQuery)}” · ${list.length}건</div><div class="olist">`
    +list.map(o=>_ordCard(o,true)).join("")+`</div>`;
  P.querySelectorAll(".ocard[data-mst]").forEach(c=>c.onclick=()=>selectOrd(c.dataset.mst));
}

function flash(el){if(!el)return;el.classList.remove("flash");void el.offsetWidth;
  el.classList.add("flash");el.scrollIntoView({behavior:"smooth",block:"center"});}

async function selectOrd(mst){
  curMst=mst;
  const d=await getJSON(`/api/ordinance/${encodeURIComponent(mst)}`);
  const r=d.recommend||{}, m=d.meta||{}, arts=d.articles||[];
  LOCALREFS=d.local_refs||{}; LAWREFS=d.law_refs||{};
  if(r.css && !cssInjected){const s=document.createElement("style");
    s.textContent=r.css;document.head.appendChild(s);cssInjected=true;}
  const meta=`시행 ${fdate(m.enforce_date)} · 담당 ${esc(m.dept||"—")}`
    +(m.phone?` · ☎ ${esc(m.phone)}`:"");
  // 좌=현황(인용 구성), 우=상세(정비 상태 + 검토 내역). 현황은 본문 위, 정비상태는 검토 위.
  const F=d.features||{}, gr=r.grades||{};   // GM(등급 라벨)은 전역 — 단일 출처(overview)
  const comp=[];
  if(F.law)comp.push(`<span class="fchip">⚖ 상위법령 ${F.law}</span>`);
  if(F.admrul)comp.push(`<span class="fchip">📕 행정규칙 ${F.admrul}</span>`);
  if(F.local)comp.push(`<span class="fchip">🏛 타조례 ${F.local}</span>`);
  const compBar=comp.length?`<div class="dfeat"><div class="frow"><span class="flabel">인용 구성</span>${comp.join("")}</div></div>`:"";
  // 근거·연계 — 국가법령정보 공식 연계(lnkOrg)인데 본문에서 인용을 못 찾은 법령(위임·근거 관계 가능)
  const lref=d.linked_refs||[];
  const lchips=lref.map(x=>`<a class="fchip fchip-lnk" target="_blank" rel="noopener"`
    +` href="https://www.law.go.kr/${encodeURIComponent("법령")}/${encodeURIComponent(x.name)}"`
    +` title="공식 연계이나 조례 본문에 인용 미검출 — 위임·근거 관계일 수 있어 확인 권장">「${esc(x.name)}」 ↗</a>`).join("");
  const linkedBar=lref.length?`<div class="dfeat"><div class="frow"><span class="flabel" title="국가법령정보 공식 연계인데 본문 인용이 안 보이는 법령 — 위임·근거 관계 가능">근거·연계 <span class="flabel-sub">(본문 인용 미검출)</span></span>${lchips}</div></div>`:"";
  // 정비 상태(우측) — 정비 대상 등급만. 현행(변경없음)은 아래 '제외 대상' 버튼이 대신 안내.
  const gcs=["mechanical","review","check","format"].filter(k=>gr[k])
    .map(k=>`<span class="gchip g-${k}">${GM[k].emoji} ${esc(GM[k].label)} ${gr[k]}</span>`);
  const gradeBar=gcs.length
    ?`<div class="dfeat"><div class="frow"><span class="flabel">정비 상태</span>${gcs.join("")}</div></div>`
    :`<div class="dfeat"><div class="frow"><span class="gchip g-current">✅ 정비 대상 없음 — 인용 전부 현행</span></div></div>`;
  // 제외 대상(현행·변경 없음) — 기본 숨김, 버튼으로 펼침
  const curN=gr.current||0;
  const curBtn=curN?`<button id="showCurBtn" class="curbtn">＋ 제외 대상(변경 없음) ${curN}건 보기</button>`:"";
  // 개정 내용 diff 보기 방식 토글 — 기본 통합(합쳐), 클릭 시 당시·현행 나란히. 변경 내용이 있을 때만.
  const hasDiff=(gr.review||0)+(gr.mechanical||0)>0;
  const diffBtn=hasDiff?`<button id="diffModeBtn" class="curbtn diffbtn">▤ 당시·현행 나란히 보기</button>`:"";
  // 좌: 본문(조문별) — 정비 대상 인용만 강조. 배지·강조 테두리는 정비건수(act) 기준.
  const left=arts.length?arts.map(a=>`<div class="art ${a.act?'cited':'nocite'}" data-oc="${esc(a.no)}">
      <div class="ahd">${esc(a.no)}${a.title?" ("+esc(a.title)+")":""}${a.act?`<span class="ct">정비 ${a.act}</span>`:""}</div>
      <div class="atext">${a.html}</div></div>`).join("")
    :`<div class="empty">본문이 없습니다(body_xml 미적재 — 재배치 필요).</div>`;
  // 우: 권고(report 조각)
  const right=r.found?r.html:`<div class="empty">정비 항목이 없습니다(현행 유지).</div>`;
  const dp=document.getElementById("detailPanel");
  dp.innerHTML=`<div class="dhd"><button class="btn" onclick="closeDetail()">← 목록</button>
     <a class="btn reportbtn" style="margin-left:8px;text-decoration:none" target="_blank"
        href="${ordReportHref(mst)}">📄 분석 권고서(인쇄용) ↗</a>
     <h2 style="margin-top:8px">${esc(m.name||"조례")}</h2><div class="m">${meta}</div></div>
     <div class="mtabs"><button data-t="body" class="on">📄 조례 본문</button><button data-t="rec">🔧 검토 사항</button></div>
     <div class="dsplit show-body">
       <div class="dbody"><div class="dcolhd">📄 조례 본문(현황) — 정비 대상 인용 강조(클릭 시 우측 검토) · <span style="color:#1e3a8a">상위법령</span> / <span style="color:#5b21b6" title="낫표 「」 없이 쓴 상위법령 인용 — 서식 정비 대상">「」 누락</span> · <span style="color:#0f172a;font-weight:700" title="검토했고 변경 없음(현행 정합)">변경없음</span></div>${compBar}${linkedBar}${left}</div>
       <div class="drec hide-cur"><div class="dcolhd">🔧 검토 사항(상세) — 조례 조문별 · 좌측 인용 클릭 시 펼침</div>${gradeBar}${diffBtn}${curBtn}<div id="localref"></div>${right}</div>
     </div>`;
  dp.style.display="block";
  document.getElementById("layout").classList.add("detail-open");
  // 제외 대상(변경 없음·현행) — 기본 숨김. 버튼으로 펼치고 라벨 전환.
  const showCur=dp.querySelector("#showCurBtn");
  if(showCur)showCur.onclick=()=>{
    const drec=dp.querySelector(".drec");
    const hidden=drec.classList.toggle("hide-cur");
    showCur.textContent=hidden?`＋ 제외 대상(변경 없음) ${curN}건 보기`:`－ 제외 대상 숨기기`;
    showCur.classList.toggle("open",!hidden);
  };
  // diff 보기 방식: 기본 통합(합쳐) ↔ 당시·현행 나란히
  const diffMode=dp.querySelector("#diffModeBtn");
  if(diffMode)diffMode.onclick=()=>{
    const split=dp.querySelector(".drec").classList.toggle("diff-mode-split");
    diffMode.textContent=split?"◱ 변경 내용 합쳐 보기":"▤ 당시·현행 나란히 보기";
    diffMode.classList.toggle("open",split);
  };
  // 모바일 탭: 본문/검토 전환(데스크톱은 CSS로 탭 숨김·양쪽 표시)
  dp.querySelectorAll(".mtabs button").forEach(b=>b.onclick=()=>{
    const ds=dp.querySelector(".dsplit");
    ds.classList.remove("show-body","show-rec");
    ds.classList.add(b.dataset.t==="body"?"show-body":"show-rec");
    dp.querySelectorAll(".mtabs button").forEach(x=>x.classList.toggle("on",x===b));
  });
  wireFocus(dp);
}
function wireFocus(dp){
  // 조례 조문(oc)+법령+조 로 매칭 — 같은 조라도 조례 조문이 다르면 다른 항목
  const citem=(oc,law,cl)=>dp.querySelector(
    `.drec .citem[data-oc="${cssq(oc||"")}"][data-law="${cssq(law)}"][data-clause="${cssq(cl||"")}"]`)
    || dp.querySelector(`.drec .citem[data-law="${cssq(law)}"][data-clause="${cssq(cl||"")}"]`);
  // 모바일: 인용 클릭 시 검토 탭 자동 노출(좌 본문에선 우 검토가 숨겨져 있으므로)
  const showRec=()=>{
    if(!window.matchMedia("(max-width:760px)").matches)return;
    const ds=dp.querySelector(".dsplit");
    ds.classList.remove("show-body"); ds.classList.add("show-rec");
    dp.querySelectorAll(".mtabs button").forEach(x=>x.classList.toggle("on",x.dataset.t==="rec"));
  };
  // 좌 인용 클릭 → 우 해당 검토항목 펼침 + 강조
  dp.querySelectorAll(".dbody mark[data-law]").forEach(mk=>mk.onclick=()=>{
    showRec();
    if(mk.classList.contains("cite-local")){showLocalRef(mk.dataset.law);return;}
    // 상위법령 인용 → 우측에 법령 정보 카드(항상) + 검토항목 있으면 펼침/강조
    showLawRef(mk.dataset.law);
    const it=citem(mk.dataset.oc, mk.dataset.law, mk.dataset.clause);
    if(it){it.open=true; it.classList.add("focus");
      setTimeout(()=>it.classList.remove("focus"),1600); flash(it);}
  });
  // 우 검토항목 펼침 → 좌 본문의 해당 인용 강조(스크롤 다툼 방지 위해 강조만)
  dp.querySelectorAll(".drec .citem").forEach(it=>it.addEventListener("toggle",()=>{
    if(!it.open)return;
    dp.querySelectorAll(`.dbody mark[data-oc="${cssq(it.dataset.oc||"")}"][data-law="${cssq(it.dataset.law)}"][data-clause="${cssq(it.dataset.clause||"")}"]`)
      .forEach(mk=>{mk.classList.add("cite-focus");
        setTimeout(()=>mk.classList.remove("cite-focus"),1600);});
  }));
  // 우 조문 헤더 클릭 → 좌 본문 해당 조문으로 스크롤
  dp.querySelectorAll(".drec .artsec[data-oc]").forEach(sec=>{
    const hd=sec.querySelector(".arthd");if(hd){hd.style.cursor="pointer";
      hd.onclick=()=>flash(dp.querySelector(`.dbody .art[data-oc="${cssq(sec.dataset.oc)}"]`));}
  });
}
function cssq(s){return String(s).replace(/["\\]/g,"\\$&");}
function showLocalRef(name){
  // 타 조례(자치법규) 인용 클릭 → 우측에 그 조례 미니 정보 카드.
  // 상위법령·행정규칙 카드와 동일하게 국가법령정보센터 포털 링크를 함께 제공(일관성).
  const box=document.getElementById("localref"); if(!box)return;
  const ref=LOCALREFS[String(name||"").replace(/\s/g,"")];
  const disp=ref?ref.name:name;
  const portal=`https://www.law.go.kr/자치법규/${encodeURIComponent(disp)}`;
  const portalBtn=`<a class="btn" href="${portal}" target="_blank" rel="noopener">국가법령정보센터에서 보기 ↗</a>`;
  const inner = ref
    ? `<div class="h">「${esc(ref.name)}」<span class="tag">자치법규</span></div>
       <div class="m">담당 ${esc(ref.dept||"—")} · 시행 ${fdate(ref.enforce_date)}</div>
       <button class="btn" onclick="selectOrd('${esc(ref.mst)}')">이 조례 열기 →</button> ${portalBtn}`
    : `<div class="h">「${esc(name)}」<span class="tag">자치법규</span></div>
       <div class="m muted">수집 범위 외 — 군포시 조례가 아니거나 미수집(포털에서 원문 확인)</div>
       ${portalBtn}`;
  box.innerHTML=`<div class="lref">${inner}<span class="x" title="닫기"
       onclick="document.getElementById('localref').innerHTML=''">×</span></div>`;
  box.scrollIntoView({behavior:"smooth",block:"nearest"});
}
function showLawRef(name){
  // 상위법령 인용 클릭 → 우측에 그 법령 간단 정보(인용 조문·판정 요약·국가법령정보센터 링크)
  const box=document.getElementById("localref"); if(!box)return;
  const ref=LAWREFS[String(name||"").replace(/\s/g,"")];
  const law=ref?ref.name:name;
  // 세 갈래: 지자체 자체 행정규칙(API 미수록) / 중앙 행정규칙(ADM:) / 상위법령 — 태그·포털 구분
  const isLocalAdm=ref&&ref.local_admrul;
  const isAdm=ref&&String(ref.law_id||"").indexOf("ADM:")===0;
  const kindTag=isLocalAdm?'지자체 행정규칙':isAdm?'행정규칙':'상위법령';
  const idLabel=isAdm?"행정규칙ID "+esc(String(ref.law_id).slice(4)):"법령ID "+esc(ref?ref.law_id:"");
  const portal=isLocalAdm?`https://www.law.go.kr/자치법규/${encodeURIComponent(law)}`
    :isAdm?`https://www.law.go.kr/행정규칙/${encodeURIComponent(law)}`
    :`https://www.law.go.kr/법령/${encodeURIComponent(law)}`;
  const SEV={mechanical:["번호정정","#1d4ed8"],review:["검토","#b45309"],
    check:["확인","#52525b"],format:["서식","#6d28d9"],current:["변경없음","#15803d"]};
  let body="";
  if(ref){
    const cls=(ref.clauses||[]).slice(0,12).map(c=>`<span class="rcl">${esc(c)}</span>`).join("")
      +((ref.clauses||[]).length>12?` <span class="muted">외 ${ref.clauses.length-12}</span>`:"");
    const sum=Object.entries(ref.counts||{}).filter(([,n])=>n)
      .map(([g,n])=>`<span class="rsum" style="color:${(SEV[g]||['',''])[1]}">${(SEV[g]||[g])[0]} ${n}</span>`).join(" · ")
      ||'<span class="muted">판정 항목 없음(근거 인용)</span>';
    const idNote=isLocalAdm?" · 지자체 자체 행정규칙(국가법령정보 미수록) — 포털 검색으로 원문 확인"
      :isAdm?" · 시점 개정이력 없음(존재·현행 확인)":"";
    body=`<div class="m">인용 조문: ${cls||'<span class="muted">법명만 인용</span>'}</div>
       <div class="m">판정: ${sum}${ref.law_id?` · <span class="muted">${idLabel}</span>`:""}${idNote}</div>`;
  }else{
    body=`<div class="m muted">이 조례에서 인용한 상위법령</div>`;
  }
  box.innerHTML=`<div class="lref law"><div class="h">「${esc(law)}」<span class="tag tlaw">${kindTag}</span></div>
     ${body}
     <a class="btn" href="${portal}" target="_blank" rel="noopener">국가법령정보센터에서 보기 ↗</a>
     <span class="x" title="닫기" onclick="document.getElementById('localref').innerHTML=''">×</span></div>`;
  box.scrollIntoView({behavior:"smooth",block:"nearest"});
}
function closeDetail(){curMst="";
  document.getElementById("detailPanel").style.display="none";
  document.getElementById("layout").classList.remove("detail-open");}

function selectDept(dept){
  curDept=dept;
  if(curQuery){curQuery="";document.getElementById("searchBox").value="";}  // 과 선택 시 검색 해제
  document.getElementById("deptSel").value=dept;
  refresh();
}
function clearSearch(){
  document.getElementById("searchBox").value=""; curQuery=""; refresh();
}
function selectGrade(k){
  if(curGrade===k){clearGrade();return;}          // 같은 등급 재클릭 = 해제(토글)
  curGrade=k;
  const ac=document.getElementById("actChk"); if(ac)ac.checked=true;  // 등급 필터는 정비 대상 기준
  renderHero();                                   // 선택 칩 강조 갱신
  refresh();
}
function clearGrade(){
  curGrade="";
  renderHero();
  refresh();
}
async function renderGradeList(){
  // 과 미선택 상태에서 등급 칩만으로 전체 조례를 필터(담당과 교차 표시)
  let list=await fetchOrds({action:document.getElementById("actChk").checked});
  list=sortOrds(list.filter(o=>o.grades&&o.grades[curGrade]>0));
  const P=document.getElementById("listPanel");
  const m=GM[curGrade]||{emoji:"",label:curGrade};
  if(!list.length){P.innerHTML=`<div class="empty">${m.emoji} ${esc(m.label)} 대상 조례가 없습니다.</div>`;return;}
  P.innerHTML=`<div class="listhd">${m.emoji} ${esc(m.label)} · 조례 ${list.length}건</div><div class="olist">`
    +list.map(o=>_ordCard(o,true)).join("")+`</div>`;
  P.querySelectorAll(".ocard[data-mst]").forEach(c=>c.onclick=()=>selectOrd(c.dataset.mst));
}
function renderCrumb(){
  const c=document.getElementById("crumb");
  const m=GM[curGrade];
  const gb=curGrade&&m?`<span class="gradebadge g-${gcls(curGrade)}">${m.emoji} ${esc(m.label)}`
    +`<a class="gdl" href="${gradeReportHref(curGrade)}" download title="이 등급 전 부서 정비목록 CSV 다운로드(엑셀)">⬇ CSV</a>`
    +`<a class="gx" onclick="clearGrade()" title="등급 필터 해제">×</a></span>`:"";
  if(curQuery){c.innerHTML=`<b class="crumb-cur">검색 “${esc(curQuery)}”</b>`+gb;return;}
  if(!curDept){c.innerHTML=gb;return;}
  c.innerHTML=`<b class="crumb-cur">${esc(curDept)}</b>`
    +`<a class="crumb-link" href="${deptReportHref(curDept,'html')}" target="_blank">🖨 과별 리포트</a>`
    +`<a class="crumb-link" href="${deptReportHref(curDept,'csv')}">CSV 내려받기</a>`+gb;
}
async function goHome(){
  // 첫페이지로 복귀 + 데이터 새로고침(overview·changes 재조회) — 상세/검색/과 선택 초기화
  const sb=document.getElementById("searchBox"); if(sb)sb.value="";
  const ds=document.getElementById("deptSel"); if(ds)ds.value="";
  const ac=document.getElementById("actChk"); if(ac)ac.checked=false;
  curDept=""; curQuery=""; curGrade=""; _ALLORDS=null;
  closeDetail();
  try{OV=await getJSON("/api/overview");}catch(e){}
  renderBanner(); renderHero(); renderChanges();
  renderCrumb(); renderDeptTable();
}
async function refresh(){
  closeDetail();
  renderCrumb();
  if(curQuery) await renderSearch();
  else if(curDept) await renderOrdList();
  else if(curGrade) await renderGradeList();
  else renderDeptTable();
}

async function init(){
  OV=await getJSON("/api/overview");
  GO.push(...OV.grade_order); Object.assign(GM,OV.grade_meta);
  OV.depts.forEach((d,i)=>DEPTIDX[d.dept]=i);     // 정적 과별 리포트 파일 인덱스
  if(STATIC){const al=document.getElementById("adminLink"); if(al)al.style.display="none";}
  renderBanner(); renderHero(); fillDeptSelect(); renderChanges();
  document.getElementById("deptSel").onchange=e=>selectDept(e.target.value);
  document.getElementById("actChk").onchange=()=>{
    if(curQuery)renderSearch();else if(curDept)renderOrdList();
    else if(curGrade)renderGradeList();};
  document.getElementById("sortSel").onchange=e=>{curSort=e.target.value;
    if(curQuery)renderSearch();else if(curDept)renderOrdList();
    else if(curGrade)renderGradeList();};
  const sb=document.getElementById("searchBox");
  let _t=null;
  sb.oninput=()=>{clearTimeout(_t);_t=setTimeout(()=>{
    const v=sb.value.trim();
    if(v===curQuery)return;
    curQuery=v; refresh();
  },200);};
  refresh();
}
init();
</script>
</body></html>
"""


# ---------- 관리자: 판정 근거 검사기(단일 HTML, 의존성 0) ----------
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>판정 근거 검사 — 관리자</title>
<style>
body{font-family:-apple-system,"Malgun Gothic",sans-serif;margin:0;background:#f8fafc;color:#111827;}
.wrap{max-width:1280px;margin:0 auto;padding:18px 22px;}
h1{font-size:19px;margin:0 0 4px;}
.sub{color:#6b7280;font-size:13px;margin:0 0 14px;}
.sub a{color:#2563eb;text-decoration:none;}
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 12px;}
.controls select,.controls input{font-size:13.5px;padding:6px 10px;border:1px solid #d1d5db;border-radius:8px;outline:none;}
.controls input{min-width:240px;}
.controls input:focus,.controls select:focus{border-color:#2563eb;box-shadow:0 0 0 2px #bfdbfe;}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin:0 0 12px;}
.schip{font-size:12.5px;border:1px solid #e5e7eb;background:#fff;border-radius:999px;padding:4px 12px;
  cursor:pointer;color:#374151;}
.schip:hover{background:#f9fafb;}
.schip.on{border-color:#2563eb;background:#eff6ff;color:#1d4ed8;font-weight:700;}
.schip b{font-weight:700;}
.layout{display:grid;grid-template-columns:minmax(380px,1fr) minmax(420px,1.25fr);gap:14px;align-items:start;}
.panel{background:#fff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #f1f5f9;vertical-align:top;}
th{background:#f8fafc;color:#6b7280;font-weight:600;position:sticky;top:0;}
.flist{max-height:78vh;overflow:auto;}
.flist tr{cursor:pointer;}
.flist tr:hover{background:#f9fafb;}
.flist tr.sel{background:#eff6ff;}
.sev{font-size:11px;font-weight:700;border-radius:999px;padding:1px 8px;white-space:nowrap;}
.sev.mechanical{background:#dbeafe;color:#1e40af;}
.sev.review{background:#fef3c7;color:#92400e;}
.sev.check{background:#e5e7eb;color:#374151;}
.sev.current{background:#dcfce7;color:#166534;}
.muted{color:#9ca3af;}
.trace{padding:14px 16px;max-height:78vh;overflow:auto;}
.thead{font-size:15px;font-weight:700;margin:0 0 2px;}
.tmeta{font-size:12.5px;color:#6b7280;margin:0 0 10px;}
.verdict{display:flex;gap:10px;align-items:center;margin:0 0 14px;padding:9px 12px;border-radius:9px;font-size:13px;}
.verdict.ok{background:#f0fdf4;border:1px solid #bbf7d0;}
.verdict.bad{background:#fef2f2;border:1px solid #fecaca;}
.verdict.na{background:#f8fafc;border:1px solid #e5e7eb;}
.badge{font-weight:700;font-size:12px;border-radius:999px;padding:2px 10px;}
.badge.ok{background:#16a34a;color:#fff;}
.badge.bad{background:#dc2626;color:#fff;}
.step{border:1px solid #eef2f7;border-left:3px solid #93c5fd;border-radius:8px;padding:9px 12px;margin:0 0 9px;}
.step .k{font-size:12.5px;font-weight:700;color:#1d4ed8;margin:0 0 4px;}
.step .d{font-size:11.5px;color:#6b7280;margin:0 0 6px;}
.kv{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12.5px;margin:2px 0;}
.kv b{color:#374151;}
.chiprow{display:flex;flex-wrap:wrap;gap:5px;margin:3px 0;}
.chip{font-size:11.5px;background:#f1f5f9;border-radius:6px;padding:1px 7px;color:#334155;}
.yes{color:#16a34a;font-weight:700;}.no{color:#dc2626;font-weight:700;}
pre{white-space:pre-wrap;word-break:break-word;font-family:"SFMono-Regular",Consolas,monospace;
  font-size:12px;background:#f8fafc;border:1px solid #eef2f7;border-radius:7px;padding:8px 10px;margin:4px 0 0;
  max-height:230px;overflow:auto;}
.amd{background:#fde68a;color:#92400e;border-radius:3px;padding:0 2px;font-weight:600;}
.cmp{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.cmp .lbl{font-size:11px;font-weight:700;color:#6b7280;}
.empty{padding:30px;text-align:center;color:#9ca3af;font-size:13px;}
</style></head>
<body><div class="wrap">
  <h1>판정 근거 검사기 <span class="muted" style="font-size:13px;font-weight:400">(관리자 · 라이브 API 0)</span></h1>
  <p class="sub">finding 을 만든 검토 로직을 DB 원본으로 <b>그대로 재실행</b>해 단계별 신호를 보여줍니다.
     재판정과 저장값을 대조해 <b>일치 여부</b>도 표시(불일치=파서/로직이 배치 이후 바뀜 → reparse 필요).
     · <a href="/">← 대시보드</a></p>
  <div class="controls">
    <input type="search" id="q" placeholder="🔍 법령명·조례명">
    <select id="sev"><option value="">등급 — 전체</option>
      <option value="mechanical">🔧 기계적</option><option value="review">⚠️ 검토</option>
      <option value="check">📋 확인</option><option value="current">✅ 현행</option></select>
    <select id="ct"><option value="">변경유형 — 전체</option>
      <option>내용변경</option><option>동일</option><option>번호이동</option>
      <option>삭제</option><option>당시부재</option><option>미확인</option>
      <option>제명변경</option><option>법령미해결</option></select>
    <span class="muted" id="count"></span>
  </div>
  <div class="chips" id="chips"></div>
  <div class="layout">
    <div class="panel"><div class="flist" id="flist"></div></div>
    <div class="panel"><div class="trace" id="trace"><div class="empty">왼쪽에서 finding 을 선택하세요.</div></div></div>
  </div>
</div>
<script>
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function fd(s){const d=String(s||"").replace(/[^0-9]/g,"").slice(0,8);
  return d.length===8?`${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}`:(s||"—");}
const SEVK={mechanical:"🔧기계적",review:"⚠️검토",check:"📋확인",current:"✅현행"};
let SEL=null;
async function getJSON(u){const r=await fetch(u);return r.json();}

function amdHi(text){ // <개정 …> 태그 강조(escape 후 래핑)
  return esc(text).replace(/(&lt;(?:개정|신설|전문개정)[^&]*?&gt;)/g,'<span class="amd">$1</span>');}

function renderChips(counts,cur){
  // 등급별 표본 칩 — 검토뿐 아니라 '제외(현행)'까지 한클릭. current=정비 제외 판정.
  const order=[["","전체"],["mechanical","🔧 기계적"],["review","⚠️ 검토"],
    ["check","📋 확인"],["current","✅ 제외(현행)"]];
  const tot=Object.values(counts).reduce((a,b)=>a+b,0);
  document.getElementById("chips").innerHTML=order.map(([k,lbl])=>{
    const n=k===""?tot:(counts[k]||0);
    return `<span class="schip${cur===k?" on":""}" data-sev="${k}">${lbl} <b>${n}</b></span>`;}).join("");
  document.querySelectorAll("#chips .schip").forEach(c=>c.onclick=()=>{
    document.getElementById("sev").value=c.dataset.sev; loadList();});
}

async function loadList(){
  const q=document.getElementById("q").value.trim();
  const sev=document.getElementById("sev").value, ct=document.getElementById("ct").value;
  const u=`/api/admin/findings?limit=300`+(q?`&q=${encodeURIComponent(q)}`:"")
    +(sev?`&severity=${sev}`:"")+(ct?`&change_type=${encodeURIComponent(ct)}`:"");
  const r=await getJSON(u);
  document.getElementById("count").textContent=`${r.shown} / ${r.total}건`+(r.shown<r.total?" (상위 300 표본)":"");
  renderChips(r.counts||{}, sev);
  if(!r.findings.length){document.getElementById("flist").innerHTML=`<div class="empty">해당 finding 없음.</div>`;return;}
  const rows=r.findings.map(f=>`<tr data-id="${f.id}">
    <td>${esc(f.ord_name)}<div class="muted" style="font-size:11px">${esc(f.dept||"")}</div></td>
    <td>「${esc(f.law_name)}」 ${esc(f.clause_label)}${esc(f.clause_detail||"")}</td>
    <td><span class="sev ${f.severity}">${SEVK[f.severity]||f.severity}</span>
        ${f.change_type?`<div class="muted" style="font-size:11px;margin-top:2px">${esc(f.change_type)}</div>`:""}</td></tr>`).join("");
  document.getElementById("flist").innerHTML=
    `<table><thead><tr><th>조례</th><th>인용 상위법</th><th>판정</th></tr></thead><tbody>${rows}</tbody></table>`;
  document.querySelectorAll("#flist tr[data-id]").forEach(tr=>tr.onclick=()=>selectF(tr.dataset.id,tr));
}

async function selectF(id,tr){
  document.querySelectorAll("#flist tr.sel").forEach(x=>x.classList.remove("sel"));
  if(tr)tr.classList.add("sel");
  const t=await getJSON(`/api/admin/trace?id=${id}`);
  renderTrace(t);
}

function vline(label,val,cls){return `<span class="kv"><b>${label}</b> <span class="${cls||""}">${val}</span></span>`;}

function renderStep(s){
  let body="";
  const d=s.detail?`<div class="d">${esc(s.detail)}</div>`:"";
  if(s.k==="현행 조문 원본"){
    body=`<div class="kv"><b>존재</b> <span class="${s.found?'yes':'no'}">${s.found?"있음":"없음"}</span>`
      +(s.clause_enforce?` · <b>조문시행일</b> ${fd(s.clause_enforce)}`:"")+`</div>`
      +(s.note?`<div class="d no">${esc(s.note)}</div>`:"")
      +(s.content?`<pre>${amdHi(s.content)}</pre>`:"");
  }else if(s.k.indexOf("amend_dates")>=0){
    body=`<div class="chiprow">${(s.dates||[]).map(x=>`<span class="chip">${fd(x)}</span>`).join("")||'<span class="muted">없음</span>'}</div>`;
  }else if(s.k==="basis_dates"){
    body=`<div class="kv"><b>조례시행</b> ${fd(s.ord_enforce)} · <b>당시기준(was)</b> ${esc(s.was||"—")} · `
      +`<b>현행기준(now)</b> ${s.now?fd(s.now):"—"} · <b>시행후개정</b> `
      +`<span class="${s.amended_after?'no':'yes'}">${s.amended_after===true?"있음→검토":s.amended_after===false?"없음→현행":"판별불가"}</span></div>`;
  }else if(s.k.indexOf("as_of")>=0){
    body=`<div class="kv"><b>당시본 수</b> ${s.n_versions} · `
      +(s.selected_mst?`<b>선택</b> 시행 ${fd(s.selected_enforce)} (mst ${esc(s.selected_mst)})`
        :`<span class="no">당시본 못 고름 → check_clause 폴백</span>`)+`</div>`;
  }else if(s.k.indexOf("좁히기")>=0){
    body=`<div class="kv"><b>대상</b> ${esc(s.subspec)} · <b>좁히기</b> `
      +`<span class="${s.narrowed?'yes':'no'}">${s.narrowed?"성공":"실패→조 전체 폴백"}</span>`
      +(s.narrowed?` · <b>동일</b> <span class="${s.equal?'yes':'no'}">${s.equal?"예":"아니오(변경)"}</span>`:"")+`</div>`
      +(s.narrowed?`<div class="cmp"><div><div class="lbl">[당시]</div><pre>${esc(s.old_sub)}</pre></div>`
        +`<div><div class="lbl">[현행]</div><pre>${esc(s.cur_sub)}</pre></div></div>`:"");
  }else{
    body=`<div class="kv">${esc(JSON.stringify(Object.fromEntries(Object.entries(s).filter(([k])=>k!=="k"&&k!=="detail"))))}</div>`;
  }
  return `<div class="step"><div class="k">${esc(s.k)}</div>${d}${body}</div>`;
}

function renderTrace(t){
  const box=document.getElementById("trace");
  if(t.error){box.innerHTML=`<div class="empty">${esc(t.error)}</div>`;return;}
  const f=t.finding, o=t.ordinance;
  let v;
  if(t.match===null) v=`<div class="verdict na">${esc(t.reason||"재실행 대조 대상 아님")}</div>`;
  else{
    const rc=t.recomputed;
    const ok=t.match;
    v=`<div class="verdict ${ok?'ok':'bad'}"><span class="badge ${ok?'ok':'bad'}">${ok?"✓ 일치":"⚠ 불일치"}</span>
       <span>저장: <b>${SEVK[f.severity]||f.severity}</b>${f.change_type?" / "+esc(f.change_type):""}
       &nbsp;↔&nbsp; 재판정: <b>${SEVK[rc.severity]||rc.severity}</b>${rc.change_type?" / "+esc(rc.change_type):""}</span></div>`
       +(!ok?`<div class="d" style="margin:-8px 0 12px;color:#b91c1c">불일치 = 파서/로직이 배치 이후 변경됨 → <b>--reparse</b> 권장</div>`:"");
  }
  box.innerHTML=`<div class="thead">${esc(o.name||"조례")} <span class="muted">·</span> 「${esc(f.law_name)}」 ${esc(f.clause_label)}${esc(f.clause_detail||"")}</div>
    <div class="tmeta">조례 시행 ${fd(o.enforce_date)} · 담당 ${esc(o.dept||"—")} · 인용 위치 ${esc(f.ord_clause||"—")} · deep=${t.deep}</div>
    ${v}
    <div class="d" style="margin:0 0 8px"><b>저장 detail:</b> ${esc(f.detail||"")}</div>
    ${t.steps.map(renderStep).join("")}`;
}

document.getElementById("q").oninput=(()=>{let h;return()=>{clearTimeout(h);h=setTimeout(loadList,220);};})();
document.getElementById("sev").onchange=loadList;
document.getElementById("ct").onchange=loadList;
loadList();
</script>
</body></html>
"""


# ---------- 정적 사이트 생성(서버 없이 GitHub Pages 등 호스팅) ----------
def export_static(out_dir="site", db_path=db.DEFAULT_DB, verbose=True):
    """DB를 읽어 서버 없이 열람 가능한 정적 사이트를 출력한다(공개 호스팅용).

    대시보드 HTML(정적 모드) + api/*.json + 조례별 권고서 HTML + 과별 리포트.
    검색은 브라우저에서 클라이언트 필터로 동작(전체 목록에 인용 법령명 동봉).
    제외: /admin(전체 DB 필요)·검토완료(ack, DB 쓰기) — 읽기 전용 공개본.
    """
    import os
    import json as _json
    from .batch import law_changes_report

    out = os.path.abspath(out_dir)
    for sub in ("", "api", "api/ordinance", "report", "dept_report", "grade_report"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)

    def w(path, text):
        with open(os.path.join(out, path), "w", encoding="utf-8") as f:
            f.write(text)

    def wj(path, obj):
        w(path, _json.dumps(obj, ensure_ascii=False))

    # 대시보드(정적 모드 토글) — 라이브 엔드포인트 대신 정적 파일을 읽도록
    w("index.html", DASHBOARD_HTML.replace("const STATIC=false;", "const STATIC=true;"))

    ov = overview(db_path)
    wj("api/overview.json", ov)

    # 전체 조례 목록 + 인용 법령명(검색용, 클라 필터)
    ords = list_ordinances(db_path)
    conn = db.connect(db_path)
    laws_by = {}
    for r in conn.execute("SELECT DISTINCT mst, law_name FROM citations "
                          "WHERE cite_type='법령' AND law_name != ''"):
        laws_by.setdefault(r["mst"], []).append(r["law_name"])
    conn.close()
    for o in ords:
        o["laws"] = sorted(laws_by.get(o["mst"], []))
    wj("api/ordinances.json", ords)

    wj("api/changes.json", law_changes_report(db_path))

    # 조례별 상세(JSON) + 분석 권고서(HTML)
    for o in ords:
        mst = o["mst"]
        wj(f"api/ordinance/{mst}.json", ordinance_detail(db_path, mst))
        model = build_model(db_path, mst=mst, include_current=True)
        name = model["ordinances"][0]["name"] if model["ordinances"] else mst
        w(f"report/{mst}.html", render_html(model, title=f"{name} — 정비 권고(분석 결과)"))

    # 과별 리포트 — overview 순서 인덱스 = 대시보드 링크(deptReportHref)와 일치
    for i, d in enumerate(ov["depts"]):
        if not d.get("action"):
            continue
        m = build_model(db_path, dept=d["dept"])
        w(f"dept_report/{i}.html", render_html(m, title=f"{d['dept']} 자치법규 정비 권고"))
        w(f"dept_report/{i}.csv", model_to_csv(m, dept=d["dept"]))

    # 등급별 전 부서 정비목록 CSV (의법팀이 등급별로 뽑아 부서 공문 발송·법무 내부 정비)
    for g in GRADE_KEYS:
        if g == "current":
            continue
        w(f"grade_report/{g}.csv", grade_to_csv(db_path, g))

    nfiles = sum(len(fs) for _, _, fs in os.walk(out))
    if verbose:
        print(f"정적 사이트 생성 → {out}  (조례 {len(ords)}건 · 파일 {nfiles}개)")
        print("  GitHub Pages: 이 폴더 내용을 공개 리포에 올리고 Pages 활성화")
    return {"out": out, "ordinances": len(ords), "files": nfiles}
