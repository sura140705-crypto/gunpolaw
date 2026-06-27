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
import csv
import difflib
import html
import io
import re
from datetime import datetime

from . import db
from . import moleg


# ---------- 등급 도출 ----------
GRADE_META = {
    "mechanical": {"order": 0, "emoji": "🔧", "label": "기계적 개정", "cls": "g-mech"},
    "review":     {"order": 1, "emoji": "⚠️", "label": "실질 검토",   "cls": "g-rev"},
    "check":      {"order": 2, "emoji": "📋", "label": "확인",         "cls": "g-chk"},
    "format":     {"order": 3, "emoji": "📐", "label": "서식 정비",     "cls": "g-fmt"},
    "current":    {"order": 4, "emoji": "✅", "label": "현행 유지",     "cls": "g-cur"},
}
# format = 내용은 현행이나 「」 없이 인용된 서식 결함(정비 권장, 최저 우선순위)
GRADE_KEYS = ("mechanical", "review", "check", "format", "current")


# ---------- 파싱·날짜·diff 헬퍼 ----------
def _jo_num(label):
    """'제9조' / '제9조, 제12조' → 9 (정렬용, 첫 조문번호)."""
    m = re.search(r"제(\d+)조", label or "")
    return int(m.group(1)) if m else 9999


def _clause_num(label):
    """상위법 조 라벨 → (조, 가지) 정렬키."""
    jo = re.search(r"제(\d+)조", label or "")
    ga = re.search(r"의(\d+)", label or "")
    return (int(jo.group(1)) if jo else 9999, int(ga.group(1)) if ga else 0)


def _fmtdate(s):
    """YYYYMMDD → YYYY-MM-DD. 날짜 아닌 라벨('원제정')은 그대로, 빈값은 '—'."""
    raw = str(s or "").strip()
    digits = re.sub(r"[^\d]", "", raw)[:8]
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return raw if raw else "—"


_EV_RE = re.compile(r"\[당시\]\s*(.*?)\n\[현행\]\s*(.*)", re.S)


def _split_evidence(ev):
    """'[당시] ...\\n[현행] ...' → (당시, 현행). 형식 아니면 (None, None)."""
    m = _EV_RE.match(ev or "")
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)


def _diff_marks(old, new):
    """당시/현행 내용을 토큰 diff 하여 바뀐 부분만 <mark> 로 감싼 (당시HTML, 현행HTML)."""
    a, b = _TOKEN_RE.findall(old or ""), _TOKEN_RE.findall(new or "")
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    o_html, n_html = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        at, bt = "".join(a[i1:i2]), "".join(b[j1:j2])
        if op == "equal":
            o_html.append(_esc(at))
            n_html.append(_esc(bt))
            continue
        o_html.append(f'<mark class="d">{_esc(at)}</mark>' if at.strip() else _esc(at))
        n_html.append(f'<mark class="i">{_esc(bt)}</mark>' if bt.strip() else _esc(bt))
    return "".join(o_html), "".join(n_html)


def grade_of(severity, change_type=""):
    """finding의 등급 키. 1단계(mechanical)·2단계(번호이동) 모두 수용."""
    if severity == "current":
        return "current"
    if severity == "mechanical" or change_type == "번호이동":
        return "mechanical"
    if severity == "review":
        return "review"
    return "check"


def finding_grade(severity, change_type="", cite_naked=0):
    """finding 1건의 **최종 등급 키**(권고서·대시보드 공용 단일 출처).

    grade_of 위에 '서식 정비' 승격 규칙을 더한다: 내용은 현행이나 「」 없이
    맨몸 인용된 결함은 current가 아니라 format(서식 정비)으로 끌어올린다.
    """
    g = grade_of(severity, change_type)
    if g == "current" and (cite_naked or 0):
        return "format"
    return g


def _get(f, key):
    """dict / sqlite3.Row 양쪽에서 안전하게 값 읽기 (없으면 빈 문자열)."""
    try:
        v = f[key]
    except (KeyError, IndexError):
        return ""
    return v if v is not None else ""


# ---------- 행동 지시 문안 ----------
def action_text(f):
    """finding 1건 → 담당자 행동 지시 한 문장 (등급·변경유형별 템플릿).

    답의 주체를 조례 쪽으로: "「상위법」 제X조가 바뀌었으니 → 이 조례 제Y조를 정비".
    ord_clause(인용한 조례 조문)가 있으면 정비 위치를 직접 가리킨다.
    """
    law = _get(f, "law_name") or "(법령명 미상)"
    clause = _get(f, "clause_label")
    ct = _get(f, "change_type")
    here = _get(f, "ord_clause")
    g = grade_of(_get(f, "severity"), ct)
    loc = f"이 조례 {here}" if here else "이 조례의 해당 조문"
    # 제명변경: 인용한 법령명이 현행과 다름 → 인용 법령명 정정(detail에 신·구 명칭 포함)
    if ct == "제명변경":   # detail에 신·구 명칭이 담겨 있음(인용 법령명 정정 안내)
        return _get(f, "detail") or f"「{law}」 법령명 불일치 — 인용 법령명 정정"
    # 내용은 현행이지만 「」 없이 인용된 서식 결함 → 서식 정정만 안내(내용 변경 없음)
    if _get(f, "cite_naked") and _get(f, "severity") == "current":
        cl = f" {clause}" if clause else ""
        return (f"「{law}」{cl} 인용을 꺽쇠(「」) 서식으로 정정 ({loc}) "
                f"— 내용 변경은 없음, 서식만 정비")
    # 내용 변경이 동반된 맨몸 인용은 내용 정비와 함께 인용 서식도 바로잡도록 덧붙임
    naked_note = (f' (덧붙여 「{law}」처럼 꺽쇠 서식으로 정정)'
                  if _get(f, "cite_naked") else "")

    def _base():
        if g == "mechanical":
            # detail 안에 "제X조 → 제Y조" 이동처 안내가 들어 있음
            return f"「{law}」 {clause} 인용을 현행 조문번호로 정정 ({loc}) — {_get(f, 'detail')}"
        if g == "review":
            if ct == "내용변경":
                return f"「{law}」 {clause} 개정 내용을 반영하여 {loc}을(를) 검토·정비"
            if ct == "삭제":
                return f"「{law}」 {clause} 삭제·통합 여부를 확인하고 {loc}의 인용을 정비"
            if ct == "번호이동":
                return f"「{law}」 {clause} 조문번호 이동 — {loc}의 인용 조문번호 정정"
            return f"「{law}」 {clause} 변경 사항을 검토하여 {loc} 정비"
        # check
        if ct == "법령미해결":
            return f"「{law}」 제명변경·폐지 여부를 확인하고 {loc}의 인용 법령명을 정정"
        if ct == "당시부재":
            return f"「{law}」 {clause} 인용 시점을 확인 — {loc} (제정 당시 부재)"
        return f"「{law}」 {clause} 소재를 확인(삭제·이동·오기 여부) — {loc}"

    return _base() + naked_note


# ---------- DB → 보고 모델 ----------
def build_model(db_path=db.DEFAULT_DB, mst=None, dept=None, include_current=False):
    """findings + ordinances → 권고서 렌더 모델.

    {summary:{...}, ordinances:[{name, enforce_date, grades:{}, items:[...]}, ...]}
    조례는 정비 우선순위(기계적→실질→확인 순 가중)로 정렬.
    mst 지정 시 해당 조례 1건만(UI 권고 뷰용). dept 지정 시 그 담당과 조례만(과별 리포트).
    include_current=True면 현행(변경 없음) 인용도 항목으로 포함 — '검토했으나 변경 없음'을
    명시(대시보드 통합 뷰). 독립 권고서는 False(정비 대상만).
    """
    conn = db.connect(db_path)
    # 구 DB 호환: 없는 컬럼은 빈 값으로 대체
    fcols = [r[1] for r in conn.execute("PRAGMA table_info(findings)")]

    def sel(col, default="''"):
        return f"f.{col}" if col in fcols else f"{default} AS {col}"

    where, args = "", []
    if mst is not None:
        where = "WHERE f.mst = ?"
        args = [str(mst)]
    elif dept is not None:
        where = "WHERE o.dept = ?"
        args = [dept]
    rows = conn.execute(
        f"""SELECT f.mst, f.law_name, f.clause_label, {sel('clause_detail')},
                  f.severity, f.change_type,
                  {sel('ord_clause')}, {sel('ord_seq', '999999')}, f.detail, f.evidence,
                  f.ord_enforce, {sel('old_enforce')}, f.clause_enforce,
                  {sel('cite_naked', '0')},
                  o.name AS ord_name, o.enforce_date AS ord_enforce_date
           FROM findings f LEFT JOIN ordinances o ON o.mst = f.mst
           {where}
           ORDER BY f.law_name, f.clause_label""", args
    ).fetchall()
    # 조례 본문(조문별 텍스트) — 권고에 '무엇을 고칠지' 조례 원문을 함께 보여주기 위함
    msts = {r["mst"] for r in rows}
    body_articles = {}
    if msts:
        qm = ",".join("?" * len(msts))
        for br in conn.execute(
                f"SELECT mst, body_xml FROM ordinances WHERE mst IN ({qm})", list(msts)):
            if not br["body_xml"]:
                continue
            parsed = moleg.parse_ordinance_body(br["body_xml"])
            if "error" not in parsed:
                body_articles[br["mst"]] = {
                    a["no"]: a.get("body", "") for a in parsed["articles"] if a.get("no")}
    conn.close()

    by_mst = {}
    summary = {k: 0 for k in GRADE_KEYS}
    for r in rows:
        # 내용 현행 + 맨몸 인용은 finding_grade가 '서식 정비'로 끌어올린다(단일 출처)
        g = finding_grade(r["severity"], r["change_type"], r["cite_naked"])
        summary[g] += 1
        o = by_mst.setdefault(r["mst"], {
            "mst": r["mst"],
            "name": r["ord_name"] or "(조례명 미상)",
            "enforce_date": r["ord_enforce_date"] or r["ord_enforce"] or "",
            "grades": {k: 0 for k in GRADE_KEYS},
            "items": [],
            "articles_text": body_articles.get(r["mst"], {}),
        })
        o["grades"][g] += 1
        if g != "current":
            o["items"].append({
                "grade": g,
                "law_name": r["law_name"] or "",
                "clause_label": r["clause_label"] or "",
                "clause_detail": r["clause_detail"] or "",
                "change_type": r["change_type"] or "",
                "ord_clause": r["ord_clause"] or "",
                "ord_seq": r["ord_seq"] if r["ord_seq"] is not None else 999999,
                "action": action_text(r),
                "evidence": r["evidence"] or "",
                "ord_enforce": r["ord_enforce"] or o["enforce_date"],
                "old_enforce": r["old_enforce"] or "",
                "clause_enforce": r["clause_enforce"] or "",
                "cite_naked": r["cite_naked"] or 0,
            })
        elif include_current:
            # 변경 없음(현행) 인용도 '검토 완료'로 표기 — 누락이 아니라 검토 결과임을 명시
            o["items"].append({
                "grade": "current",
                "law_name": r["law_name"] or "",
                "clause_label": r["clause_label"] or "",
                "clause_detail": r["clause_detail"] or "",
                "change_type": "동일",
                "ord_clause": r["ord_clause"] or "",
                "ord_seq": r["ord_seq"] if r["ord_seq"] is not None else 999999,
                "action": "검토 완료 — 인용 조항이 조례 시행 이후 개정되지 않아 변경 없음.",
                "evidence": "",
                "ord_enforce": r["ord_enforce"] or o["enforce_date"],
                "old_enforce": r["old_enforce"] or "",
                "clause_enforce": r["clause_enforce"] or "",
                "cite_naked": r["cite_naked"] or 0,
            })

    ordinances = [o for o in by_mst.values() if o["items"]]
    for o in ordinances:
        # 조례 조문번호 순(제1조→제2조…) → 그 안에서 본문 등장 순서 → 상위법 조 순
        o["items"].sort(key=lambda it: (_jo_num(it.get("ord_clause")),
                                        it.get("ord_seq", 999999),
                                        _clause_num(it.get("clause_label"))))

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
:root { --mech:#2563eb; --rev:#d97706; --chk:#6b7280; --fmt:#7c3aed; --cur:#16a34a; }
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
.g-chk .n { color:var(--chk);} .g-fmt .n { color:var(--fmt);} .g-cur .n { color:var(--cur);}
.ord { border:1px solid #e5e7eb; border-radius:10px; padding:16px 18px;
       margin:0 0 16px; page-break-inside:avoid; }
.ord h2 { font-size:17px; margin:0 0 2px; }
.ord .meta { color:#6b7280; font-size:12px; margin-bottom:10px; }
.badges { margin-bottom:10px; }
.badge { display:inline-block; font-size:12px; padding:2px 9px; border-radius:999px;
         margin-right:6px; color:#fff; }
.b-mech { background:var(--mech);} .b-rev { background:var(--rev);}
.b-chk { background:var(--chk);} .b-fmt { background:var(--fmt);}
.artsec { margin:0 0 6px; }
.arthd { font-size:13px; font-weight:700; color:#111827; background:#eef2ff;
         border-left:3px solid var(--mech); padding:4px 10px; border-radius:4px;
         margin:12px 0 4px; }
.item { border-top:1px solid #f3f4f6; padding:9px 0 9px 10px; }
.iline { margin-bottom:3px; }
.item .tag { font-size:11px; font-weight:700; padding:1px 7px; border-radius:6px;
             margin-right:8px; white-space:nowrap; }
.t-mech { background:#dbeafe; color:#1e40af;} .t-rev { background:#fef3c7; color:#92400e;}
.t-chk { background:#f3f4f6; color:#374151;}
.t-naked { background:#ede9fe; color:#5b21b6;}
.t-cur { background:#dcfce7; color:#166534;}
details.citem.cur > summary { opacity:.72; }
details.citem.cur .law { font-weight:500; }
/* 접이식 인용 항목(대시보드 우측) — 기본 접힘, 좌측 인용 클릭 시 펼침 */
details.citem { border-top:1px solid #f3f4f6; padding:0; }
details.citem > summary { list-style:none; cursor:pointer; padding:9px 10px 9px 8px;
    display:flex; align-items:center; gap:5px; flex-wrap:wrap; }
details.citem > summary::-webkit-details-marker { display:none; }
details.citem > summary::before { content:"▸"; color:#9ca3af; font-size:11px; margin-right:2px; }
details.citem[open] > summary::before { content:"▾"; color:#2563eb; }
details.citem[open] { background:#fbfcfe; }
details.citem > .act { padding:2px 10px 0; } details.citem > .basis { padding:0 10px; }
details.citem > .diff { margin:6px 10px 10px; }
details.citem.focus { box-shadow:inset 3px 0 0 #2563eb; }
.item .law { font-size:13px; font-weight:600; color:#374151; }
.item .act { font-size:14px; margin:2px 0; }
.basis { font-size:11.5px; color:#6b7280; margin:4px 0 6px; }
.basis b { color:#374151; font-weight:600; }
.ordtext { font-size:12.5px; color:#1f2937; background:#f8fafc; border:1px solid #e5e7eb;
           border-left:3px solid #94a3b8; border-radius:6px; padding:7px 10px; margin:4px 0 8px;
           white-space:pre-wrap; line-height:1.6; }
.ordtext b { color:#0f172a; margin-right:4px; }
.item .ev { font-size:12px; color:#6b7280; white-space:pre-wrap;
            background:#f9fafb; border-radius:6px; padding:7px 9px; margin-top:6px; }
.diff { border:1px solid #eef0f3; border-radius:6px; overflow:hidden; margin-top:6px; }
.drow { display:flex; gap:8px; font-size:12px; padding:6px 9px; }
.drow + .drow { border-top:1px solid #f1f3f5; }
.dlabel { flex:0 0 34px; font-weight:700; font-size:11px; padding-top:1px; }
.dlabel.was { color:#b91c1c; } .dlabel.now { color:#15803d; }
.dtext { flex:1; white-space:pre-wrap; word-break:break-all; color:#374151; line-height:1.5; }
mark.d { background:#fee2e2; color:#991b1b; text-decoration:line-through;
         border-radius:2px; padding:0 1px; }
mark.i { background:#dcfce7; color:#166534; border-radius:2px; padding:0 1px; }
footer { color:#9ca3af; font-size:12px; margin-top:32px; text-align:center; }
@media print {
  body { background:#fff; } .page { max-width:none; padding:0; }
  .ord, .card { border-color:#d1d5db; }
  mark.d, mark.i { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
}
"""


def _esc(s):
    return html.escape(str(s or ""))


def _card(summary, key, title):
    m = GRADE_META[key]
    return (f'<div class="card {m["cls"]}"><div class="n">{summary.get(key, 0)}</div>'
            f'<div class="t">{m["emoji"]} {title}</div></div>')


def _primary_article(it):
    """이 항목의 대표 조례 조문(첫 조문). 위치 미상이면 표시용 라벨."""
    oc = (it.get("ord_clause") or "").split(",")[0].strip()
    return oc or "(위치 미상)"


def _basis_line(it):
    """판단 기준 날짜 한 줄: 조례 시행 → 당시 법령본 → 현행 조문."""
    return (
        '<div class="basis">기준일 — '
        f'조례 시행 <b>{_fmtdate(it.get("ord_enforce"))}</b> · '
        f'당시 법령본 <b>{_fmtdate(it.get("old_enforce"))}</b> → '
        f'현행 조문 <b>{_fmtdate(it.get("clause_enforce"))}</b></div>')


def _drow(o_html, n_html):
    return (f'<div class="drow"><span class="dlabel was">당시</span>'
            f'<span class="dtext">{o_html}</span></div>'
            f'<div class="drow"><span class="dlabel now">현행</span>'
            f'<span class="dtext">{n_html}</span></div>')


def _evidence_block(it):
    """내용변경이면 당시/현행 diff(전체 표시). 인용 단위(호/목)로 좁혀 오므로 생략 없이
    바뀐 토큰만 하이라이트해 그대로 보여준다."""
    old, new = _split_evidence(it.get("evidence", ""))
    if old is None:
        ev = it.get("evidence", "")
        return f'<div class="ev">{_esc(ev)}</div>' if ev else ""
    o_html, n_html = _diff_marks(old, new)
    return f'<div class="diff">{_drow(o_html, n_html)}</div>'


def _item_block(it, collapsible=False):
    """인용 1건 권고. collapsible=True면 <details>로 접어 헤드라인(법령·등급)만 보이고,
    펼치면 행동지시·기준일·diff. 좌측 본문 인용 클릭 시 (law,clause)로 이 항목을 편다.
    """
    tag_cls = {"mechanical": "t-mech", "review": "t-rev", "check": "t-chk",
               "format": "t-naked", "current": "t-cur"}[it["grade"]]
    # 서식=서식, 현행=변경 없음, 그 외=변경유형
    tag = {"format": "서식", "current": "✅ 변경 없음"}.get(
        it["grade"], it["change_type"] or GRADE_META[it["grade"]]["label"])
    # 인용된 호·목까지 표기(제3조제5호나목) — 좁혀 판정한 단위를 그대로 보여줌
    clause_full = (it["clause_label"] or "") + (it.get("clause_detail") or "")
    law = f'「{_esc(it["law_name"])}」 {_esc(clause_full)}'.rstrip()
    # 내용변경+맨몸은 별도 「」누락 배지, 서식정비(format) 항목은 태그 자체가 서식이라 생략
    naked = ('<span class="tag t-naked">「」누락</span>'
             if it.get("cite_naked") and it["grade"] != "format" else "")
    head = (f'<span class="tag {tag_cls}">{_esc(tag)}</span>{naked}'
            f'<span class="law">{law}</span>')
    body = (f'<div class="act">{_esc(it["action"])}</div>'
            f'{_basis_line(it)}{_evidence_block(it)}')
    if collapsible:
        cls = "item citem cur" if it["grade"] == "current" else "item citem"
        oc = (it.get("ord_clause") or "").split(",")[0].strip()
        return (f'<details class="{cls}" data-oc="{_esc(oc)}"'
                f' data-law="{_esc(it["law_name"])}"'
                f' data-clause="{_esc(it["clause_label"])}">'
                f'<summary class="iline">{head}</summary>{body}</details>')
    return (f'<div class="item"><div class="iline">{head}</div>{body}</div>')


def _ord_block(o, collapsible=False):
    gr = o["grades"]
    badges = []
    for key, bcls in (("mechanical", "b-mech"), ("review", "b-rev"),
                      ("check", "b-chk"), ("format", "b-fmt")):
        if gr.get(key):
            badges.append(f'<span class="badge {bcls}">'
                          f'{GRADE_META[key]["emoji"]} {GRADE_META[key]["label"]} {gr[key]}</span>')

    # 조례 조문 순서대로 묶어서, 각 조문 아래 언급된 법령을 등장 순서로 나열
    sections, cur_art, buf = [], None, []
    for it in o["items"]:
        art = _primary_article(it)
        if art != cur_art:
            if buf:
                sections.append((cur_art, buf))
            cur_art, buf = art, []
        buf.append(it)
    if buf:
        sections.append((cur_art, buf))

    atext = o.get("articles_text") or {}
    secs_html = []
    for art, group in sections:
        body = "".join(_item_block(it, collapsible) for it in group)
        # 조례 원문은 평면(독립 권고서)에서만 — 대시보드(collapsible)는 좌측 본문이 대신함
        ord_src = ""
        if not collapsible:
            labels = []
            for it in group:
                for a in (it.get("ord_clause") or "").split(","):
                    a = a.strip()
                    if a and a not in labels:
                        labels.append(a)
            ord_src = "".join(
                f'<div class="ordtext"><b>{_esc(a)}</b> {_esc(atext.get(a, ""))}</div>'
                for a in labels if atext.get(a))
        secs_html.append(
            f'<div class="artsec" data-oc="{_esc(art)}">'
            f'<div class="arthd">조례 {_esc(art)}</div>{ord_src}{body}</div>')

    return (
        f'<div class="ord"><h2>{_esc(o["name"])}</h2>'
        f'<div class="meta">시행 {_fmtdate(o["enforce_date"])} · 정비 항목 {len(o["items"])}건 · '
        f'조례 조문 순서로 정렬</div>'
        f'<div class="badges">{"".join(badges)}</div>'
        f'{"".join(secs_html)}</div>')


def render_html(model, generated_at="", title="군포시 자치법규 정비 권고서"):
    s = model["summary"]
    cards = (
        _card(s, "mechanical", "기계적 개정") + _card(s, "review", "실질 검토") +
        _card(s, "check", "확인 필요") + _card(s, "format", "서식 정비") +
        _card(s, "current", "현행 유지"))
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


def recommend_fragment(mst, db_path=db.DEFAULT_DB, collapsible=False):
    """단일 조례(mst)의 권고 뷰 조각 — UI(통합 뷰) 임베드용.

    반환: {found, mst, name, grades, items_count, html, css}
      - html : 권고서와 동일 마크업의 조례 1건 블록(<div class="ord">…). 정비항목 0이면 빈 문자열.
      - css  : 권고서 CSS(_CSS). 페이지에 1회만 주입하면 됨.
    collapsible=True면 인용 항목을 <details>로 접어(대시보드 우측 패널) 좌측 본문 인용과 연동.
    findings 가 모두 current(현행)면 found=False, html='' (정비 불필요).
    """
    model = build_model(db_path, mst=mst, include_current=collapsible)
    ords = model["ordinances"]
    if not ords:
        return {"found": False, "mst": str(mst), "name": "", "grades": {},
                "items_count": 0, "html": "", "css": _CSS}
    o = ords[0]
    return {"found": True, "mst": str(mst), "name": o["name"],
            "grades": o["grades"], "items_count": len(o["items"]),
            "html": _ord_block(o, collapsible=collapsible), "css": _CSS}


def model_to_csv(model, dept=""):
    """권고 모델 → CSV 문자열(과별 통지·결재용). Excel 한글 위해 UTF-8 BOM 부착.

    한 행 = 정비 항목 1건. 조례 조문 순서(모델 정렬)를 그대로 보존.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["담당과", "조례", "조례시행일", "조례조문", "등급", "법령", "상위법조문",
                "변경유형", "행동지시", "당시법령본", "현행조문"])
    for o in model["ordinances"]:
        for it in o["items"]:
            w.writerow([
                dept, o["name"], _fmtdate(o["enforce_date"]),
                it.get("ord_clause", ""), GRADE_META[it["grade"]]["label"],
                it.get("law_name", ""), it.get("clause_label", ""),
                ("서식" if it["grade"] == "format" else it.get("change_type", "")),
                it.get("action", ""),
                _fmtdate(it.get("old_enforce")), _fmtdate(it.get("clause_enforce"))])
    return "﻿" + buf.getvalue()


def write_report(db_path=db.DEFAULT_DB, out_path="개정권고서.html", generated_at=None):
    """권고서 HTML 파일 생성 → out_path 반환."""
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    model = build_model(db_path)
    htmltext = render_html(model, generated_at=generated_at)
    with open(out_path, "w", encoding="utf-8") as fp:
        fp.write(htmltext)
    return out_path, model["summary"]
