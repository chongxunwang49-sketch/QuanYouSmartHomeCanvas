"""
全局配置：基于 pydantic-settings 读取项目根目录 .env。

设计要点：
1. 所有密钥只从环境变量/.env 读取，代码中绝不硬编码（对应 AC-13 审计要求）。
2. 模型提供方全部走配置，运行时可切换（对应 V2.0 文档「模型可插拔」）。
3. 默认值即本机实测可用的配置，克隆仓库后配好 .env 即可运行。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> core -> app -> backend -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """项目全局配置。字段名即环境变量名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── 基础 ──────────────────────────────────────────────
    APP_NAME: str = "全友·智绘家 QuanYou Smart HomeCanvas"
    APP_ENV: Literal["dev", "prod"] = "dev"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"
    PROJECT_ROOT: Path = PROJECT_ROOT

    # ── 本地部署约束（附录 B.6 / AC-37）─────────────────────
    # 决策：本项目全部演示在本地完成，不做任何公网暴露。
    # 因此监听地址一律绑回环，CORS 走白名单——绝不使用 0.0.0.0 或 ["*"]。
    # 这与 ADR-12 同理：安全约束必须在**资源暴露的那一层**强制。
    BIND_HOST: str = "127.0.0.1"
    BACKEND_PORT: int = 8000
    CORS_ALLOW_ORIGINS: list[str] = Field(
        default_factory=lambda: [
            "http://localhost",
            "http://127.0.0.1",
            "http://localhost:5173",   # Vite 开发服务器
            "http://127.0.0.1:5173",
            "http://localhost:80",
        ],
        description="CORS 白名单。禁止填 ['*']——本项目存用户上传的户型图。",
    )
    # 显式开关：若某天需要局域网内用手机看效果，改成 true 并自行承担风险。
    ALLOW_LAN_ACCESS: bool = False

    # ── DeepSeek（多模态主模型）───────────────────────────
    # 实测：/v1 与 /anthropic 两个端点都能直接读图，本客户端走 /v1（OpenAI 兼容）
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_MODEL: str = "deepseek-flash"
    DEEPSEEK_VISION_MODEL: str = "deepseek-flash"

    # ── Ollama（本地兜底，宿主机原生运行）─────────────────
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_VISION_MODEL: str = "minicpm-v4.6:latest"
    OLLAMA_TEXT_MODEL: str = "qwen2.5:3b"
    OLLAMA_EMBEDDING_MODEL: str = "quentinz/bge-large-zh-v1.5:latest"
    OLLAMA_EMBEDDING_DIM: int = 1024

    # ── 降级链 ────────────────────────────────────────────
    # 多模态：deepseek -> ollama_vision -> 失败
    # 文本  ：deepseek -> ollama_text   -> 失败
    ENABLE_DEGRADATION: bool = True
    LLM_PROVIDER: Literal["deepseek", "ollama"] = "deepseek"

    # ── LLM 调用参数 ──────────────────────────────────────
    # ⚠️ 实测坑：deepseek-flash 是思考模型，reasoning token 会先消耗预算。
    #    max_tokens=100 时 169 个 reasoning token 吃光全部预算，content 返回空字符串。
    #    因此下限必须给足，默认 8000。
    LLM_MAX_TOKENS: int = 8000
    LLM_TIMEOUT_SECONDS: float = 120.0
    LLM_TEMPERATURE: float = 0.0
    LLM_MAX_RETRIES: int = 2

    # ── Agent 执行控制 ────────────────────────────────────
    AGENT_TIMEOUT_SECONDS: float = 30.0
    AGENT_MAX_CONCURRENCY: int = 4

    # ── Redis ─────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_CHECKPOINT_PREFIX: str = "qy:checkpoint"
    REDIS_CACHE_PREFIX: str = "qy:cache"
    REDIS_QUOTA_PREFIX: str = "qy:quota"
    REDIS_TASK_PREFIX: str = "qy:task"
    ENABLE_REDIS_CHECKPOINTER: bool = True

    # ── PostgreSQL ────────────────────────────────────────
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "qy"
    POSTGRES_PASSWORD: str = "qy"
    POSTGRES_DB: str = "quanyou"

    # ── ChromaDB（嵌入式，非独立容器）──────────────────────
    CHROMA_PATH: Path = Path("E:/quanyou/data/chroma")
    CHROMA_COLLECTION_KNOWLEDGE: str = "quanyou_knowledge"
    CHROMA_COLLECTION_CASES: str = "quanyou_cases"

    # ── Rerank（复用已部署服务，按需启停；留空即关闭）───────
    RERANK_URL: str = ""
    RERANK_TIMEOUT_SECONDS: float = 10.0

    # ── 图像生成 ──────────────────────────────────────────
    # ⚠️ V2.0 决策：SDXL 在 4GB 显存下不可行，主路径为 SD1.5+LCM；
    #    vector 为常驻兜底，任何情况下都必须能出图。
    IMAGE_PROVIDER: Literal["vector", "local_sd15"] = "vector"
    SD15_MODEL_PATH: Path = Path("E:/quanyou/models/sd15")
    SD15_CONTROLNET_PATH: Path = Path("E:/quanyou/models/controlnet_sd15_canny")
    SD15_LCM_LORA_PATH: Path = Path("E:/quanyou/models/lcm-lora-sdv15")
    SD15_IMAGE_SIZE: int = 512          # 768 会溢出 3.5GB 可用显存
    SD15_STEPS: int = 6                 # LCM-LoRA 4-8 步
    SD15_GUIDANCE: float = 1.0          # LCM 需低 CFG
    IMAGE_SERIAL_LOCK: bool = True      # 全局串行锁，禁止并发调图（否则 OOM）

    # AI 图上的热区 —— ⚠️ 默认关闭（ADR-10）
    # ControlNet 不保证几何精确对齐，而 conditioning 边缘图与生成图 Canny 边缘图
    # 之间即使几何完全对齐，IoU 也可能只有 0.4–0.6。预设阈值会让功能整体失效。
    # 待 M5 用 20 张样本人工标注标定后再决定是否启用。默认行为：
    # 热区全部走矢量图，AI 图仅作风格示意。
    IMAGE_HOTSPOT_ENABLED: bool = False
    IMAGE_HOTSPOT_IOU_THRESHOLD: float | None = None   # 标定后填，未标定则保持 None
    IMAGE_HOTSPOT_IOU_FALLBACK: float = 0.55           # 仅当阈值标定失败时启用

    # ── 文件存储 ──────────────────────────────────────────
    UPLOAD_DIR: Path = Path("E:/quanyou/uploads")
    OUTPUT_DIR: Path = Path("E:/quanyou/outputs")
    MAX_UPLOAD_MB: int = 10

    # ── 模型下载源（实测：huggingface.co 直连不通）──────────
    HF_ENDPOINT: str = "https://hf-mirror.com"

    # ── 权限额度 ──────────────────────────────────────────
    QUOTA_USER_PARSE_PER_DAY: int = 5
    QUOTA_USER_GENERATE_PER_DAY: int = 3

    @field_validator("DEEPSEEK_API_KEY", mode="before")
    @classmethod
    def _strip(cls, v: str) -> str:
        return (v or "").strip()

    # ── 派生属性 ──────────────────────────────────────────
    @property
    def postgres_dsn(self) -> str:
        """SQLAlchemy 使用的同步 DSN。"""
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def deepseek_enabled(self) -> bool:
        """未配置 key 时自动跳过 DeepSeek，直接走本地兜底。"""
        return bool(self.DEEPSEEK_API_KEY) and self.LLM_PROVIDER == "deepseek"

    @property
    def rerank_enabled(self) -> bool:
        return bool(self.RERANK_URL.strip())

    @property
    def cors_origins(self) -> list[str]:
        """
        CORS 白名单。默认全部为 localhost / 127.0.0.1。

        `ALLOW_LAN_ACCESS=true` 时额外放行本机局域网地址——**仅在确有需要时开启**。
        无论哪种情况都不返回 `["*"]`。
        """
        origins = list(self.CORS_ALLOW_ORIGINS)
        if self.ALLOW_LAN_ACCESS:
            origins += [f"http://{self.BIND_HOST}:5173", f"http://{self.BIND_HOST}"]
        return origins

    def ensure_dirs(self) -> None:
        """确保所有输出目录存在。"""
        for d in (
            self.UPLOAD_DIR,
            self.OUTPUT_DIR,
            self.CHROMA_PATH,
            self.SD15_MODEL_PATH.parent,
        ):
            Path(d).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例配置。测试中可用 get_settings.cache_clear() 重置。"""
    return Settings()


settings = get_settings()
