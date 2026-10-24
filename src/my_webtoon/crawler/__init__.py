"""네이버 시리즈 수집기.

client.py   정중한 HTTP 클라이언트 (요청 간격, robots.txt, 재시도)
parser.py   HTML → dict (순수 함수)
seeds.py    BFS 출발점 페이지 목록
runner.py   시드 → 큐 → 상세 페이지 → 링크 확장 루프 + 백그라운드 스레드
"""
