"""拦截由历史 SQL 初始化脚本所提供的公开默认管理员密码哈希."""

from sqlalchemy import Engine, select

from app.models.user import User

LEGACY_ADMIN_HASH = (
    "scrypt:32768:8:1$ULeZhKdUrZi46VVx$"
    "30e562fbf0a745b562fd7b2e037ba1e634cb13f82cd6f9d27763863136b16dc20e21802e4"
    "4af40eaa5cee287c6bbd9e474d211b157b85dba3bb85bde49e4b2f1"
)


def check_legacy_admin(database_engine: Engine) -> None:
    """当历史默认管理员仍存在且未修改密码时拒绝服务启动."""
    with database_engine.connect() as conn:
        found = conn.scalar(
            select(User.id)
            .where(User.role == "admin", User.password_hash == LEGACY_ADMIN_HASH)
            .limit(1),
        )
    if found is not None:
        message = (
            "Historical default administrator credentials are disabled; "
            "run python -m entry.reset_admin --username <administrator>"
        )
        raise RuntimeError(message)
