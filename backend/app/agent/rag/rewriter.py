"""检索前的查询改写：把多轮里的追问补成能独立检索的句子。

改写是「有时更好」的优化，不是取数链路的一环：模型不可用、超时或返回空，
一律退回原问题，绝不因为改写失败而让检索失败。
"""

import asyncio

from langchain_core.messages import BaseMessage

# 只补指代、不扩写：扩写会往问题里加原本没有的主题，把检索带偏。
INSTRUCTION = """把「最新问题」改写成脱离上面的对话也能独立检索的一句话。

规则：
- 只补全指代的含义（「它」「这个」指的是什么），不改变提问意图，不添加新主题。
- 最新问题本身已经完整、可以独立检索时，原样返回。
- 只输出改写后的那一句，不要解释、不要加引号、不要分点。

对话：
{history}

最新问题：{query}"""

# 改写必须能被超时掐断：它占的是整轮生成的时间预算。
TIMEOUT_SECONDS = 8.0

# 取多少条历史消息做指代消解；够消解最近一次追问即可，多了只是白占预算。
HISTORY_MESSAGES = 6

# 单条历史只留这么多字：改写要知道聊过什么，不需要完整原文。
HISTORY_CHARS = 200

# 改写结果的长度上限；超过这个长度就是在解释，不是在改写。
RESULT_CHARS = 200


def recent_dialogue(messages, limit=HISTORY_MESSAGES) -> str:
    """把本轮之前最近的几条用户与助手消息拼成对话文本，供改写消解指代。

    必须排除本轮：本轮的用户消息就是待改写的那句，把它当成历史会让「没有历史就
    原样返回」的短路失效，白白多调一次模型。
    只取人写的那两种消息：带工具调用的助手消息正文为空，工具消息是检索到的原文，
    两者拼进去都只是白占预算。
    """
    items = list(messages or [])
    # 最后一个用户消息及其之后都属于本轮，不计入历史。
    cut = len(items)
    for index in range(len(items) - 1, -1, -1):
        if getattr(items[index], "type", None) == "human":
            cut = index
            break
    lines = []
    for message in items[:cut][-limit:]:
        if not isinstance(message, BaseMessage) or message.type not in ("human", "ai"):
            continue
        text = str(message.text).strip()
        if text:
            lines.append(f"{'用户' if message.type == 'human' else '助手'}：{text[:HISTORY_CHARS]}")
    return "\n".join(lines)


async def rewrite(model, query, history, timeout=TIMEOUT_SECONDS) -> str:
    """改写查询；没有历史或任何一步失败都返回原问题。

    model 传本轮 Agent 用过的那个实例，不另建连接。
    """
    if not history:
        return query
    # 用替换而不是格式化：用户问题里的花括号不该被当成占位符。
    prompt = INSTRUCTION.replace("{history}", history).replace("{query}", query)
    try:
        # 关掉流式：这次调用不属于本轮回答，一旦被上游的流式处理器升格成流式，
        # 它的分片会混进回答正文。
        quiet = model.model_copy(update={"disable_streaming": True})
        async with asyncio.timeout(timeout):
            response = await quiet.ainvoke(prompt)
    except Exception:
        return query
    text = str(getattr(response, "text", "")).strip().strip("\"'“”").strip()
    # 空回答、或改写成一段解释，都不如原问题可靠。
    return text if 0 < len(text) <= RESULT_CHARS else query
