"""数据库驱动连接串转换。"""

from sqlalchemy.engine import make_url


def postgres_dsn(url: str) -> str:
    """转换为 psycopg 连接串，保留连接所需密码；返回值不可写入日志。"""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
