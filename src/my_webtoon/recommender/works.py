"""같은 작품의 여러 판본 묶기.

네이버 시리즈에는 한 작품이 여러 상품으로 올라와 있다:
    '나루토' (웹툰, 화)  /  '나루토 [단행본]' (권)
    '붉은 매2'          /  '붉은매2 [단행본]'
    '열혈강호'          /  '열혈강호 [단행본]'
추천에서는 이것들을 한 작품으로 보고 가장 점수 높은 판본 하나만 보여 준다.
하나라도 평가했으면 나머지 판본도 추천하지 않는다.
"""

from __future__ import annotations

import re

# [단행본] [독점] [개정판] [선연재] [완전판] … 같은 대괄호 꼬리표, 그리고 '신장판'·'완전판' 같은 판본 표기
_TAGS = re.compile(
    r"\[[^\]]*\]|【[^】]*】|\((?:[^)]*판|단행본|컬러|흑백)\)|\s(?:신장판|완전판|애장판|개정판)\b"
)
_NOISE = re.compile(r"[\s\W_]+")


def work_key(title: str) -> str:
    """판본 표기·공백·문장부호를 뗀 소문자 제목. 이게 같으면 같은 작품으로 본다."""
    stripped = _NOISE.sub("", _TAGS.sub(" ", title)).lower()
    return stripped or title


def author_key(name: str) -> str:
    """'Ukyo kodachi' 와 'KODACHI Ukyo' 처럼 대소문자·성이름 순서만 다른 이름을 같게."""
    return "".join(sorted(_NOISE.sub("", part).lower() for part in name.split()))


def work_keys(comics) -> dict[int, str]:
    """작품들 → {product_no: 작품 키}. 제목이 같아도 작가가 하나도 안 겹치면 다른 작품으로 나눈다.

    예) '킹덤' (하라 야스히사) 과 '킹덤' (다른 작가의 국내 웹툰) 은 제목만 같은 별개 작품.
    작가 정보가 없는 판본은 같은 제목의 아무 묶음에나 붙는다 (나눌 근거가 없으니까).
    """
    by_title: dict[str, list] = {}
    for c in comics:
        by_title.setdefault(work_key(c.title), []).append(c)

    keys: dict[int, str] = {}
    for title, group in by_title.items():
        if len(group) == 1:
            keys[group[0].product_no] = title
            continue
        # 작가가 하나라도 겹치는 판본끼리 같은 묶음 (union-find 대신 단순 병합 — 묶음이 2~3개라 충분)
        clusters: list[tuple[set[str], list[int]]] = []
        for c in sorted(group, key=lambda c: -len(c.authors)):
            authors = {author_key(a) for a in c.authors} - {""}
            for names, members in clusters:
                if not authors or names & authors:
                    names |= authors
                    members.append(c.product_no)
                    break
            else:
                clusters.append((authors, [c.product_no]))
        for i, (_, members) in enumerate(clusters):
            for no in members:
                keys[no] = title if i == 0 else f"{title}#{i}"
    return keys
