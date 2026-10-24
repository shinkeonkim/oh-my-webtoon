"""명령줄 진입점.

my-webtoon serve                 웹 서버 (기본 0.0.0.0:8000)
my-webtoon crawl --limit 500     크롤링 한 번 (로컬에서 데이터를 미리 채울 때)
my-webtoon import 2140867        작품 하나만 가져오기
my-webtoon user list             사용자 목록
my-webtoon user add alice        사용자 추가 (비밀번호는 프롬프트로)
my-webtoon user set-password singun11              비밀번호 재설정 (잊었을 때 kubectl exec -it 로)
my-webtoon user demo test --copy-from singun11     시연용 계정 지정 (--off 로 해제, demo.py)
my-webtoon backup now           지금 백업하고 바로 검사 (BACKUP_DIR 필요)
my-webtoon backup verify FILE    백업 파일 검사 (깨졌는지, 행 수) — 복구 연습 때 (docs/04)
my-webtoon eval singun11         추천 품질 측정 (설정별 leave-one-out 비교, recommender/evaluate.py)
"""

from __future__ import annotations

import argparse
import logging

from . import db
from .config import load_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="my-webtoon")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="웹 서버 실행")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    crawl = sub.add_parser("crawl", help="크롤링 한 번 실행")
    crawl.add_argument("--limit", type=int, default=None, help="상세 페이지 최대 개수")
    crawl.add_argument("--seeds", action="store_true", help="시드 페이지를 강제로 다시 수집")

    imp = sub.add_parser("import", help="작품 하나 가져오기")
    imp.add_argument("product_no", type=int)

    user = sub.add_parser("user", help="사용자 관리")
    user_sub = user.add_subparsers(dest="user_command", required=True)
    user_sub.add_parser("list")
    add = user_sub.add_parser("add")
    add.add_argument("username")
    add.add_argument("--admin", action="store_true")
    setpw = user_sub.add_parser("set-password")
    setpw.add_argument("username")
    demo_cmd = user_sub.add_parser("demo")
    demo_cmd.add_argument("username")
    demo_cmd.add_argument("--copy-from", help="이 사용자의 평가(메모 제외)를 기준 평가로 복사")
    demo_cmd.add_argument("--off", action="store_true", help="데모 해제")

    bk = sub.add_parser("backup", help="백업 검사")
    bk_sub = bk.add_subparsers(dest="backup_command", required=True)
    bk_sub.add_parser("now")
    bk_verify = bk_sub.add_parser("verify")
    bk_verify.add_argument("file")

    ev = sub.add_parser("eval", help="추천 품질 측정 (설정별 비교)")
    ev.add_argument("username")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = load_settings()
    with db.session(settings.database_path) as conn:
        db.init_schema(conn, settings.admin_username, _initial_admin_hash(settings))
        db.apply_genre_exclusion(conn, settings.excluded_genres)

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "my_webtoon.web.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    elif args.command == "crawl":
        from .crawler.runner import run_once

        result = run_once(
            settings.database_path,
            user_agent=settings.user_agent,
            delay_seconds=settings.crawl_delay_seconds,
            batch_size=args.limit or settings.crawl_batch_size,
            recrawl_after_days=settings.recrawl_after_days,
            force_seeds=args.seeds,
            excluded_genres=settings.excluded_genres,
        )
        print(result.summary())
    elif args.command == "import":
        from .crawler.runner import import_one

        data = import_one(
            settings.database_path,
            args.product_no,
            user_agent=settings.user_agent,
            excluded_genres=settings.excluded_genres,
        )
        print(
            f"{data['title']} — {data['free_episodes']}/{data['total_episodes']}{data['unit']} 무료"
        )
    elif args.command == "user":
        _user_command(settings, args)
    elif args.command == "backup":
        import json
        from pathlib import Path

        from . import backup

        if args.backup_command == "now":
            if not settings.backup_dir:
                raise SystemExit("BACKUP_DIR 이 설정돼 있지 않아요")
            check = backup.backup_and_verify(settings.database_path, settings.backup_dir)
        else:
            with db.session(settings.database_path) as conn:
                check = backup.verify_backup(Path(args.file), conn)
        print(json.dumps(check, ensure_ascii=False, indent=2))
        if not check["ok"]:
            raise SystemExit(1)
    elif args.command == "eval":
        from .recommender import evaluate

        with db.session(settings.database_path) as conn:
            u = db.get_user_by_name(conn, args.username)
            if u is None:
                raise SystemExit(f"없는 사용자: {args.username}")
            for result in evaluate.run_all(conn, u.id):
                print(result.row())


def _initial_admin_hash(settings) -> str | None:
    from .web.auth import hash_password

    return hash_password(settings.app_password) if settings.app_password else None


def _user_command(settings, args) -> None:
    import getpass

    from .web import auth

    with db.session(settings.database_path) as conn:
        if args.user_command == "list":
            for u, count in db.list_users(conn):
                role = "관리자" if u.is_admin else ("데모" if u.is_demo else "사용자")
                print(f"{u.id:>3} {u.username:<20} {role}  평가 {count}개")
            return
        if args.user_command == "demo":
            _demo_command(conn, args)
            return
        password = getpass.getpass("새 비밀번호: ")
        if password != getpass.getpass("한 번 더: "):
            raise SystemExit("두 비밀번호가 달라요")
        auth.validate_new_password(password)
        if args.user_command == "add":
            u = db.create_user(conn, args.username, auth.hash_password(password), args.admin)
            print(f"추가했어요: {u.username}")
        else:
            u = db.get_user_by_name(conn, args.username)
            if u is None:
                raise SystemExit(f"없는 사용자: {args.username}")
            db.set_password_hash(conn, u.id, auth.hash_password(password))
            print(f"{u.username} 의 비밀번호를 바꿨어요 (다른 세션은 끊겨요)")


def _demo_command(conn, args) -> None:
    from . import demo

    target = db.get_user_by_name(conn, args.username)
    if target is None:
        raise SystemExit(f"없는 사용자: {args.username}")
    if args.off:
        demo.unmake_demo(conn, target)
        print(f"{target.username} 데모 해제")
        return
    source = db.get_user_by_name(conn, args.copy_from) if args.copy_from else None
    if args.copy_from and source is None:
        raise SystemExit(f"없는 사용자: {args.copy_from}")
    n = demo.make_demo(conn, target, source)
    print(f"{target.username} 을(를) 데모로 지정 — 기준 평가 {n}개, 매일 밤 이 상태로 돌아가요")


if __name__ == "__main__":
    main()
