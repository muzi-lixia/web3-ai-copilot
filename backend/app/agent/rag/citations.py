"""引用编号：给片段标上序号，编号就是模型上下文里的 [1]、[2]。

前端的片段列表与模型收到的原文块共用这一份编号，两边才对得上：模型写「[2]」时，
用户看到的就是卡片上的第 2 条。
"""


def build(hits) -> list[dict]:
    """按现有顺序编号，返回带 index 的片段列表。"""
    return [dict(hit, index=position) for position, hit in enumerate(hits, 1)]
