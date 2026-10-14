"""工具注册处：只有这里汇总 TOOLS。"""

from app.agent.tools.balance import aggregate_results, query_assets
from app.agent.tools.knowledge import search_docs
from app.agent.tools.market import get_chain_info, get_token_price

TOOLS = [
    query_assets,
    get_token_price,
    get_chain_info,
    aggregate_results,
    search_docs,
]
