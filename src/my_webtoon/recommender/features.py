"""1단계: 작품 → 벡터 (콘텐츠 기반 추천의 "아이템 표현").

작품 하나를 두 종류의 TF-IDF 벡터로 만든 뒤 이어 붙인다.

  (a) 텍스트 벡터 — 제목 + 줄거리를 '글자 n-gram(2~3글자)'으로 쪼갠다.
      한국어는 조사/어미가 붙어서 단어 단위로 쪼개면 "회귀한", "회귀했다"가 다른 단어가 된다.
      글자 n-gram 은 형태소 분석기 없이도 "회귀"라는 공통 조각을 잡아낸다.
      제목은 두 번 넣어서 줄거리보다 조금 더 무겁게 친다.

  (b) 메타 벡터 — 장르 / 해시태그 / 작가 / 출판사 를 "토큰" 하나씩으로 본다.
      ["장르:무협", "#먼치킨", "#회귀", "작가:목하치수", ...]
      같은 작가, 같은 태그는 줄거리 문장보다 취향을 훨씬 직접적으로 드러낸다.

두 벡터는 각각 길이 1로 정규화돼 있으므로, sqrt(가중치)를 곱해 이어 붙이면
최종 코사인 유사도 = text_weight * 텍스트유사도 + meta_weight * 메타유사도 가 된다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize

from ..db import Comic

# 텍스트 0.7 : 메타 0.3 — `my-webtoon eval` 로 0.5:0.5 / 0.3:0.7 등과 비교해 골랐다 (docs/03)
DEFAULT_TEXT_WEIGHT = 0.7
DEFAULT_META_WEIGHT = 0.3


def text_of(comic: Comic) -> str:
    return f"{comic.title} {comic.title} {comic.synopsis}"


def meta_tokens(comic: Comic) -> list[str]:
    tokens = []
    if comic.genre:
        tokens.append(f"장르:{comic.genre}")
    # 네이버는 장르를 첫 해시태그로 한 번 더 붙인다(#소년). 장르를 두 번 세지 않도록 뺀다.
    tokens += [f"#{t}" for t in comic.tags if t != comic.genre]
    tokens += [f"작가:{a}" for a in comic.authors]
    if comic.publisher:
        tokens.append(f"출판사:{comic.publisher}")
    return tokens


def _identity(tokens: list[str]) -> list[str]:
    return tokens


@dataclass
class ItemFeatures:
    product_nos: list[int]
    index: dict[int, int]  # product_no → 행 번호
    matrix: sparse.csr_matrix  # (작품 수, 특성 수), 각 행 길이 1
    meta_matrix: sparse.csr_matrix  # 설명("왜 추천?")을 만들 때 쓰는 메타 부분만
    meta_vocab: np.ndarray  # meta_matrix 의 열 이름 ("#먼치킨" 등)

    def row(self, product_no: int) -> sparse.csr_matrix:
        return self.matrix[self.index[product_no]]


def build_features(
    comics: list[Comic],
    text_weight: float = DEFAULT_TEXT_WEIGHT,
    meta_weight: float = DEFAULT_META_WEIGHT,
) -> ItemFeatures:
    if not comics:
        raise ValueError("작품이 하나도 없다 — 먼저 크롤링을 돌릴 것")

    text_vec = TfidfVectorizer(
        analyzer="char_wb", ngram_range=(2, 3), sublinear_tf=True, max_features=100_000
    )
    x_text = text_vec.fit_transform([text_of(c) for c in comics])

    # 메타 토큰이 하나도 없는 작품도 있으니 빈 문서를 허용하는 identity analyzer 를 쓴다.
    meta_vec = TfidfVectorizer(analyzer=_identity, lowercase=False)
    docs = [meta_tokens(c) for c in comics]
    if any(docs):
        x_meta = meta_vec.fit_transform(docs)
        vocab = meta_vec.get_feature_names_out()
    else:
        x_meta = sparse.csr_matrix((len(comics), 0))
        vocab = np.array([], dtype=object)

    combined = sparse.hstack(
        [math.sqrt(text_weight) * x_text, math.sqrt(meta_weight) * x_meta], format="csr"
    )
    product_nos = [c.product_no for c in comics]
    return ItemFeatures(
        product_nos=product_nos,
        index={no: i for i, no in enumerate(product_nos)},
        matrix=normalize(combined),
        meta_matrix=sparse.csr_matrix(x_meta),
        meta_vocab=vocab,
    )
