import argparse
import asyncio
import sys
from collections.abc import Sequence
import db_bootstrap
from config import settings
from database import engine
from shared.alembic_convert import CONVERSIONS, convert, format_report


async def main_async(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.alembic_convert")
    parser.add_argument(
        "--apply", action="store_true",
        help="convert (default: preview, nothing is changed)")
    args = parser.parse_args(argv)
    db_bootstrap.register_models()
    schema = settings.POSTGRES_SCHEMA
    try:
        report = await convert(
            engine, schema, apply=args.apply,
            alembic_ini=db_bootstrap.ALEMBIC_INI)
    finally:
        await engine.dispose()
    print(format_report(report, CONVERSIONS[schema]))
    return 1 if report.status == "refused" else 0


def main() -> None:
    sys.exit(asyncio.run(main_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
