"""向量空间标识：模型名、部署修订号、维度与向量输入格式共同决定兼容性。"""

import hashlib
import json

# 改变标题/正文的拼接、清洗或切分策略时须提升输入版本并重建已有文档。
INPUT_VERSION = "heading-content-v1"


def embedding_version(settings) -> str:
    """同名模型替换也必须更新 embedding_revision；不根据维度猜测兼容。"""
    identity = [
        settings.embedding_model,
        settings.embedding_revision,
        settings.knowledge_embedding_dimensions,
        INPUT_VERSION,
    ]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
