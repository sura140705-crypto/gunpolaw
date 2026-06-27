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
from . import moleg
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
    rows = conn.execute(
        f"""SELECT o.mst, o.name, o.dept, o.enforce_date,
                   f.severity, f.change_type, {naked} AS cite_naked
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
        g = finding_grade(r["severity"], r["change_type"], r["cite_naked"])
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


def list_ordinances(db_path=db.DEFAULT_DB, dept=None, action_only=False):
    """담당과/정비대상 필터 조례 목록. 정비 항목 많은 순 → 이름 순."""
    ords = _scan(db_path)
    if dept:
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
    conn.close()

    by_art = {}
    for c in cits:
        by_art.setdefault(c["article_no"] or "", []).append(dict(c))

    articles = []
    if o and o["body_xml"]:
        parsed = moleg.parse_ordinance_body(o["body_xml"])
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
    return {"meta": meta, "articles": articles,
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
            if path == "/api/overview":
                return self._json(overview(self.db_path))
            if path == "/api/ordinances":
                q = parse_qs(u.query)
                dept = (q.get("dept") or [None])[0] or None
                action = (q.get("action") or ["0"])[0] in ("1", "true", "yes")
                return self._json(list_ordinances(self.db_path, dept=dept,
                                                  action_only=action))
            if path.startswith("/api/ordinance/"):
                mst = path.rsplit("/", 1)[-1]
                return self._json(ordinance_detail(self.db_path, mst))
            if path == "/api/dept_report":
                q = parse_qs(u.query)
                dept = (q.get("dept") or [""])[0]
                fmt = (q.get("format") or ["html"])[0]
                return self._dept_report(dept, fmt)
            return self._json({"error": "not found", "path": path}, status=404)
        except Exception as e:  # 서빙은 죽지 않게 — 오류도 JSON으로
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
                border-bottom:1px dotted #94a3b8;}
mark.cite-focus{outline:2px solid #f59e0b;outline-offset:1px;}
.artsec{scroll-margin-top:8px;}
.flash{animation:flash 1.4s ease;}
@keyframes flash{0%{background:#fde68a;}70%{background:#fef3c7;}100%{background:transparent;}}
</style></head>
<body><div class="wrap">
  <h1>자치법규 정비 — 총괄 대시보드</h1>
  <div class="banner" id="banner">불러오는 중…</div>
  <div class="cards" id="cards"></div>
  <div class="controls">
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
let OV=null, curDept="", curMst="", cssInjected=false;

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

function flash(el){if(!el)return;el.classList.remove("flash");void el.offsetWidth;
  el.classList.add("flash");el.scrollIntoView({behavior:"smooth",block:"center"});}

async function selectOrd(mst){
  curMst=mst;
  const d=await getJSON(`/api/ordinance/${encodeURIComponent(mst)}`);
  const r=d.recommend||{}, m=d.meta||{}, arts=d.articles||[];
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
     <h2 style="margin-top:8px">${esc(m.name||"조례")}</h2><div class="m">${meta}</div></div>
     <div class="dsplit">
       <div class="dbody"><div class="dcolhd">📄 조례 본문 — <span style="color:#1e3a8a">상위법령 「」</span> / <span style="color:#5b21b6">맨몸</span> / <span style="color:#64748b">타 조례(검토 대상 외)</span></div>${left}</div>
       <div class="drec"><div class="dcolhd">🔧 검토 사항 — 조례 조문별 · 좌측 인용 클릭 시 펼침</div>${right}</div>
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
function closeDetail(){curMst="";
  document.getElementById("detailPanel").style.display="none";
  document.getElementById("layout").classList.remove("detail-open");}

function selectDept(dept){
  curDept=dept;
  document.getElementById("deptSel").value=dept;
  refresh();
}
function renderCrumb(){
  const c=document.getElementById("crumb");
  if(!curDept){c.innerHTML="";return;}
  const dq=encodeURIComponent(curDept);
  c.innerHTML=`<a onclick="selectDept('')">← 전체 담당과</a> · <b>${esc(curDept)}</b>`
    +` · <a href="/api/dept_report?dept=${dq}&format=html" target="_blank">🖨 과별 리포트</a>`
    +` · <a href="/api/dept_report?dept=${dq}&format=csv">CSV 내려받기</a>`;
}
async function refresh(){
  closeDetail();
  renderCrumb();
  if(curDept) await renderOrdList(); else renderDeptTable();
}

async function init(){
  OV=await getJSON("/api/overview");
  GO.push(...OV.grade_order); Object.assign(GM,OV.grade_meta);
  renderBanner(); renderCards(); fillDeptSelect();
  document.getElementById("deptSel").onchange=e=>selectDept(e.target.value);
  document.getElementById("actChk").onchange=()=>{if(curDept)renderOrdList();};
  refresh();
}
init();
</script>
</body></html>
"""
