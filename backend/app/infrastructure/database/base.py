"""各领域 ORM 共享的声明基类；实体定义属于对应业务模块。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """共享 ORM 元数据入口。迁移加载领域模型后，通过 metadata 对照当前表结构。"""

    pass
