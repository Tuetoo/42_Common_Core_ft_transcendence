import argparse
import asyncio
import sys
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession
from user_state_sync import commit_with_state, state_of
from domains.admin.repository import AdminRepository
from domains.auth.repository import UserRepository
from domains.auth.schema import normalize_email
from shared.enums import SystemRole


class RoleChangeRefused(Exception):
    pass


@dataclass(frozen=True)
class RoleChangeResult:
    email: str
    user_id: str
    old_role: str
    new_role: str
    changed: bool
    dry_run: bool

    def describe(self) -> str:
        if not self.changed:
            return (
                f"{self.email} ({self.user_id}) already has role "
                f"'{self.new_role}'; nothing to do.")
        prefix = "[dry run] would change" if self.dry_run else "Changed"
        return (
            f"{prefix} {self.email} ({self.user_id}): "
            f"'{self.old_role}' -> '{self.new_role}'.")


async def set_system_role(
        session: AsyncSession, email: str, role: SystemRole, *,
        dry_run: bool = False) -> RoleChangeResult:
    users = UserRepository(session)
    found = await users.get_by_email(normalize_email(email))
    admins = AdminRepository(session)
    await admins.lock_active_admin_ids()
    user = await users.lock(found.id) if found is not None else None
    if user is None:
        raise RoleChangeRefused(f"No account with email '{email}'.")
    if user.deleted_at is not None:
        raise RoleChangeRefused(f"The account '{email}' has been deleted.")
    old_role = user.system_role
    result = RoleChangeResult(
        email=user.email, user_id=str(user.id), old_role=old_role,
        new_role=str(role), changed=old_role != str(role), dry_run=dry_run)
    if not result.changed:
        return result
    if old_role == SystemRole.ADMIN:
        admin_ids = await admins.lock_active_admin_ids()
        if user.id in admin_ids and len(admin_ids) <= 1:
            raise RoleChangeRefused(
                f"'{email}' is the last active admin; promote another "
                "admin first.")
    if dry_run:
        return result
    previous = state_of(user)
    user.system_role = str(role)
    await commit_with_state(session, user, previous)
    return result


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.set_system_role", description=(
            "Set a user's system role (admin, moderator, user, guest)."))
    parser.add_argument("--email", required=True, help="the account's email")
    parser.add_argument(
        "--role", required=True, choices=[role.value for role in SystemRole])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the change without writing it")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    from database import SessionLocal, engine
    from shared.redis import close_redis_connections

    try:
        async with SessionLocal() as session:
            try:
                result = await set_system_role(
                    session, args.email, SystemRole(args.role),
                    dry_run=args.dry_run)
            except RoleChangeRefused as exc:
                print(f"Refused: {exc}", file=sys.stderr)
                return 1
        print(result.describe())
        return 0
    finally:
        await engine.dispose()
        await close_redis_connections()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
