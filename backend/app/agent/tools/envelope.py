"""卡片外壳工厂：给工具产出的结果补全前端信封。

result_id / kind / status / queriedAt 由这里统一补齐，body 由各工具自己定；
信封字段契约见 frontend/src/types/api.ts 的 ResultEnvelope。
"""

from datetime import UTC, datetime
from uuid import uuid4


def make_result(context, kind, body, *, source_desc, currency=None, sources=None, queried_at=None):
    """把一张卡片追加进 context.results，补全信封必需的字段。

    Args:
        context: 工具运行上下文，卡片写进它的 results 列表。
        kind: 结果种类，决定前端用哪个渲染分支。
        body: 卡片真正渲染的业务数据，各工具自己定。
        source_desc: 来源描述，由后端组装，不交给模型生成。
        currency: 计价单位；没有这个概念就留空。
        sources: 数据来源列表（源名 + 时间）。
        queried_at: 上游给出的查询时间；省略则用当前时间。
    """
    context.results.append(
        {
            "result_id": str(uuid4()),
            "kind": kind,
            "status": "complete",
            "queriedAt": queried_at or datetime.now(UTC).isoformat(),
            "currency": currency,
            "sources": sources or [],
            "failed": [],
            "source_desc": source_desc,
            "body": body,
        }
    )
