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
from .extract import normalize_text
from .report import (GRADE_KEYS, GRADE_META, finding_grade, recommend_fragment,
                     build_model, render_html, model_to_csv)


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

    # 정비 대상이 많은 과를 위로
    dept_list = sorted(depts.values(), key=lambda d: (-d["action"], d["dept"]))
    return {
        "batch": (dict(meta) if meta else {}),
        "grades": grades,
        "grade_meta": {k: {"emoji": GRADE_META[k]["emoji"],
                           "label": GRADE_META[k]["label"]} for k in GRADE_KEYS},
        "grade_order": list(GRADE_KEYS),
        "totals": {"ordinances": len(ords),
                   "action": sum(1 for o in ords if o["items_count"] > 0),
                   "depts": len(dept_list)},
        "depts": dept_list,
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


def _highlight_article(no, body, cites):
    """조례 조문 본문(정규화)에 인용 span을 <mark>로 감싼 HTML.

    span은 normalize_text(조문본문) 기준 offset이므로 같은 정규화 텍스트에 적용한다.
    겹치거나 범위를 벗어난 span은 건너뛴다(안전).
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
        # 타 조례(자치법규)는 상위법령 정합성 검토 대상이 아니라 회색으로 구분(우측 검토에 없음)
        if c.get("cite_type") == "자치법규":
            cls = "cite-local"
        elif c.get("cite_naked"):
            cls = "cite-naked"
        else:
            cls = "cite-law"
        out.append(
            f'<mark class="{cls}" data-oc="{_esc(no)}" data-law="{_esc(c["law_name"])}"'
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
        """SELECT article_no, law_name, clause_label, span_start, span_end,
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
                articles.append({
                    "no": no, "title": a.get("title", ""),
                    "html": _highlight_article(no, a["body"], ac),
                    "cites": len(ac),
                })

    meta = {k: o[k] for k in o.keys() if k != "body_xml"} if o else {}
    return {"meta": meta, "articles": articles, "local_refs": local_refs,
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
        self._send(json.dumps(obj, ensure_ascii=False),
                   status=status, ctype="application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        try:
            if path == "/" or path == "/index.html":
                return self._send(DASHBOARD_HTML, ctype="text/html; charset=utf-8")
            if path == "/admin":
                return self._send(ADMIN_HTML, ctype="text/html; charset=utf-8")
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
.controls .crumb{font-size:13px;color:#6b7280;}
.controls .crumb a{color:#2563eb;cursor:pointer;text-decoration:none;}
.layout{display:grid;grid-template-columns:1fr;gap:16px;}
.panel{background:#fff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;}
table{width:100%;border-collapse:collapse;font-size:13.5px;}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid #f1f3f5;}
th{background:#f9fafb;color:#6b7280;font-weight:600;font-size:12px;position:sticky;top:0;}
tbody tr{cursor:pointer;}
tbody tr:hover{background:#f8fafc;}
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
.detail .body{padding:8px 14px 18px;}
.empty{padding:40px 16px;color:#9ca3af;text-align:center;font-size:14px;}
.btn{font-size:12px;padding:4px 10px;border:1px solid #d1d5db;border-radius:7px;
     background:#fff;cursor:pointer;color:#374151;}
.muted{color:#9ca3af;}
/* 단계4 통합 상세: 좌 본문(하이라이트) | 우 권고 */
.layout.detail-open #listPanel{display:none;}
.layout.detail-open{grid-template-columns:1fr;}
.dsplit{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);}
.dbody{padding:12px 16px;border-right:1px solid #eef0f3;max-height:76vh;overflow:auto;}
.drec{padding:6px 14px;max-height:76vh;overflow:auto;}
.dcolhd{font-size:12px;color:#6b7280;font-weight:600;margin:2px 0 8px;}
.art{margin:0 0 10px;scroll-margin-top:8px;border:1px solid #eef0f3;border-radius:9px;
     padding:9px 13px;background:#fff;}
.art.cited{border-left:3px solid #2563eb;}
.art.nocite{background:#fafafa;opacity:.62;}
.art .ahd{font-size:12px;font-weight:700;color:#1e3a8a;margin-bottom:5px;
          display:flex;align-items:center;gap:7px;}
.art.nocite .ahd{color:#9ca3af;}
.art .ahd .ct{font-size:10.5px;font-weight:600;background:#dbeafe;color:#1e40af;
              border-radius:999px;padding:1px 8px;}
.art .atext{font-size:13px;line-height:1.95;white-space:pre-wrap;color:#1f2937;
            word-break:break-word;}
mark.cite-law{background:#dbeafe;color:#1e3a8a;border-radius:3px;padding:0 2px;cursor:pointer;
              transition:background .15s;}
mark.cite-law:hover{background:#bfdbfe;}
mark.cite-naked{background:#ede9fe;color:#5b21b6;border-radius:3px;padding:0 2px;cursor:pointer;}
mark.cite-naked:hover{background:#ddd6fe;}
mark.cite-local{background:#f1f5f9;color:#64748b;border-radius:3px;padding:0 2px;
                border-bottom:1px dotted #94a3b8;cursor:pointer;}
mark.cite-local:hover{background:#e2e8f0;}
mark.cite-focus{outline:2px solid #f59e0b;outline-offset:1px;}
/* 타 조례(자치법규) 인용 클릭 시 우측 미니 정보 카드 */
.lref{position:relative;border:1px solid #c7d2fe;background:#eef2ff;border-radius:9px;
      padding:10px 28px 10px 12px;margin:0 0 12px;}
.lref .h{font-size:13px;font-weight:600;color:#3730a3;}
.lref .tag{font-size:10.5px;background:#e0e7ff;color:#4338ca;border-radius:999px;
           padding:1px 7px;font-weight:600;margin-left:4px;}
.lref .m{font-size:12px;color:#6366f1;margin:4px 0 8px;}
.lref .x{position:absolute;top:7px;right:10px;cursor:pointer;color:#94a3b8;font-size:16px;}
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
.changes .law .lname{font-size:13.5px;color:#7f1d1d;font-weight:600;}
.changes .law .lmeta{font-size:12px;color:#b91c1c;font-weight:400;margin-left:6px;}
.changes .grp{display:flex;align-items:flex-start;gap:8px;margin:6px 0 0;}
.changes .lbl{flex:0 0 auto;font-size:11px;font-weight:700;border-radius:999px;padding:2px 9px;
              margin-top:2px;white-space:nowrap;}
.changes .lbl.hit{background:#dc2626;color:#fff;}
.changes .lbl.unc{background:#fde68a;color:#92400e;}
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
</style></head>
<body><div class="wrap">
  <h1>자치법규 정비 — 총괄 대시보드
    <a href="/admin" style="float:right;font-size:13px;font-weight:400;color:#6b7280;text-decoration:none">🔧 판정 근거 검사</a></h1>
  <div class="banner" id="banner">불러오는 중…</div>
  <div class="cards" id="cards"></div>
  <div class="changes" id="changes" style="display:none"></div>
  <div class="controls">
    <input type="search" id="searchBox" placeholder="🔍 조례명·인용 법령 검색" autocomplete="off">
    <select id="deptSel"><option value="">담당과 — 전체</option></select>
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
let OV=null, curDept="", curMst="", curQuery="", cssInjected=false, LOCALREFS={};

const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function fdate(s){const d=String(s||"").replace(/[^0-9]/g,"").slice(0,8);
  return d.length===8?`${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}`:(s||"—");}
function dist(grades){ // 등급 분포 pill (current 제외, 0은 생략)
  return GO.filter(k=>k!=="current"&&grades[k]>0)
    .map(k=>`<span class="pill ${PCLS[k]}">${GM[k].emoji} ${grades[k]}</span>`).join("")||'<span class="muted">—</span>';}

async function getJSON(u){const r=await fetch(u);return r.json();}

function renderCards(){
  const g=OV.grades, t=OV.totals;
  let h=`<div class="card"><div class="n">${t.action}</div><div class="t">정비 대상 조례 / 전체 ${t.ordinances}</div></div>`;
  for(const k of GO){const m=GM[k];
    h+=`<div class="card g-${k==="mechanical"?"mech":k==="review"?"rev":k==="check"?"chk":k==="format"?"fmt":"cur"}">
        <div class="n">${g[k]}</div><div class="t">${m.emoji} ${esc(m.label)}</div></div>`;}
  document.getElementById("cards").innerHTML=h;
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
  let nAff=0,nUnc=0; ch.forEach(c=>{nAff+=(c.affected||[]).length;nUnc+=(c.uncertain||[]).length;});
  const chip=o=>`<a data-mst="${esc(o.mst)}" title="${esc(o.dept||"")}">${esc(o.name)}`
    +(o.clauses&&o.clauses.length?` <span class="cl">${esc(o.clauses.join(","))}</span>`:"")+`</a>`;
  const laws=ch.map(c=>{
    const aff=c.affected||[], unc=c.uncertain||[];
    const caTxt=c.changed_articles==="*"?"전부개정"
      :(!c.changed_articles?"바뀐 조문 판별불가":`바뀐 조문 ${esc(c.changed_articles)}`);
    const affHtml=aff.length?`<div class="grp"><span class="lbl hit">해당 ${aff.length}</span>
        <div class="ords">${aff.map(chip).join("")}</div></div>`:"";
    const uncHtml=unc.length?`<div class="grp"><span class="lbl unc">확인 ${unc.length}</span>
        <div class="ords">${unc.map(chip).join("")}</div></div>`:"";
    const empty=(!aff.length&&!unc.length)?'<div class="none">바뀐 조문을 인용한 조례 없음(무관)</div>':"";
    const ackBtn=c.acked
      ?`<button class="ackb done" data-law="${esc(c.law_id)}" data-key="${esc(c.new_key||"")}" data-ack="0">✓ 검토완료 (해제)</button>`
      :`<button class="ackb" data-law="${esc(c.law_id)}" data-key="${esc(c.new_key||"")}" data-ack="1">검토완료 표시</button>`;
    return `<div class="law${c.acked?" acked":""}"><span class="lname">「${esc(c.name)}」</span>
      <span class="lmeta">${esc(c.revise_type||"개정")} · 시행 ${fdate(c.new_enforce)} · ${caTxt}</span>
      ${ackBtn}${affHtml}${uncHtml}${empty}</div>`;
  }).join("");
  const nOpen=ch.filter(c=>!c.acked).length;
  box.innerHTML=`<div class="chd"><span class="badge">🔔 ${nOpen}</span>
     법령 개정 감지 — 해당 조례 ${nAff}건 · 확인 ${nUnc}건
     <span class="muted" style="font-weight:400">· 조례 클릭 시 상세</span>
     <label class="muted chall"><input type="checkbox" id="chAll"${CHANGES_ALL?" checked":""}> 검토완료 포함</label>
     <span class="arr">▸</span></div>
     <div class="clist">${laws}</div>`;
  box.style.display="block";
  box.classList.add("open");
  box.querySelector(".chd").onclick=e=>{if(e.target.id!=="chAll")box.classList.toggle("open");};
  box.querySelector("#chAll").onclick=e=>{e.stopPropagation();CHANGES_ALL=e.target.checked;renderChanges();};
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

function renderDeptTable(){
  let rows=OV.depts.map(d=>{const dq=encodeURIComponent(d.dept);
    const rep=d.action?`<a href="/api/dept_report?dept=${dq}&format=html" target="_blank" onclick="event.stopPropagation()">🖨</a>
       <a href="/api/dept_report?dept=${dq}&format=csv" onclick="event.stopPropagation()">CSV</a>`:'<span class="muted">—</span>';
    return `<tr data-dept="${esc(d.dept)}">
     <td>${esc(d.dept)}</td>
     <td class="num">${d.action}</td><td class="num">${d.total}</td>
     <td class="dist">${dist(d.grades)}</td><td class="rep">${rep}</td></tr>`;}).join("");
  document.getElementById("listPanel").innerHTML=
    `<table><thead><tr><th>담당과</th><th class="num">정비대상</th>
     <th class="num">전체</th><th>등급 분포</th><th>리포트</th></tr></thead><tbody>${rows}</tbody></table>`;
  document.querySelectorAll("#listPanel tr[data-dept]").forEach(tr=>
    tr.onclick=()=>{selectDept(tr.dataset.dept);});
}

async function renderOrdList(){
  const act=document.getElementById("actChk").checked?"&action=1":"";
  const list=await getJSON(`/api/ordinances?dept=${encodeURIComponent(curDept)}${act}`);
  if(!list.length){document.getElementById("listPanel").innerHTML=
    `<div class="empty">해당 조건의 조례가 없습니다.</div>`;return;}
  let rows=list.map(o=>`<tr data-mst="${esc(o.mst)}">
     <td>${esc(o.name)}</td><td class="muted">${fdate(o.enforce_date)}</td>
     <td class="num">${o.items_count}</td><td class="dist">${dist(o.grades)}</td></tr>`).join("");
  document.getElementById("listPanel").innerHTML=
    `<table><thead><tr><th>조례</th><th>시행일</th><th class="num">정비</th>
     <th>등급</th></tr></thead><tbody>${rows}</tbody></table>`;
  document.querySelectorAll("#listPanel tr[data-mst]").forEach(tr=>
    tr.onclick=()=>{selectOrd(tr.dataset.mst);});
}

async function renderSearch(){
  // 자유검색 결과(담당과 교차) — 조례명 또는 인용 법령명 매치. 담당과 컬럼 표시.
  const act=document.getElementById("actChk").checked?"&action=1":"";
  const list=await getJSON(`/api/ordinances?q=${encodeURIComponent(curQuery)}${act}`);
  if(!list.length){document.getElementById("listPanel").innerHTML=
    `<div class="empty">'${esc(curQuery)}' 검색 결과 없음.</div>`;return;}
  let rows=list.map(o=>{
    const hit=(o.law_hits&&o.law_hits.length)
      ?`<span class="lawhit">↳ 인용 법령: ${esc(o.law_hits.slice(0,3).join(", "))}${o.law_hits.length>3?" 외 "+(o.law_hits.length-3):""}</span>`:"";
    return `<tr data-mst="${esc(o.mst)}">
     <td>${esc(o.name)}${hit}</td><td class="muted">${esc(o.dept)}</td>
     <td class="muted">${fdate(o.enforce_date)}</td>
     <td class="num">${o.items_count}</td><td class="dist">${dist(o.grades)}</td></tr>`;}).join("");
  document.getElementById("listPanel").innerHTML=
    `<table><thead><tr><th>조례 (${list.length})</th><th>담당과</th><th>시행일</th>
     <th class="num">정비</th><th>등급</th></tr></thead><tbody>${rows}</tbody></table>`;
  document.querySelectorAll("#listPanel tr[data-mst]").forEach(tr=>
    tr.onclick=()=>{selectOrd(tr.dataset.mst);});
}

function flash(el){if(!el)return;el.classList.remove("flash");void el.offsetWidth;
  el.classList.add("flash");el.scrollIntoView({behavior:"smooth",block:"center"});}

async function selectOrd(mst){
  curMst=mst;
  const d=await getJSON(`/api/ordinance/${encodeURIComponent(mst)}`);
  const r=d.recommend||{}, m=d.meta||{}, arts=d.articles||[];
  LOCALREFS=d.local_refs||{};
  if(r.css && !cssInjected){const s=document.createElement("style");
    s.textContent=r.css;document.head.appendChild(s);cssInjected=true;}
  const meta=`시행 ${fdate(m.enforce_date)} · 담당 ${esc(m.dept||"—")}`
    +(m.phone?` · ☎ ${esc(m.phone)}`:"");
  // 좌: 본문(조문별, 인용 하이라이트)
  const left=arts.length?arts.map(a=>`<div class="art ${a.cites?'cited':'nocite'}" data-oc="${esc(a.no)}">
      <div class="ahd">${esc(a.no)}${a.title?" ("+esc(a.title)+")":""}${a.cites?`<span class="ct">인용 ${a.cites}</span>`:""}</div>
      <div class="atext">${a.html}</div></div>`).join("")
    :`<div class="empty">본문이 없습니다(body_xml 미적재 — 재배치 필요).</div>`;
  // 우: 권고(report 조각)
  const right=r.found?r.html:`<div class="empty">정비 항목이 없습니다(현행 유지).</div>`;
  const dp=document.getElementById("detailPanel");
  dp.innerHTML=`<div class="dhd"><button class="btn" onclick="closeDetail()">← 목록</button>
     <a class="btn" style="margin-left:8px;text-decoration:none" target="_blank"
        href="/api/ordinance_report?mst=${encodeURIComponent(mst)}&format=html">📄 분석 권고서(인쇄용)</a>
     <h2 style="margin-top:8px">${esc(m.name||"조례")}</h2><div class="m">${meta}</div></div>
     <div class="dsplit">
       <div class="dbody"><div class="dcolhd">📄 조례 본문 — <span style="color:#1e3a8a">상위법령 「」</span> / <span style="color:#5b21b6">맨몸</span> / <span style="color:#64748b">타 조례(클릭 시 정보)</span></div>${left}</div>
       <div class="drec"><div class="dcolhd">🔧 검토 사항 — 조례 조문별 · 좌측 인용 클릭 시 펼침</div><div id="localref"></div>${right}</div>
     </div>`;
  dp.style.display="block";
  document.getElementById("layout").classList.add("detail-open");
  wireFocus(dp);
}
function wireFocus(dp){
  // 조례 조문(oc)+법령+조 로 매칭 — 같은 조라도 조례 조문이 다르면 다른 항목
  const citem=(oc,law,cl)=>dp.querySelector(
    `.drec .citem[data-oc="${cssq(oc||"")}"][data-law="${cssq(law)}"][data-clause="${cssq(cl||"")}"]`)
    || dp.querySelector(`.drec .citem[data-law="${cssq(law)}"][data-clause="${cssq(cl||"")}"]`);
  // 좌 인용 클릭 → 우 해당 검토항목 펼침 + 강조
  dp.querySelectorAll(".dbody mark[data-law]").forEach(mk=>mk.onclick=()=>{
    if(mk.classList.contains("cite-local")){showLocalRef(mk.dataset.law);return;}
    const it=citem(mk.dataset.oc, mk.dataset.law, mk.dataset.clause);
    if(it){it.open=true; it.classList.add("focus");
      setTimeout(()=>it.classList.remove("focus"),1600); flash(it);}
    else flash(dp.querySelector(`.drec .artsec[data-oc="${cssq(mk.dataset.oc)}"]`));
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
  // 타 조례(자치법규) 인용 클릭 → 우측에 그 조례 미니 정보 카드(이름·담당과·시행일 + 열기)
  const box=document.getElementById("localref"); if(!box)return;
  const ref=LOCALREFS[String(name||"").replace(/\s/g,"")];
  const inner = ref
    ? `<div class="h">「${esc(ref.name)}」<span class="tag">자치법규</span></div>
       <div class="m">담당 ${esc(ref.dept||"—")} · 시행 ${fdate(ref.enforce_date)}</div>
       <button class="btn" onclick="selectOrd('${esc(ref.mst)}')">이 조례 열기 →</button>`
    : `<div class="h">「${esc(name)}」<span class="tag">자치법규</span></div>
       <div class="m muted">수집 범위 외 — 군포시 조례가 아니거나 미수집</div>`;
  box.innerHTML=`<div class="lref">${inner}<span class="x" title="닫기"
       onclick="document.getElementById('localref').innerHTML=''">×</span></div>`;
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
function renderCrumb(){
  const c=document.getElementById("crumb");
  if(curQuery){c.innerHTML=`<a onclick="clearSearch()">← 검색 해제</a> · <b>검색 “${esc(curQuery)}”</b>`;return;}
  if(!curDept){c.innerHTML="";return;}
  const dq=encodeURIComponent(curDept);
  c.innerHTML=`<a onclick="selectDept('')">← 전체 담당과</a> · <b>${esc(curDept)}</b>`
    +` · <a href="/api/dept_report?dept=${dq}&format=html" target="_blank">🖨 과별 리포트</a>`
    +` · <a href="/api/dept_report?dept=${dq}&format=csv">CSV 내려받기</a>`;
}
async function refresh(){
  closeDetail();
  renderCrumb();
  if(curQuery) await renderSearch();
  else if(curDept) await renderOrdList(); else renderDeptTable();
}

async function init(){
  OV=await getJSON("/api/overview");
  GO.push(...OV.grade_order); Object.assign(GM,OV.grade_meta);
  renderBanner(); renderCards(); fillDeptSelect(); renderChanges();
  document.getElementById("deptSel").onchange=e=>selectDept(e.target.value);
  document.getElementById("actChk").onchange=()=>{
    if(curQuery)renderSearch();else if(curDept)renderOrdList();};
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
  if(t.match===null) v=`<div class="verdict na">이 판정은 조문 비교 함수 밖(파이프라인 단계) — 재실행 대조 대상 아님</div>`;
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
