"""集中读取 HEALTH_ 环境变量；配置只在 Settings 创建时校验。"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
from typing import Literal


# 应用配置入口；推理模型与嵌入模型分开配置，后者必须匹配已发布索引。
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HEALTH_")

    # Ollama 推理地址、模型及单次调用的时间和上下文预算。
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3.8:27b"
    timeout: float = Field(default=600, gt=0)
    num_ctx: int = Field(default=32768, ge=2048)
    # 最终分析使用 xhigh；规划和重排单独使用 medium。
    think: bool | Literal["low", "medium", "xhigh"] = "xhigh"
    plan_think: bool | Literal["low", "medium", "xhigh"] = "medium"
    rerank_think: bool | Literal["low", "medium", "xhigh"] = "medium"
    # 每阶段的生成额度都包含思考和最终 JSON；xhigh 不能沿用非思考的小额度。
    plan_num_predict: int = Field(default=16384, ge=256)
    rerank_num_predict: int = Field(default=4096, ge=128)
    num_predict: int = Field(default=16384, ge=256)
    # 数据目录同时存放业务库、队列库、文件锁和本地向量集合。
    data_dir: Path = Path(__file__).resolve().parents[2] / "data"
    max_attempts: int = Field(default=3, ge=1, le=5)
    retry_delay: int = Field(default=2, ge=1)
    embedding_model: str = "qwen3-embedding:0.6b"
    # 分块按字符计数；重叠长度是目标值，不保证每块都恰好重叠该长度。
    chunk_size: int = Field(default=800, ge=100)
    chunk_overlap: int = Field(default=120, ge=0)
    # 默认只查已核对摘要；研究模式仅查单独发布的研究描述集合。
    review_policy: Literal["reviewed_only", "research_all"] = "reviewed_only"
    evidence_family: Literal["reviewed_summary", "medlineplus_summary"] = (
        "reviewed_summary"
    )
    rrf_constant: int = Field(default=60, ge=1)
    recall_limit: int = Field(default=30, ge=1, le=100)
    candidate_limit: int = Field(default=12, ge=1, le=100)
    topic_fragment_limit: int = Field(default=2, ge=1, le=3)
    reranker: Literal["name_llm", "body_llm", "cross_encoder", "none"] = "name_llm"
    rerank_batch_size: int = Field(default=3, ge=1, le=12)
    cross_encoder_path: Path = (
        Path(__file__).resolve().parents[2] / "data/models/mmarco"
    )
    cross_encoder_revision: str = ""
    cross_encoder_min_score: float = 0
    context_neighbors: int = Field(default=0, ge=0, le=1)

    @model_validator(mode="after")
    def check_chunks(self):
        """在配置入口检查分块参数之间的关系，返回已验证配置。"""
        if self.reranker == "cross_encoder" and not self.cross_encoder_revision:
            raise ValueError("Cross-Encoder 必须指定已下载模型的固定 revision")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        return self


# 各模块共用同一份配置；修改环境变量后应重新启动相关进程。
settings = Settings()
