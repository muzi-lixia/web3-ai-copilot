"""检索前的查询改写：把多轮里的追问补成能独立检索的句子。

没有历史、超时或调用失败时一律退回原问题，改写失败不影响检索。
"""

import asyncio
import time

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field

from app.core.logging import get_logger

LOGGER = get_logger(__name__)

# 补指代与省略掉的主语、对象。
INSTRUCTION = """请将下面给定的「最新问题」改写成一句脱离当前对话也能独立检索的完整问题。

改写规则：
- 补全指代（例如「它」「这个」「上面说的」「那」「那它」具体指什么），以及省略掉的主语或对象。
- 只能使用对话中明确出现过的信息进行补全，不得引入对话中未出现的实体。
- 不改变提问意图，不添加新主题，不回答问题。
- 「要不要改写」不看读起来通不通：先看「最新问题」里有没有指代词（它、这个、那个、上面
  说的…）或省略掉的对象（只说动作、不说对象，如「那最多能加几个」）。占了一条就必须补成
  对话里出现过的具体名字，不能原样返回；两条都不占才原样返回。

对话内容：
{history}

最新问题：
{query}

输出要求：
- 若无需改写，则直接原样输出「最新问题」；「无需改写」按上面那条客观判定，不是凭读起来是否通顺。
- 若因缺少必要信息而无法补全，则在保持不引入新信息的前提下，输出尽量完整、可独立检索的版本。"""

# 改写必须能被超时掐断：它占的是整轮生成的时间预算。
TIMEOUT_SECONDS = 8.0

# 取多少条历史消息做指代消解；够消解最近一次追问即可，多了只是白占预算。
HISTORY_MESSAGES = 6

# 单条历史只留这么多字：改写要知道聊过什么，不需要完整原文。
HISTORY_CHARS = 200

# 改写结果的长度上限；超过这个长度就是在解释，不是在改写。
RESULT_CHARS = 200

# 首次尝试 + 一次重试。再多只是把整轮预算耗在改写上，不如直接退回原问题。
ATTEMPTS = 2

# 问题里出现这些词，就认为它还不能独立检索；只有这类问题才值得再调一次模型。
PRONOUNS = ("它", "他", "她", "这", "那", "此", "上面", "上述", "前者", "后者", "刚才")


class RewriteResult(BaseModel):
    """改写结果：供检索使用的句子，以及它是否与原问题不同。"""

    rewritten: bool
    query: str


class _Draft(BaseModel):
    """模型这一步的输出：只有改写后的句子。

    `rewritten` 不在这里 —— 改没改由代码比较原句得出，不交给模型自评。
    """

    query: str = Field(
        min_length=1,
        max_length=RESULT_CHARS,
        description="脱离对话也能独立检索的一句话，只补指代与省略信息、不扩写",
    )


def needs_rewrite(query: str) -> bool:
    """问题里是否还有指代词。

    「其他」「其它」里的「他」不是指代，先去掉再判。
    """
    text = query.replace("其他", "").replace("其它", "")
    return any(word in text for word in PRONOUNS)


def fallback(query: str) -> RewriteResult:
    """退回原问题：检索用用户原话，并记为未改写。"""
    return RewriteResult(rewritten=False, query=query)


def recent_dialogue(messages, limit=HISTORY_MESSAGES) -> str:
    """把本轮之前最近几条用户与助手的消息拼成对话文本。

    本轮不计入：本轮的用户消息就是待改写的那句。工具调用与工具结果也不计入。
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


def _record(model, started: float, *, status: str, reason: str | None, rewritten: bool) -> None:
    """记一次改写的结果：拿到模型结果还是退回原话、退回的原因、以及有没有改动。

    只在日志里区分：返回值上「退回原话」与「模型判定无需改写」都是 `rewritten=False`，
    不记这一笔就分不出改写是生效了还是被放弃了。
    """
    LOGGER.info(
        "rewrite.completed",
        extra={
            "status": status,
            "error_code": reason,
            "rewritten": rewritten,
            "model": getattr(model, "model_name", "") or getattr(model, "model", ""),
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
        },
    )


async def rewrite(model, query, history, timeout=TIMEOUT_SECONDS) -> RewriteResult:
    """改写查询；没有历史、问题里没有指代词、或任何一步失败，都返回原问题并记为未改写。

    model 用本轮 Agent 的实例，不另建连接。
    """
    if not history or not needs_rewrite(query):
        return fallback(query)
    # 用替换而不是格式化：用户问题里的花括号不该被当成占位符。
    prompt = INSTRUCTION.replace("{history}", history).replace("{query}", query)
    # 关掉流式：这次调用不属于本轮回答，一旦被上游的流式处理器升格成流式，
    # 它的分片会混进回答正文。
    quiet = model.model_copy(update={"disable_streaming": True})
    structured = quiet.with_structured_output(_Draft)
    original = query.strip()
    started = time.monotonic()
    reason = None
    try:
        # 重试与首次尝试共用同一份超时预算：不能因为重试把整轮时间拖穿。
        async with asyncio.timeout(timeout):
            for _ in range(ATTEMPTS):
                try:
                    draft = await structured.ainvoke(prompt)
                except Exception:
                    reason = "structured_failed"
                    continue
                if draft is None:
                    reason = "empty_draft"
                    continue
                text = draft.query.strip()
                if text:
                    changed = text != original
                    _record(model, started, status="success", reason=None, rewritten=changed)
                    return RewriteResult(query=text, rewritten=changed)
                reason = "empty_draft"
    except TimeoutError:
        reason = "timeout"
    except Exception:
        reason = "unexpected"
    _record(model, started, status="failed", reason=reason, rewritten=False)
    return fallback(query)
