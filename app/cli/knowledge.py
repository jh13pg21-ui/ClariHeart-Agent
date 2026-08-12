from __future__ import annotations

import argparse

from app.core.bootstrap import submit_builtin_knowledge
from app.core.config import get_settings
from app.core.database import SessionLocal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MindBridge 内置知识摄取")
    parser.add_argument("command", choices=["sync-builtins"])
    args = parser.parse_args(argv)
    if args.command == "sync-builtins":
        db = SessionLocal()
        try:
            results = submit_builtin_knowledge(db, settings=get_settings())
        finally:
            db.close()
        created = sum(int(item.created) for item in results)
        print(f"已扫描 {len(results)} 个文件，新建并派发 {created} 个摄取任务。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
