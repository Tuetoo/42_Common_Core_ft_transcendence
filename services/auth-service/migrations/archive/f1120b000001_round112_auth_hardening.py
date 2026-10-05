from collections.abc import Sequence

from alembic import op

revision: str = "f1120b000001"
down_revision: str | None = "442fc97143aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE bad_emails text; bad_usernames text;
        BEGIN
          SELECT string_agg(e, ', ') INTO bad_emails FROM (
            SELECT lower(email) AS e FROM users
            GROUP BY lower(email) HAVING count(*) > 1) d;
          SELECT string_agg(u, ', ') INTO bad_usernames FROM (
            SELECT lower(username) AS u FROM users
            GROUP BY lower(username) HAVING count(*) > 1) d;
          IF bad_emails IS NOT NULL OR bad_usernames IS NOT NULL THEN
            RAISE EXCEPTION
              'Round112 (S15): case-insensitive duplicates must be resolved '
              'by hand before this migration. Emails: [%]. Usernames: [%].',
              coalesce(bad_emails, ''), coalesce(bad_usernames, '');
          END IF;
        END $$;
        """
    )
    op.execute(
        "UPDATE users SET email = lower(email) WHERE email <> lower(email)"
    )
    op.execute(
        "UPDATE organization_invitations SET email = lower(email) "
        "WHERE email <> lower(email)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower "
        "ON users (lower(email))"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_lower "
        "ON users (lower(username))"
    )

    op.execute(
        "ALTER TABLE connections ADD COLUMN IF NOT EXISTS blocked_by UUID "
        "REFERENCES users(id) ON DELETE SET NULL"
    )

    op.execute(
        "ALTER TABLE two_factor_secrets "
        "ADD COLUMN IF NOT EXISTS last_used_step BIGINT"
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'user_sessions'
              AND column_name = 'refresh_token'
          ) THEN
            UPDATE user_sessions
              SET refresh_token =
                encode(sha256(convert_to(refresh_token, 'UTF8')), 'hex');
            ALTER TABLE user_sessions
              RENAME COLUMN refresh_token TO refresh_token_hash;
          END IF;
        END $$;
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_user_sessions_refresh_token_hash "
        "ON user_sessions (refresh_token_hash)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_user_sessions_refresh_token_hash")
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'user_sessions'
              AND column_name = 'refresh_token_hash'
          ) THEN
            UPDATE user_sessions SET revoked_at = now() WHERE revoked_at IS NULL;
            ALTER TABLE user_sessions
              RENAME COLUMN refresh_token_hash TO refresh_token;
          END IF;
        END $$;
        """  # noqa: E501
    )
    op.execute(
        "ALTER TABLE two_factor_secrets DROP COLUMN IF EXISTS last_used_step"
    )
    op.execute("ALTER TABLE connections DROP COLUMN IF EXISTS blocked_by")
    op.execute("DROP INDEX IF EXISTS uq_users_username_lower")
    op.execute("DROP INDEX IF EXISTS uq_users_email_lower")
