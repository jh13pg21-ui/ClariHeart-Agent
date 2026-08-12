"""MySQL 集成测试的 destructive 操作护栏。"""
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url


SAFE_DATABASE_PREFIX = "mindbridge_test_"


def validate_destructive_database_target(
    database_url: str,
    selected_database: str | None,
    confirmation: str | None,
) -> str:
    configured_database = make_url(database_url).database
    if confirmation != "1":
        raise RuntimeError(
            "拒绝 destructive MySQL 测试：必须显式设置 "
            "MINDBRIDGE_ALLOW_DESTRUCTIVE_DB_TESTS=1"
        )
    if (
        not configured_database
        or not selected_database
        or not selected_database.startswith(SAFE_DATABASE_PREFIX)
    ):
        raise RuntimeError(
            "拒绝 destructive MySQL 测试：数据库名必须以 "
            f"{SAFE_DATABASE_PREFIX} 开头"
        )
    if configured_database != selected_database:
        raise RuntimeError(
            "拒绝 destructive MySQL 测试：URL 数据库名与 "
            "SELECT DATABASE() 返回值不一致"
        )
    return selected_database


def require_safe_destructive_database(
    engine,
    database_url: str,
    confirmation: str | None,
) -> str:
    with engine.connect() as connection:
        selected_database = connection.execute(
            text("SELECT DATABASE()")
        ).scalar_one_or_none()
    return validate_destructive_database_target(
        database_url,
        selected_database,
        confirmation,
    )


def reset_mysql_schema(
    engine,
    database_url: str,
    confirmation: str | None,
) -> None:
    require_safe_destructive_database(engine, database_url, confirmation)
    with engine.begin() as connection:
        connection.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        try:
            table_names = inspect(connection).get_table_names()
            quote_identifier = connection.dialect.identifier_preparer.quote
            for table_name in table_names:
                connection.execute(
                    text(f"DROP TABLE {quote_identifier(table_name)}")
                )
        finally:
            connection.execute(text("SET FOREIGN_KEY_CHECKS=1"))
