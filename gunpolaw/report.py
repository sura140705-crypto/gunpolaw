# -*- coding: utf-8 -*-
"""3단계: 개정 권고서 생성.

findings(변경 탐지 결과)를 담당자가 바로 결재·정비에 쓰는 "행동 지시서"로 변환.
시스템 역할 = 등급 분류 + 조항별 행동 문안 초안 + 근거. 최종 판단은 담당자 몫.

산출물: 표준 라이브러리만으로 만든 단일 HTML(인쇄→PDF 가능, 의존성 0).

등급(grade) — finding의 (severity, change_type)에서 도출:
  mechanical 🔧 기계적 개정 : 인용 조문번호만 정정(번호이동 등)
  review     ⚠️ 실질 검토   : 내용변경·삭제 — 조문 정비 검토
  check      📋 확인        : 소재 미확인·법령미해결 — 사람 확인
  current    ✅ 현행 유지   : 변경 없음(권고서 본문엔 집계만)
"""
import html
from datetime import datetime

from . import db


# ---------- 등급 도출 ----------
GRADE_META = {
    "mechanical": {"order": 0, "emoji": "🔧", "label": "기계적 개정", "cls": "g-mech"},
    "review":     {"order": 1, "emoji": "⚠️", "label": "실질 검토",   "cls": "g-rev"},
    "check":      {"order": 2, "emoji": "📋", "label": "확인",         "cls": "g-chk"},
    "current":    {"order": 3, "emoji": "✅", "label": "현행 유지",     "cls": "g-cur"},
}
GRADE_KEYS = ("mechanical", "review", "check", "current")


def grade_of(severity, change_type=""):
    """finding의 등급 키. 1단계(mechanical)·2단계(번호이동) 모두 수용."""
    if severity == "current":
        return "current"
    if severity == "mechanical" or change_type == "번호이동":
        return "mechanical"
    if severity == "review":
        return "review"
    return "check"


def _get(f, key):
    """dict / sqlite3.Row 양쪽에서 안전하게 값 읽기 (없으면 빈 문자열)."""
    try:
        v = f[key]
    except (KeyError, IndexError):
        return ""
    return v if v is not None else ""


# ---------- 행동 지시 문안 ----------
def action_text(f):
    """finding 1건 → 담당자 행동 지시 한 문장 (등급·변경유형별 템플릿)."""
    law = _get(f, "law_name") or "(법령명 미상)"
    clause = _get(f, "clause_label")
    ct = _get(f, "change_type")
    g = grade_of(_get(f, "severity"), ct)

    if g == "mechanical":
        # detail 안에 "제X조 → 제Y조" 이동처 안내가 들어 있음
        return f"「{law}」 {clause} 인용을 현행 조문번호로 정정 — {_get(f, 'detail')}"
    if g == "review":
        if ct == "내용변경":
            return (f"「{law}」 {clause} 개정 내용을 반영하여 해당 조문을 검토·정비")
        if ct == "삭제":
            return (f"「{law}」 {clause} 삭제·통합 여부를 확인하고 인용을 정비")
        return f"「{law}」 {clause} 변경 사항을 검토하여 조문 정비"
    # check
    if ct == "법령미해결":
        return f"「{law}」 제명변경·폐지 여부를 확인하고 인용 법령명을 정정"
    if ct == "당시부재":
        return f"「{law}」 {clause} 인용 시점을 확인 (제정 당시 부재)"
    return f"「{law}」 {clause} 소재를 확인 (삭제·이동·오기 여부)"


# ---------- DB → 보고 모델 ----------
def build_model(db_path=db.DEFAULT_DB):
    """findings + ordinances → 권고서 렌더 모델.

    {summary:{...}, ordinances:[{name, enforce_date, grades:{}, items:[...]}, ...]}
    조례는 정비 우선순위(기계적→실질→확인 순 가중)로 정렬.
    """
    conn = db.connect(db_path)
    rows = conn.execute(
        """SELECT f.mst, f.law_name, f.clause_label, f.severity, f.change_type,
                  f.detail, f.evidence, f.ord_enforce, f.clause_enforce,
                  o.name AS ord_name, o.enforce_date AS ord_enforce_date
           FROM findings f LEFT JOIN ordinances o ON o.mst = f.mst
           ORDER BY f.law_name, f.clause_label"""
    ).fetchall()
    conn.close()

    by_mst = {}
    summary = {k: 0 for k in GRADE_KEYS}
    for r in rows:
        g = grade_of(r["severity"], r["change_type"])
        summary[g] += 1
        o = by_mst.setdefault(r["mst"], {
            "mst": r["mst"],
            "name": r["ord_name"] or "(조례명 미상)",
            "enforce_date": r["ord_enforce_date"] or r["ord_enforce"] or "",
            "grades": {k: 0 for k in GRADE_KEYS},
            "items": [],
        })
        o["grades"][g] += 1
        if g != "current":
            o["items"].append({
                "grade": g,
                "law_name": r["law_name"] or "",
                "clause_label": r["clause_label"] or "",
                "change_type": r["change_type"] or "",
                "action": action_text(r),
                "evidence": r["evidence"] or "",
                "clause_enforce": r["clause_enforce"] or "",
            })

    ordinances = [o for o in by_mst.values() if o["items"]]
    for o in ordinances:
        o["items"].sort(key=lambda it: GRADE_META[it["grade"]]["order"])

    def prio(o):
        gr = o["grades"]
        # 기계적이 많을수록, 그다음 실질·확인 순으로 위에
        return (-(gr["mechanical"] * 100 + gr["review"] * 10 + gr["check"]),
                o["name"])
    ordinances.sort(key=prio)

    summary["ordinances_total"] = len({r["mst"] for r in rows})
    summary["ordinances_action"] = len(ordinances)
    return {"summary": summary, "ordinances": ordinances}


# ---------- HTML 렌더 ----------
_CSS = """
:root { --mech:#2563eb; --rev:#d97706; --chk:#6b7280; --cur:#16a34a; }
* { box-sizing: border-box; }
body { font-family: "Malgun Gothic","맑은 고딕",system-ui,sans-serif;
       color:#1f2937; margin:0; background:#f3f4f6; }
.page { max-width:980px; margin:0 auto; padding:32px 28px 64px; background:#fff; }
h1 { font-size:24px; margin:0 0 4px; }
.sub { color:#6b7280; font-size:13px; margin-bottom:24px; }
.cards { display:flex; gap:12px; flex-wrap:wrap; margin:0 0 28px; }
.card { flex:1 1 160px; border:1px solid #e5e7eb; border-radius:10px;
        padding:14px 16px; }
.card .n { font-size:28px; font-weight:700; line-height:1; }
.card .t { font-size:13px; color:#6b7280; margin-top:6px; }
.g-mech .n { color:var(--mech);} .g-rev .n { color:var(--rev);}
.g-chk .n { color:var(--chk);} .g-cur .n { color:var(--cur);}
.ord { border:1px solid #e5e7eb; border-radius:10px; padding:16px 18px;
       margin:0 0 16px; page-break-inside:avoid; }
.ord h2 { font-size:17px; margin:0 0 2px; }
.ord .meta { color:#6b7280; font-size:12px; margin-bottom:10px; }
.badges { margin-bottom:10px; }
.badge { display:inline-block; font-size:12px; padding:2px 9px; border-radius:999px;
         margin-right:6px; color:#fff; }
.b-mech { background:var(--mech);} .b-rev { background:var(--rev);}
.b-chk { background:var(--chk);}
.item { border-top:1px solid #f3f4f6; padding:10px 0; }
.item:first-of-type { border-top:none; }
.item .tag { font-size:11px; font-weight:700; padding:1px 7px; border-radius:6px;
             margin-right:8px; white-space:nowrap; }
.t-mech { background:#dbeafe; color:#1e40af;} .t-rev { background:#fef3c7; color:#92400e;}
.t-chk { background:#f3f4f6; color:#374151;}
.item .act { font-size:14px; }
.item .ev { font-size:12px; color:#6b7280; white-space:pre-wrap;
            background:#f9fafb; border-radius:6px; padding:7px 9px; margin-top:6px; }
footer { color:#9ca3af; font-size:12px; margin-top:32px; text-align:center; }
@media print {
  body { background:#fff; } .page { max-width:none; padding:0; }
  .ord, .card { border-color:#d1d5db; }
}
"""


def _esc(s):
    return html.escape(str(s or ""))


def _card(summary, key, title):
    m = GRADE_META[key]
    return (f'<div class="card {m["cls"]}"><div class="n">{summary[key]}</div>'
            f'<div class="t">{m["emoji"]} {title}</div></div>')


def _ord_block(o):
    gr = o["grades"]
    badges = []
    for key, bcls in (("mechanical", "b-mech"), ("review", "b-rev"), ("check", "b-chk")):
        if gr[key]:
            badges.append(f'<span class="badge {bcls}">'
                          f'{GRADE_META[key]["emoji"]} {GRADE_META[key]["label"]} {gr[key]}</span>')
    items = []
    for it in o["items"]:
        tag_cls = {"mechanical": "t-mech", "review": "t-rev", "check": "t-chk"}[it["grade"]]
        tag = (it["change_type"] or GRADE_META[it["grade"]]["label"])
        ev = f'<div class="ev">{_esc(it["evidence"])}</div>' if it["evidence"] else ""
        items.append(
            f'<div class="item"><span class="tag {tag_cls}">{_esc(tag)}</span>'
            f'<span class="act">{_esc(it["action"])}</span>{ev}</div>')
    return (
        f'<div class="ord"><h2>{_esc(o["name"])}</h2>'
        f'<div class="meta">시행 {_esc(o["enforce_date"])} · 정비 항목 {len(o["items"])}건</div>'
        f'<div class="badges">{"".join(badges)}</div>'
        f'{"".join(items)}</div>')


def render_html(model, generated_at="", title="군포시 자치법규 정비 권고서"):
    s = model["summary"]
    cards = (
        _card(s, "mechanical", "기계적 개정") + _card(s, "review", "실질 검토") +
        _card(s, "check", "확인 필요") + _card(s, "current", "현행 유지"))
    blocks = "".join(_ord_block(o) for o in model["ordinances"])
    sub = (f'전체 {s["ordinances_total"]}개 조례 중 '
           f'정비 대상 {s["ordinances_action"]}개 · 생성 {_esc(generated_at)}')
    return (
        "<!DOCTYPE html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body>"
        f'<div class="page"><h1>{_esc(title)}</h1><div class="sub">{sub}</div>'
        f'<div class="cards">{cards}</div>{blocks}'
        '<footer>본 권고서는 인용 조항의 시점·내용 비교로 자동 생성된 초안이며, '
        '최종 개정 판단은 담당 부서의 검토를 따릅니다.</footer>'
        "</div></body></html>")


def write_report(db_path=db.DEFAULT_DB, out_path="개정권고서.html", generated_at=None):
    """권고서 HTML 파일 생성 → out_path 반환."""
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    model = build_model(db_path)
    htmltext = render_html(model, generated_at=generated_at)
    with open(out_path, "w", encoding="utf-8") as fp:
        fp.write(htmltext)
    return out_path, model["summary"]
