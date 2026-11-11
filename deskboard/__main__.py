"""Command line entry point: ``python3 -m deskboard``."""
from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deskboard",
        description="A fast, native Tumblr dashboard for Linux.",
    )
    parser.add_argument("--demo", action="store_true", help="run against bundled sample data")
    parser.add_argument("--doctor", action="store_true", help="check installation and connectivity")
    parser.add_argument("--cookies", metavar="PATH", help="use a specific cookies.json store")
    parser.add_argument("--import-cookies", metavar="FILE",
                        help="import a cookies.txt / Cookie header export and exit")
    parser.add_argument("--feed", metavar="KIND[:VALUE]",
                        help="open a feed, e.g. dashboard, likes, blog:staff, tagged:art")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        from .config import APP_NAME, APP_VERSION

        print(f"{APP_NAME} {APP_VERSION}")
        return 0

    if args.doctor:
        from .doctor import run_doctor

        return run_doctor()

    if args.import_cookies:
        from .api.auth import SessionManager

        manager = SessionManager()
        result = manager.import_file(args.import_cookies)
        print(result.message)
        return 0 if result.ok else 1

    from .app import run

    return run(demo=args.demo, cookies_path=args.cookies, feed=args.feed)


if __name__ == "__main__":
    sys.exit(main())
