"""工具注册处：只有这里汇总 TOOLS。"""

from app.agent.tools.balance import aggregate_results, get_my_balance_overview, get_my_token_balance
from app.agent.tools.knowledge import search_docs
from app.agent.tools.market import get_chain_info, get_token_price

TOOLS = [
    get_my_balance_overview,
    get_my_token_balance,
    get_token_price,
    get_chain_info,
    aggregate_results,
    search_docs,
]
