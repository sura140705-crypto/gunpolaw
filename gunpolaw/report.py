# -*- coding: utf-8 -*-
"""3단계: 개정 권고서 생성.

findings(변경 탐지 결과)를 담당자가 바로 결재·정비에 쓰는 "행동 지시서"로 변환.
시스템 역할 = 등급 분류 + 조항별 행동 문안 초안 + 근거. 최종 판단은 담당자 몫.

산출물: 표준 라이브러리만으로 만든 단일 HTML(인쇄→PDF 가능, 의존성 0).

등급(grade) — finding의 (severity, change_type)에서 도출:
  mechanical 🔧 기계적 개정 : 인용 조문번호만 정정(번호이동 등)
  review     ⚠️ 검토 필요   : 내용변경·삭제 — 조문 정비 검토
  check      📋 확인        : 소재 미확인·법령미해결 — 사람 확인
  current    ✅ 현행 유지   : 변경 없음(권고서 본문엔 집계만)
"""
import csv
import difflib
import html
import io
import re
from datetime import datetime
from urllib.parse import quote

from . import db
from .parse import parse_ordinance_body
from .extract import normalize_text, is_local_admrul


# ---------- 등급 도출 ----------
GRADE_META = {
    "mechanical": {"order": 0, "emoji": "🔧", "label": "조문번호 정정", "cls": "g-mech",
                   "desc": "내용 동일·번호만 이동"},
    "review":     {"order": 1, "emoji": "⚠️", "label": "검토 필요",   "cls": "g-rev",
                   "desc": "상위법 내용이 바뀜"},
    "check":      {"order": 2, "emoji": "📋", "label": "확인 필요",     "cls": "g-chk",
                   "desc": "자동 대조 불가 · 직접 확인"},
    "format":     {"order": 3, "emoji": "📐", "label": "서식 정정",     "cls": "g-fmt",
                   "desc": "「」·띄어쓰기 등 표기"},
    "current":    {"order": 4, "emoji": "✅", "label": "현행 유지",     "cls": "g-cur",
                   "desc": "변경 없음"},
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
    """당시/현행 내용을 토큰 diff 하여 바뀐 부분을 <mark>로 감싼 (당시HTML, 현행HTML).

    교체는 당시=빨강(d)·현행=초록(i). 한쪽에만 생긴 변화(순수 삽입/삭제)는 difflib이
    공통 토큰을 양끝 앵커로 잡아 반대쪽에 표시가 사라지는데(예: '제77조부터 제84조까지'→
    '제77조부터 제79조까지, 제82조부터 제84조까지'는 가운데 삽입), 그 경우 반대쪽에 삽입·
    삭제 지점 캐럿(‸)을 찍어 당시·현행 양쪽 모두에서 변화가 드러나게 한다.
    """
    a, b = _TOKEN_RE.findall(old or ""), _TOKEN_RE.findall(new or "")
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    o_html, n_html = [], []
    caret = '<mark class="pt" title="{}">‸</mark>'
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        at, bt = "".join(a[i1:i2]), "".join(b[j1:j2])
        if op == "equal":
            o_html.append(_esc(at))
            n_html.append(_esc(bt))
        elif op == "insert":                      # 현행에만 추가 → 당시에 삽입 지점 캐럿
            n_html.append(f'<mark class="i">{_esc(bt)}</mark>' if bt.strip() else _esc(bt))
            if bt.strip():
                o_html.append(caret.format(_esc(bt.strip()[:40]) + " 추가됨"))
        elif op == "delete":                      # 당시에만 있던 것 삭제 → 현행에 삭제 지점 캐럿
            o_html.append(f'<mark class="d">{_esc(at)}</mark>' if at.strip() else _esc(at))
            if at.strip():
                n_html.append(caret.format(_esc(at.strip()[:40]) + " 삭제됨"))
        else:                                     # replace — 양쪽 모두 표시
            o_html.append(f'<mark class="d">{_esc(at)}</mark>' if at.strip() else _esc(at))
            n_html.append(f'<mark class="i">{_esc(bt)}</mark>' if bt.strip() else _esc(bt))
    return "".join(o_html), "".join(n_html)


def _diff_unified(old, new):
    """당시·현행을 한 흐름으로 합친 인라인 diff HTML. 삭제=취소선 빨강(d),
    추가=초록(i)을 같은 자리에 나란히 표기(교정지·변경이력식). 예: '기획재정부장관이'가
    '재정경제부장관이'로 바뀌면 '<del>기획재정부장관이</del><ins>재정경제부장관이</ins>'처럼
    한 문장에서 보인다. 당시·현행 두 블록을 각각 읽지 않아도 무엇이 바뀌었는지 즉시 파악."""
    a, b = _TOKEN_RE.findall(old or ""), _TOKEN_RE.findall(new or "")
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        at, bt = "".join(a[i1:i2]), "".join(b[j1:j2])
        if op == "equal":
            out.append(_esc(at))
        elif op == "insert":
            out.append(f'<mark class="i">{_esc(bt)}</mark>' if bt.strip() else _esc(bt))
        elif op == "delete":
            out.append(f'<mark class="d">{_esc(at)}</mark>' if at.strip() else _esc(at))
        else:                                     # replace — 삭제(취소선) 뒤 추가(초록)
            out.append(f'<mark class="d">{_esc(at)}</mark>' if at.strip() else _esc(at))
            out.append(f'<mark class="i">{_esc(bt)}</mark>' if bt.strip() else _esc(bt))
    return "".join(out)


def grade_of(severity, change_type=""):
    """finding의 등급 키. 1단계(mechanical)·2단계(번호이동) 모두 수용."""
    if severity == "current":
        return "current"
    if severity == "mechanical" or change_type == "번호이동":
        return "mechanical"
    if severity == "review":
        return "review"
    return "check"


def finding_grade(severity, change_type="", cite_naked=0, cite_spacing=0):
    """finding 1건의 **최종 등급 키**(권고서·대시보드 공용 단일 출처).

    grade_of 위에 '서식 정비' 승격 규칙을 더한다: 내용은 현행이나 「」 없이 맨몸 인용됐거나
    (cite_naked) 약칭이 붙여쓰기된(cite_spacing) 서식 결함은 current가 아니라 format으로.
    """
    g = grade_of(severity, change_type)
    if g == "current" and (cite_naked or cite_spacing):
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
def _josa(word, jong, nojong):
    """받침 유무로 조사 선택. 끝글자가 한글이고 종성이 있으면 jong, 없으면 nojong.
    끝이 비한글(」·숫자 등)이면 받침 없는 것으로 본다(발음 관용)."""
    if not word:
        return nojong
    ch = word[-1]
    if "가" <= ch <= "힣":
        return jong if (ord(ch) - 0xAC00) % 28 else nojong
    return nojong


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
    naked = _get(f, "cite_naked")
    spacing = _get(f, "cite_spacing")
    # 내용은 현행이지만 서식(꺽쇠/띄어쓰기) 결함만 → 서식 정정만 안내(내용 변경 없음)
    if (naked or spacing) and _get(f, "severity") == "current":
        cl = f" {clause}" if clause else ""
        if naked:
            a = f"{law}{cl}"            # 현재(꺽쇠 없는 맨몸 인용)
            b = f"「{law}」{cl}"          # 정정(꺽쇠 서식)
            return (f'(서식만 정비) "{a}"{_josa(a, "을", "를")} '
                    f'"{b}"{_josa(b, "으로", "로")} 정정 — 내용 변경은 없음 ({loc})')
        return (f'(서식만 정비) 「{law}」 약칭을 앞말과 띄어 정정 ({loc}) '
                f"— 내용 변경은 없음, 띄어쓰기만 정비")
    # 내용 변경이 동반된 서식 결함은 내용 정비와 함께 서식도 바로잡도록 덧붙임
    extra = (f' (덧붙여 「{law}」처럼 꺽쇠 서식으로 정정)' if naked else "")
    extra += " (덧붙여 약칭 앞 띄어쓰기 정정)" if spacing else ""

    def _base():
        if g == "mechanical":
            # detail 안에 "제X조 → 제Y조" 이동처 안내가 들어 있음
            return f"「{law}」 {clause} 인용을 현행 조문번호로 정정 ({loc}) — {_get(f, 'detail')}"
        if g == "review":
            if ct == "내용변경":
                return f"「{law}」 {clause} 개정 내용을 반영하여 {loc}{_josa(loc, '을', '를')} 검토·정비"
            if ct == "삭제":
                return f"「{law}」 {clause} 삭제·통합 여부를 확인하고 {loc}의 인용을 정비"
            if ct == "번호이동":
                return f"「{law}」 {clause} 조문번호 이동 — {loc}의 인용 조문번호 정정"
            return f"「{law}」 {clause} 변경 사항을 검토하여 {loc} 정비"
        # check
        if ct == "지자체행정규칙":
            return (f"「{law}」는 지자체 자체 행정규칙 — 국가법령정보에 없어 자동 대조 불가. "
                    f"자치법규정보시스템(ELIS, www.elis.go.kr)에서 최신 원문을 직접 확인해 "
                    f"{loc} 인용을 정비")
        if ct == "법령미해결":
            # 자동 대조는 국가법령정보 '법령'만 수집 — 중앙부처 행정규칙(훈령·예규·고시·규정)은
            # 국가법령정보센터 '행정규칙'에 있으나 매칭 안 됨. 폐지·개명·오기, 자체 규칙 가능성도.
            return (f"「{law}」 자동 매칭 실패 — 국가법령정보센터(법령·행정규칙)에서 현행 여부·명칭 확인. "
                    f"중앙부처 행정규칙(훈령·예규·규정)이거나 제명변경·폐지일 수 있고, "
                    f"지자체 자체 규칙이면 자치법규정보시스템(ELIS)에서 확인 — {loc} 인용 정비")
        if ct == "당시부재":
            return f"「{law}」 {clause} 인용 시점을 확인 — {loc} (제정 당시 부재)"
        if ct == "호미확인":
            cd = _get(f, "clause_detail") or ""
            return (f"「{law}」 {clause}{cd} 변경 여부 확인 — {clause}{_josa(clause, '은', '는')} 조례 시행 이후 "
                    f"개정됐으나 인용한 {cd or '해당 호'}엔 개정 표기가 없어 변경 여부 불명확({loc})")
        return f"「{law}」 {clause} 소재를 확인(삭제·이동·오기 여부) — {loc}"

    return _base() + extra


def action_html(f):
    """action_text 의 검토(우측) 패널용 HTML — '무엇을 → 무엇으로' 고칠지가 드러나도록
    서식 정정(신·구 표기)·제명변경(신·구 명칭)만 강조 span 을 넣는다. 문안은 그대로 두고
    색만 입혀 총괄자가 정정 지점을 한눈에 보게 함. 그 외 유형은 평문(_esc)."""
    ct = _get(f, "change_type")
    law = _get(f, "law_name") or "(법령명 미상)"
    clause = _get(f, "clause_label")
    naked = _get(f, "cite_naked")
    sev = _get(f, "severity")
    # 제명변경 — 옛 법령명(회색) → 현행 법령명(초록). 문안은 detail 그대로 두고 색만.
    if ct == "제명변경":
        det = _get(f, "detail") or ""
        m = re.search(r"현행\s*「([^」]+)」", det)
        new = m.group(1) if m else ""
        if new:
            out = _esc(det)
            out = out.replace(_esc(f"「{law}」"),
                              f'<span class="fx-old">{_esc(f"「{law}」")}</span>', 1)
            out = out.replace(_esc(f"「{new}」"),
                              f'<span class="fx-new">{_esc(f"「{new}」")}</span>', 1)
            return out
        return _esc(action_text(f))
    # 서식만 정비(「」 누락) — 맨몸 표기(회색) → 낫표 표기(삽입 「」를 초록으로)
    if naked and sev == "current":
        here = _get(f, "ord_clause")
        loc = f"이 조례 {here}" if here else "이 조례의 해당 조문"
        cl = f" {clause}" if clause else ""
        a = f"{law}{cl}"
        b_plain = f"「{law}」{cl}"
        b_html = f'<ins class="fx-brk">「</ins>{_esc(law)}<ins class="fx-brk">」</ins>{_esc(cl)}'
        return ('(서식만 정비) "'
                f'<span class="fx-old">{_esc(a)}</span>"{_josa(a, "을", "를")} "'
                f'<span class="fx-new">{b_html}</span>"{_josa(b_plain, "으로", "로")} 정정 '
                f'— <b>내용 변경은 없음</b> ({_esc(loc)})')
    return _esc(action_text(f))


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
        f"""SELECT f.mst, f.law_name, {sel('law_id')}, f.clause_label, {sel('clause_detail')},
                  f.severity, f.change_type,
                  {sel('ord_clause')}, {sel('ord_seq', '999999')}, f.detail, f.evidence,
                  f.ord_enforce, {sel('old_enforce')}, f.clause_enforce,
                  {sel('cite_naked', '0')}, {sel('cite_spacing', '0')},
                  o.name AS ord_name, o.enforce_date AS ord_enforce_date
           FROM findings f LEFT JOIN ordinances o ON o.mst = f.mst
           {where}
           ORDER BY f.law_name, f.clause_label""", args
    ).fetchall()
    # 조례 본문(조문별 텍스트) — 권고에 '무엇을 고칠지' 조례 원문을 함께 보여주기 위함.
    # 인용(법령/조례) 위치는 citations span 으로 밑줄 표기(articles_html).
    msts = {r["mst"] for r in rows}
    body_articles, body_html = {}, {}
    if msts:
        qm = ",".join("?" * len(msts))
        cites_by = {}                       # (mst, 조문) → [인용 span…]
        ccols = [c[1] for c in conn.execute("PRAGMA table_info(citations)")]
        if "span_start" in ccols:
            for cr in conn.execute(
                    f"""SELECT mst, article_no, law_name, clause_label, span_start,
                              span_end, cite_naked, cite_type
                        FROM citations WHERE mst IN ({qm})""", list(msts)):
                cites_by.setdefault((cr["mst"], cr["article_no"] or ""), []).append(dict(cr))
        for br in conn.execute(
                f"SELECT mst, body_xml FROM ordinances WHERE mst IN ({qm})", list(msts)):
            if not br["body_xml"]:
                continue
            parsed = parse_ordinance_body(br["body_xml"])
            if "error" in parsed:
                continue
            txt, htm = {}, {}
            for a in parsed["articles"]:
                no = a.get("no")
                if not no:
                    continue
                body = a.get("body", "")
                txt[no] = body
                htm[no] = _highlight_ordtext(body, cites_by.get((br["mst"], no), []))
            body_articles[br["mst"]] = txt
            body_html[br["mst"]] = htm
    conn.close()

    by_mst = {}
    summary = {k: 0 for k in GRADE_KEYS}
    for r in rows:
        # 내용 현행 + 서식 결함(맨몸/띄어쓰기)은 finding_grade가 '서식 정비'로(단일 출처)
        g = finding_grade(r["severity"], r["change_type"], r["cite_naked"], r["cite_spacing"])
        summary[g] += 1
        o = by_mst.setdefault(r["mst"], {
            "mst": r["mst"],
            "name": r["ord_name"] or "(조례명 미상)",
            "enforce_date": r["ord_enforce_date"] or r["ord_enforce"] or "",
            "grades": {k: 0 for k in GRADE_KEYS},
            "items": [],
            "articles_text": body_articles.get(r["mst"], {}),
            "articles_html": body_html.get(r["mst"], {}),
        })
        o["grades"][g] += 1
        if g != "current":
            o["items"].append({
                "grade": g,
                "law_name": r["law_name"] or "",
                "law_id": r["law_id"] or "",
                "clause_label": r["clause_label"] or "",
                "clause_detail": r["clause_detail"] or "",
                "change_type": r["change_type"] or "",
                "ord_clause": r["ord_clause"] or "",
                "ord_seq": r["ord_seq"] if r["ord_seq"] is not None else 999999,
                "action": action_text(r),
                "action_html": action_html(r),
                "evidence": r["evidence"] or "",
                "ord_enforce": r["ord_enforce"] or o["enforce_date"],
                "old_enforce": r["old_enforce"] or "",
                "clause_enforce": r["clause_enforce"] or "",
                "cite_naked": r["cite_naked"] or 0,
                "cite_spacing": r["cite_spacing"] or 0,
            })
        elif include_current:
            # 변경 없음(현행) 인용도 '검토 완료'로 표기 — 누락이 아니라 검토 결과임을 명시
            o["items"].append({
                "grade": "current",
                "law_name": r["law_name"] or "",
                "law_id": r["law_id"] or "",
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
                "cite_spacing": r["cite_spacing"] or 0,
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
/* 변수는 .page(독립 권고서)·.ord(대시보드 주입 블록)에만 — :root 로 두면 대시보드의
   자체 변수를 덮어써 색이 바뀐다. 두 루트에 한정해 주입 시 오염을 막는다. */
.page, .ord { --navy:#13325b; --navy2:#1d4e89;
  --mech:#1d4ed8; --rev:#b45309; --chk:#52525b; --fmt:#6d28d9; --cur:#15803d;
  --ink:#1f2937; --muted:#6b7280; --line:#d8dee6; --soft:#f6f8fb; }
* { box-sizing: border-box; }
/* 독립 권고서 페이지 한정(body.report·.page) — 대시보드 주입 시 충돌 방지.
   대시보드는 자체 body/h1/.cards/.sub/.card 를 가져 prefix 없으면 덮어쓴다. */
body.report { font-family: "Malgun Gothic","맑은 고딕",system-ui,sans-serif;
       color:var(--ink); margin:0; background:#eef1f5; line-height:1.55;
       -webkit-text-size-adjust:100%; }
.page { max-width:840px; margin:24px auto; padding:38px 44px 52px; background:#fff;
        border-top:4px solid var(--navy); box-shadow:0 1px 5px rgba(15,33,66,.10); }
/* 문서 머리 — 공문 단정형(머리표·제목·요지, 아래 굵은 밑줄) */
.page .dochd { border-bottom:2px solid var(--navy); padding-bottom:13px; margin-bottom:22px; }
.page .dochd .kicker { font-size:11.5px; letter-spacing:3px; color:var(--navy2);
        font-weight:700; margin:0 0 6px; }
.page h1 { font-size:22px; line-height:1.34; margin:0; color:var(--navy);
     font-weight:700; letter-spacing:-.3px; }
.page .sub { color:var(--muted); font-size:12.5px; margin-top:7px; }
.page .cards { display:flex; gap:10px; flex-wrap:wrap; margin:0 0 20px; }
.page .card { flex:1 1 138px; border:1px solid var(--line); border-top:3px solid var(--chk);
        border-radius:6px; padding:12px 15px; }
.page .card .n { font-size:25px; font-weight:700; line-height:1; color:var(--ink); }
.page .card .t { font-size:12.5px; color:var(--muted); margin-top:6px; }
.page .g-mech { border-top-color:var(--mech);} .page .g-mech .n { color:var(--mech);}
.page .g-rev { border-top-color:var(--rev);} .page .g-rev .n { color:var(--rev);}
.page .g-chk { border-top-color:var(--chk);} .page .g-chk .n { color:var(--chk);}
.page .g-fmt { border-top-color:var(--fmt);} .page .g-fmt .n { color:var(--fmt);}
.page .g-cur { border-top-color:var(--cur);} .page .g-cur .n { color:var(--cur);}
.ord { border:1px solid var(--line); border-radius:7px; padding:18px 20px;
       margin:0 0 16px; page-break-inside:avoid; }
.ord h2 { font-size:16.5px; margin:0 0 3px; color:var(--navy); font-weight:700;
          padding-left:10px; border-left:4px solid var(--navy); }
.ord .meta { color:var(--muted); font-size:12px; margin:0 0 12px; padding-left:14px; }
.badges { margin:0 0 12px; }
.badge { display:inline-block; font-size:11.5px; padding:2px 10px; border-radius:4px;
         margin-right:6px; border:1px solid currentColor; background:#fff; font-weight:600; }
.b-mech { color:var(--mech);} .b-rev { color:var(--rev);}
.b-chk { color:var(--chk);} .b-fmt { color:var(--fmt);}
.artsec { margin:0 0 8px; }
.arthd { font-size:13px; font-weight:700; color:var(--navy); background:var(--soft);
         border-left:3px solid var(--navy2); padding:5px 11px; border-radius:0 4px 4px 0;
         margin:14px 0 6px; display:flex; align-items:center; gap:7px; flex-wrap:wrap; }
/* 조문별 검토 상태 칩 — 정비 대상 등급 or ✅검토완료(제외대상만) */
.secst { font-size:11px; font-weight:700; border-radius:999px; padding:1px 9px; }
.secst.st-ok { background:#dcfce7; color:#166534; }
.secst.st-review { background:#fef3c7; color:#92400e; }
.secst.st-check { background:#f3f4f6; color:#374151; }
.secst.st-format { background:#ede9fe; color:#5b21b6; }
.secst.st-mechanical { background:#dbeafe; color:#1e40af; }
.item { border-top:1px solid #f3f4f6; padding:9px 0 9px 10px; }
.iline { margin-bottom:3px; }
.item .tag { font-size:11px; font-weight:700; padding:1px 7px; border-radius:6px;
             margin-right:8px; white-space:nowrap; }
.t-mech { background:#dbeafe; color:#1e40af;} .t-rev { background:#fef3c7; color:#92400e;}
.t-chk { background:#f3f4f6; color:#374151;}
.t-naked { background:#ede9fe; color:#5b21b6;}
.t-cur { background:#dcfce7; color:#166534;}
.t-space { background:#fef9c3; color:#854d0e;}
details.citem.cur > summary { opacity:.72; }
details.citem.cur .law { font-weight:500; }
/* 접이식 인용 항목(대시보드 우측) — 기본 접힘, 좌측 인용 클릭 시 펼침 */
details.citem { border-top:1px solid #f3f4f6; padding:0; border-left:4px solid transparent; }
/* 심각도 좌측 색바(4px) — .focus 는 box-shadow 라 색바와 공존 */
details.citem[data-sev="mechanical"] { border-left-color:var(--mech); }
details.citem[data-sev="review"] { border-left-color:var(--rev); }
details.citem[data-sev="check"] { border-left-color:var(--chk); }
details.citem[data-sev="format"] { border-left-color:var(--fmt); }
details.citem[data-sev="current"] { border-left-color:#d1d5db; }
/* 변경없음 숨기기(우측 헤더 체크) — .drec 는 serve.py 컨테이너 */
.drec.hide-cur details.citem.cur { display:none; }
details.citem > summary { list-style:none; cursor:pointer; padding:9px 10px 9px 8px;
    display:flex; align-items:center; gap:5px; flex-wrap:wrap; }
details.citem > summary::-webkit-details-marker { display:none; }
details.citem > summary::before { content:"▸"; color:#9ca3af; font-size:11px; margin-right:2px; }
details.citem[open] > summary::before { content:"▾"; color:var(--navy2); }
details.citem[open] { background:#fbfcfe; }
details.citem > .act { padding:2px 10px 0; } details.citem > .basis { padding:0 10px; }
details.citem > .diff { margin:6px 10px 10px; }
details.citem.focus { box-shadow:inset 3px 0 0 var(--navy2); }
.item .law { font-size:13px; font-weight:600; color:#374151; }
.item .law .ki { font-weight:400; font-size:12px; margin-right:1px; }
.item a.ais { font-size:11px; font-weight:600; color:#0369a1; background:#e0f2fe;
    border-radius:6px; padding:1px 7px; margin-left:6px; text-decoration:none; white-space:nowrap; }
.item a.ais:hover { background:#bae6fd; }
.item .act { font-size:13.5px; margin:3px 0; color:var(--ink); }
/* 검토 문안 내 정정 강조 — 현재 표기(회색) → 정정 표기(초록), 삽입 「」는 진한 초록 */
.act .fx-old { background:#f1f5f9; color:#64748b; border-radius:3px; padding:0 3px; }
.act .fx-new { background:#eafaf0; color:#15803d; border-radius:3px; padding:0 3px; font-weight:600; }
.act .fx-new .fx-brk { background:#bbf7d0; color:#166534; font-weight:800; border-radius:2px;
    padding:0 1px; margin:0 1px; text-decoration:none; }
.basis { font-size:11.5px; color:var(--muted); margin:4px 0 6px; }
.basis b { color:#374151; font-weight:600; }
.ordtext { font-size:12.5px; color:var(--ink); background:var(--soft); border:1px solid var(--line);
           border-left:3px solid var(--navy2); border-radius:0 6px 6px 0; padding:8px 11px;
           margin:5px 0 9px; white-space:pre-wrap; line-height:1.7; }
.ordtext b { color:var(--navy); margin-right:5px; }
mark.cite-law { background:#eff6ff; color:#1e3a8a;
   text-decoration:underline; text-decoration-color:#2563eb; text-underline-offset:2px;
   border-radius:2px; padding:0 1px; }
mark.cite-naked { background:#f5f3ff; color:#5b21b6;
   text-decoration:underline dashed; text-decoration-color:#7c3aed; text-underline-offset:2px;
   border-radius:2px; padding:0 1px; }
mark.cite-local { background:#f1f5f9; color:#475569;
   text-decoration:underline dotted; text-decoration-color:#94a3b8; text-underline-offset:2px;
   border-radius:2px; padding:0 1px; }
.page .leg { font-size:12px; color:var(--muted); margin:0 0 18px; display:flex; gap:14px;
        flex-wrap:wrap; align-items:center; }
.page .leg mark { padding:0 4px; border-radius:2px; }
.item .ev { font-size:12px; color:#6b7280; white-space:pre-wrap;
            background:#f9fafb; border-radius:6px; padding:7px 9px; margin-top:6px; }
.diff { border:1px solid #eef0f3; border-radius:6px; overflow:hidden; margin-top:6px; }
.diff .diff-uni { font-size:12px; padding:7px 9px; white-space:pre-wrap;
  word-break:keep-all; overflow-wrap:anywhere; color:#374151; line-height:1.55; }
.diff .diff-split { display:none; }                 /* 대시보드 기본 = 통합(합쳐) 보기 */
/* 나란히 모드(대시보드 토글) · 인쇄 권고서(.page)는 당시·현행 나란히 */
.diff-mode-split .diff .diff-uni, .page .diff .diff-uni { display:none; }
.diff-mode-split .diff .diff-split, .page .diff .diff-split { display:block; }
.drow { display:flex; gap:8px; font-size:12px; padding:6px 9px; }
.drow + .drow { border-top:1px solid #f1f3f5; }
.dlabel { flex:0 0 34px; font-weight:700; font-size:11px; padding-top:1px; }
.dlabel.was { color:#b91c1c; } .dlabel.now { color:#15803d; }
.dtext { flex:1; white-space:pre-wrap; word-break:keep-all; overflow-wrap:anywhere; color:#374151; line-height:1.5; }
mark.d { background:#fee2e2; color:#991b1b; text-decoration:line-through;
         border-radius:2px; padding:0 1px; }
mark.i { background:#dcfce7; color:#166534; border-radius:2px; padding:0 1px; }
mark.pt { background:#fee2e2; color:#dc2626; font-weight:700; border-radius:2px;
          padding:0 2px; cursor:help; }
.page footer { color:#9aa3af; font-size:11.5px; margin-top:34px; padding-top:14px;
         border-top:1px solid var(--line); text-align:center; }
@media print {
  @page { size:A4; margin:16mm; }
  body { background:#fff; }
  .page { max-width:none; margin:0; padding:0; border-top:none; box-shadow:none; }
  .ord, .card { border-color:#c4cbd4; }
  .dochd { border-bottom-color:#000; }
  mark.d, mark.i, mark.pt, .ordtext mark, .card, .badge {
    -webkit-print-color-adjust:exact; print-color-adjust:exact; }
}
"""


def _esc(s):
    return html.escape(str(s or ""))


def _highlight_ordtext(body, cites):
    """조례 조문 본문에 인용(법령/조례) 위치를 <mark>로 표기(밑줄). span은 citations 의
    normalize_text(본문) 기준 offset이라 같은 정규화 텍스트에 적용한다. 인용 없으면 평문.

    cite-law=상위법령(파랑 밑줄)·cite-naked=꺽쇠 누락 서식결함(보라 점선)·
    cite-local=타 자치법규(회색 밑줄). 대시보드 통합뷰와 같은 분류·색 계열.
    """
    text = normalize_text(body)
    if not cites:
        return _esc(text)
    spans = sorted(cites, key=lambda c: (c.get("span_start") or 0, c.get("span_end") or 0))
    out, pos, n = [], 0, len(text)
    for c in spans:
        s, e = c.get("span_start") or 0, c.get("span_end") or 0
        if s < pos or e > n or s >= e:            # 겹침/이상치 방어
            continue
        out.append(_esc(text[pos:s]))
        if c.get("cite_type") == "자치법규":
            cls = "cite-local"
        elif c.get("cite_naked"):
            cls = "cite-naked"
        else:
            cls = "cite-law"
        title = (c.get("law_name") or "") + (c.get("clause_label") or "")
        out.append(f'<mark class="{cls}" title="{_esc(title)}">{_esc(text[s:e])}</mark>')
        pos = e
    out.append(_esc(text[pos:]))
    return "".join(out)


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
    # 통합(합쳐) 보기 + 나란히(당시·현행) 보기를 함께 담고 CSS/토글로 하나만 노출
    return (f'<div class="diff">'
            f'<div class="diff-uni">{_diff_unified(old, new)}</div>'
            f'<div class="diff-split">{_drow(o_html, n_html)}</div>'
            f'</div>')


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
    # 인용 종류 아이콘: 행정규칙(훈령·예규·고시, ADM/지자체자체) ⇄ 상위법령(법률·시행령·규칙)
    is_adm = str(it.get("law_id") or "").startswith("ADM:") or is_local_admrul(it.get("law_name"))
    kicon = "📕" if is_adm else "⚖"
    law = f'<span class="ki" title="{"행정규칙" if is_adm else "상위법령"}">{kicon}</span> 「{_esc(it["law_name"])}」 {_esc(clause_full)}'.rstrip()
    # 내용변경 항목엔 서식 결함 배지 병기(format 등급은 태그 자체가 '서식'이라 생략)
    badges = ""
    if it["grade"] != "format":
        if it.get("cite_naked"):
            badges += '<span class="tag t-naked">「」누락</span>'
        if it.get("cite_spacing"):
            badges += '<span class="tag t-space">띄어쓰기</span>'
    # 우리가 자동 해소 못 한 법령(법령미해결·미확인·자체규칙 등)은 법제처 지능형 검색으로 유도 —
    # 정확매칭 실패해도 사람이 이 링크로 유사·예고·폐지 법령까지 직접 확인.
    ais = ""
    if not it.get("law_id"):
        ais = (f'<a class="ais" target="_blank" rel="noopener"'
               f' href="https://www.law.go.kr/LSW/ais/searchList.do?query={quote(it.get("law_name") or "")}"'
               f' title="법제처 지능형 법령검색(Lawbot)에서 이 법령 찾기 — 유사·예고·폐지 포함">🔍 지능형 검색</a>')
    head = (f'<span class="tag {tag_cls}">{_esc(tag)}</span>{badges}'
            f'<span class="law">{law}</span>{ais}')
    body = (f'<div class="act">{it.get("action_html") or _esc(it["action"])}</div>'
            f'{_basis_line(it)}{_evidence_block(it)}')
    if collapsible:
        cls = "item citem cur" if it["grade"] == "current" else "item citem"
        oc = (it.get("ord_clause") or "").split(",")[0].strip()
        return (f'<details class="{cls}" data-sev="{it["grade"]}" data-oc="{_esc(oc)}"'
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
    ahtml = o.get("articles_html") or {}
    secs_html = []
    for art, group in sections:
        body = "".join(_item_block(it, collapsible) for it in group)
        # 조례 원문은 평면(독립 권고서)에서만 — 대시보드(collapsible)는 좌측 본문이 대신함.
        # 인용(법령/조례) 위치는 밑줄 표기(articles_html). 구 모델 호환 시 평문으로 폴백.
        ord_src = ""
        if not collapsible:
            labels = []
            for it in group:
                for a in (it.get("ord_clause") or "").split(","):
                    a = a.strip()
                    if a and a not in labels:
                        labels.append(a)
            ord_src = "".join(
                f'<div class="ordtext"><b>{_esc(a)}</b> '
                f'{ahtml.get(a) or _esc(atext.get(a, ""))}</div>'
                for a in labels if (ahtml.get(a) or atext.get(a)))
        # 조문별 상태 칩(검토 사항 쪽) — 정비 대상 등급이 있으면 그 등급, 없으면 ✅ 검토완료.
        # 제외대상(현행)만 있어 밋밋하게 넘어가던 조문도 '검토했고 문제없음'을 드러낸다.
        act = [it["grade"] for it in group if it["grade"] != "current"]
        if act:
            uniq = [g for g in ("mechanical", "review", "check", "format") if g in act]
            status = "".join(
                f'<span class="secst st-{g}">{GRADE_META[g]["emoji"]} {GRADE_META[g]["label"]}</span>'
                for g in uniq)
        else:
            status = '<span class="secst st-ok">✅ 검토완료</span>'
        secs_html.append(
            f'<div class="artsec" data-oc="{_esc(art)}">'
            f'<div class="arthd">조례 {_esc(art)}{status}</div>{ord_src}{body}</div>')

    return (
        f'<div class="ord"><h2>{_esc(o["name"])}</h2>'
        f'<div class="meta">시행 {_fmtdate(o["enforce_date"])} · 정비 항목 {len(o["items"])}건 · '
        f'조례 조문 순서로 정렬</div>'
        f'<div class="badges">{"".join(badges)}</div>'
        f'{"".join(secs_html)}</div>')


def render_html(model, generated_at="", title="자치법규 정비 권고서"):
    s = model["summary"]
    cards = (
        _card(s, "mechanical", "조문번호 정정") + _card(s, "review", "검토 필요") +
        _card(s, "check", "확인 필요") + _card(s, "format", "서식 정정") +
        _card(s, "current", "현행 유지"))
    blocks = "".join(_ord_block(o) for o in model["ordinances"])
    sub = (f'전체 {s["ordinances_total"]}개 조례 중 '
           f'정비 대상 {s["ordinances_action"]}개 · 생성 {_esc(generated_at)}')
    legend = (
        '<div class="leg">본문 밑줄 = 인용 표기 · '
        '<span><mark class="cite-law">상위법령</mark></span>'
        '<span><mark class="cite-local">타 자치법규</mark></span>'
        '<span><mark class="cite-naked">꺽쇠(「」) 누락</mark></span></div>')
    return (
        "<!DOCTYPE html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body class=\"report\">"
        f'<div class="page"><div class="dochd"><div class="kicker">자치법규 정비 점검</div>'
        f'<h1>{_esc(title)}</h1><div class="sub">{sub}</div></div>'
        f'<div class="cards">{cards}</div>{legend}{blocks}'
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


def grade_to_csv(db_path, grade):
    """특정 등급(mechanical/review/check/format)의 전 부서 정비 항목 → CSV(BOM).

    한 행 = 그 등급의 정비 항목 1건. 담당과별 정렬 — 의법팀이 등급별로 뽑아 각 부서에
    정비확인 공문 발송, 서식정정은 법무 내부 처리에 바로 쓰도록. current(현행)는 제외.
    """
    conn = db.connect(db_path)
    rows = conn.execute(
        """SELECT o.dept, o.name AS ord_name, o.enforce_date AS ord_enf, o.mst,
                  f.law_name, f.clause_label, f.clause_detail, f.change_type, f.severity,
                  f.cite_naked, f.cite_spacing, f.ord_clause, f.ord_seq, f.detail
           FROM findings f JOIN ordinances o ON o.mst = f.mst
           ORDER BY o.dept, o.name, f.ord_seq""").fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["담당과", "조례", "조례시행일", "조례조문", "등급", "법령",
                "상위법조문", "변경유형", "행동지시"])
    for r in rows:
        g = finding_grade(r["severity"], r["change_type"], r["cite_naked"] or 0,
                          r["cite_spacing"] or 0)
        if g != grade:
            continue
        clause_full = (r["clause_label"] or "") + (r["clause_detail"] or "")
        w.writerow([
            r["dept"] or "", r["ord_name"] or "", _fmtdate(r["ord_enf"]),
            r["ord_clause"] or "", GRADE_META[grade]["label"], r["law_name"] or "",
            clause_full, ("서식" if grade == "format" else r["change_type"] or ""),
            action_text(dict(r))])
    return "﻿" + buf.getvalue()


def write_ordinance_report(mst, db_path=db.DEFAULT_DB, out_path=None, generated_at=None):
    """조례 1건 분석·정비 권고 HTML 파일 생성(담당자 확인·배포용). 현행 인용까지 포함.

    반환: (out_path, summary, 조례명).
    """
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    model = build_model(db_path, mst=mst, include_current=True)
    name = model["ordinances"][0]["name"] if model["ordinances"] else str(mst)
    safe = re.sub(r"[\\/:*?\"<>|]", "_", name)        # 파일명 금지문자 치환
    out_path = out_path or f"{safe}_정비권고.html"
    htmltext = render_html(model, generated_at=generated_at,
                           title=f"{name} — 정비 권고(분석 결과)")
    with open(out_path, "w", encoding="utf-8") as fp:
        fp.write(htmltext)
    return out_path, model["summary"], name


def write_report(db_path=db.DEFAULT_DB, out_path="개정권고서.html", generated_at=None):
    """권고서 HTML 파일 생성 → out_path 반환."""
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    model = build_model(db_path)
    # 제목의 지자체명은 배치 스냅샷(batch_meta)에서 — 타 시군 재사용 시 자동 반영
    conn = db.connect(db_path)
    mrow = conn.execute("SELECT region_name FROM batch_meta WHERE id=1").fetchone()
    conn.close()
    region = (mrow["region_name"] if mrow and mrow["region_name"] else "").strip()
    title = f"{region} 자치법규 정비 권고서".strip()
    htmltext = render_html(model, generated_at=generated_at, title=title)
    with open(out_path, "w", encoding="utf-8") as fp:
        fp.write(htmltext)
    return out_path, model["summary"]
