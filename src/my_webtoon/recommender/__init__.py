"""콘텐츠 기반 추천기. 읽는 순서:

features.py   작품 → TF-IDF 벡터 (제목·줄거리 글자 n-gram + 장르·태그·작가 토큰)
profile.py    내 평가 → 취향 벡터 (평가 가중합)
scoring.py    유사도 × 무료/회차/평점 보정, '유료 위주' 작품은 맨 아래로
engine.py     위를 엮어 추천 목록 + "왜 추천됐는지" 설명
"""

from .engine import Engine, Recommendation
from .scoring import ScoringOptions

__all__ = ["Engine", "Recommendation", "ScoringOptions"]
