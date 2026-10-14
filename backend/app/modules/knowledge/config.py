"""HTTP 和 worker 共用的知识库数据配置，默认值只维护一份。"""

from pydantic import BaseModel, Field


class KnowledgeDataSettings(BaseModel):
    knowledge_storage_dir: str = "var/knowledge"
    """上传原件的落盘目录，相对后端工作目录。库里只存元数据，不存文件本体。"""

    embedding_revision: str = Field(default="1", min_length=1)
    embedding_model: str = "bge-m3:latest"
    """生成检索向量的模型。维度必须与 knowledge_embedding_dimensions 一致。"""

    embedding_base_url: str = "http://127.0.0.1:11434"
    """生成向量的服务地址，默认本机 Ollama。走本机地址，不受环境代理影响。"""

    knowledge_embedding_dimensions: int = Field(default=1024, gt=0)
    """向量列维度。改这个值必须同时重建 chunks.embedding 列，否则写入会被拒绝。"""


class KnowledgeSettings(KnowledgeDataSettings):
    """基础 HTTP 服务的上传限制与在线检索配置。"""

    knowledge_max_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    """单个上传文件的体积上限，超出直接拒绝。"""

    embedding_query_timeout_seconds: float = Field(default=15, gt=0)
    embedding_query_concurrency: int = Field(default=2, ge=1)
    embedding_query_base_url: str = ""
    knowledge_search_min_score: float = Field(default=0.55, ge=-1, le=1)
