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
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import db
from .report import GRADE_KEYS, GRADE_META, finding_grade, recommend_fragment


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


def ordinance_detail(db_path=db.DEFAULT_DB, mst=None):
    """조례 1건 상세: 메타(담당과·연락처) + 권고 조각(report.recommend_fragment)."""
    conn = db.connect(db_path)
    o = conn.execute(
        """SELECT mst, name, dept, phone, knd, promulg_date, enforce_date
           FROM ordinances WHERE mst = ?""", (str(mst),)).fetchone()
    conn.close()
    return {"meta": (dict(o) if o else {}),
            "recommend": recommend_fragment(mst, db_path)}


# ---------- HTTP(읽기전용) ----------
class _Handler(BaseHTTPRequestHandler):
    db_path = db.DEFAULT_DB
    server_version = "gunpolaw-serve/1.0"

    def _send(self, body, status=200, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
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
            return self._json({"error": "not found", "path": path}, status=404)
        except Exception as e:  # 서빙은 죽지 않게 — 오류도 JSON으로
            return self._json({"error": type(e).__name__, "detail": str(e)},
                              status=500)

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
.layout.split{grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);}
.panel{background:#fff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;}
table{width:100%;border-collapse:collapse;font-size:13.5px;}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid #f1f3f5;}
th{background:#f9fafb;color:#6b7280;font-weight:600;font-size:12px;position:sticky;top:0;}
tbody tr{cursor:pointer;}
tbody tr:hover{background:#f8fafc;}
td.num{text-align:right;font-variant-numeric:tabular-nums;}
.dist{white-space:nowrap;}
.pill{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;
      margin-right:4px;color:#fff;}
.p-mech{background:var(--mech);}.p-rev{background:var(--rev);}.p-chk{background:var(--chk);}
.p-fmt{background:var(--fmt);}.p-cur{background:#9ca3af;}
.detail{padding:0;max-height:78vh;overflow:auto;}
.detail .dhd{padding:14px 16px;border-bottom:1px solid #eef0f3;}
.detail .dhd h2{font-size:16px;margin:0 0 3px;}
.detail .dhd .m{font-size:12px;color:#6b7280;}
.detail .body{padding:8px 14px 18px;}
.empty{padding:40px 16px;color:#9ca3af;text-align:center;font-size:14px;}
.btn{font-size:12px;padding:4px 10px;border:1px solid #d1d5db;border-radius:7px;
     background:#fff;cursor:pointer;color:#374151;}
.muted{color:#9ca3af;}
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
  let rows=OV.depts.map(d=>`<tr data-dept="${esc(d.dept)}">
     <td>${esc(d.dept)}</td>
     <td class="num">${d.action}</td><td class="num">${d.total}</td>
     <td class="dist">${dist(d.grades)}</td></tr>`).join("");
  document.getElementById("listPanel").innerHTML=
    `<table><thead><tr><th>담당과</th><th class="num">정비대상</th>
     <th class="num">전체</th><th>등급 분포</th></tr></thead><tbody>${rows}</tbody></table>`;
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

async function selectOrd(mst){
  curMst=mst;
  const d=await getJSON(`/api/ordinance/${encodeURIComponent(mst)}`);
  const r=d.recommend||{}, m=d.meta||{};
  if(r.css && !cssInjected){const s=document.createElement("style");
    s.textContent=r.css;document.head.appendChild(s);cssInjected=true;}
  const dp=document.getElementById("detailPanel");
  const meta=`시행 ${fdate(m.enforce_date)} · 담당 ${esc(m.dept||"—")}`
    +(m.phone?` · ☎ ${esc(m.phone)}`:"");
  const body=r.found?r.html:`<div class="empty">정비 항목이 없습니다(현행 유지).</div>`;
  dp.innerHTML=`<div class="dhd"><button class="btn" onclick="closeDetail()">✕ 닫기</button>
     <h2 style="margin-top:8px">${esc(m.name||"조례")}</h2><div class="m">${meta}</div></div>
     <div class="body">${body}</div>`;
  dp.style.display="block";
  document.getElementById("layout").classList.add("split");
}
function closeDetail(){curMst="";
  document.getElementById("detailPanel").style.display="none";
  document.getElementById("layout").classList.remove("split");}

function selectDept(dept){
  curDept=dept;
  document.getElementById("deptSel").value=dept;
  refresh();
}
function renderCrumb(){
  const c=document.getElementById("crumb");
  c.innerHTML=curDept?`<a onclick="selectDept('')">← 전체 담당과</a> · <b>${esc(curDept)}</b>`:"";
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
