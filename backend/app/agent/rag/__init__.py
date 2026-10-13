"""检索增强：改写问题、重排片段、给片段编号并拼成可引用的原文块。"""

from app.agent.rag.citations import build as label_hits
from app.agent.rag.context_builder import build as build_context
from app.agent.rag.reranker import rerank
from app.agent.rag.rewriter import recent_dialogue, rewrite

__all__ = ["build_context", "label_hits", "recent_dialogue", "rerank", "rewrite"]
