"""PostgreSQL pgvector 的参数序列化。"""


def to_literal(vector: list[float]) -> str:
    """向量转成 pgvector 的字面量文本，写入与查询都由 SQL 侧转换类型。"""
    return "[" + ",".join(str(value) for value in vector) + "]"
