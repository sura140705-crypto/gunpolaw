# -*- coding: utf-8 -*-
"""지자체 설정 — 타 시군 재사용. 코드 수정 없이 대상 지자체만 교체한다.

우선순위: 개별 환경변수 > JSON 파일 > 기본값(군포시).
  LAW_ORG     광역 지자체코드(목록 API org)        예: 6410000(경기도)
  LAW_SBORG   시군구 지자체코드(lnkOrg/목록 필터)   예: 4020000(군포시)
  LAW_REGION  표시용 지자체명                       예: 군포시
JSON(기본 region.json, 또는 GUNPOLAW_CONFIG 경로):
  {"org": "...", "sborg": "...", "region_name": "...", "knd_codes": [...]}

서빙(serve)·재파싱(reparse)은 batch_meta(DB)의 region_name 을 읽으므로 이 설정과 무관.
이 설정은 '수집(batch)' 경계에서만 쓰인다.
"""
import json
import os
from pathlib import Path

DEFAULTS = {
    "org": "6410000",        # 광역(경기도)
    "sborg": "4020000",      # 시군(군포시)
    "region_name": "군포시",
    # 전수 대상 자치법규 종류(조례/규칙/훈령/예규/고시 등)
    "knd_codes": ["30001", "30002", "30003", "30004", "30010", "30011"],
}


def load(path=None):
    """설정 dict 반환 — 기본값 위에 JSON·환경변수 순으로 덮어쓴다."""
    cfg = dict(DEFAULTS)
    p = Path(path or os.environ.get("GUNPOLAW_CONFIG", "region.json"))
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass                                   # 깨진 설정은 무시하고 기본값 사용
    for key, env in (("org", "LAW_ORG"), ("sborg", "LAW_SBORG"),
                     ("region_name", "LAW_REGION")):
        v = os.environ.get(env)
        if v:
            cfg[key] = v.strip()
    return cfg
