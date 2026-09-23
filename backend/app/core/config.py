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
    # ⚠️ 以下默认值来自 2026-09-22 的画质消融实验，**不是照抄教程**。
    #    完整结论见 ADR-01「V2.3 任务范围与采样器修正」。核心三条：
    #    1. LCM-LoRA 默认关闭——它把出图从 13s 降到 7s，但**跨种子极不稳定**
    #       （同一提示词，seed=7 出 3D 等轴测、seed=42 退化成平面噪声图）。
    #       演示不能靠运气。标准采样 13.4s，仍在 25s 目标内。
    #    2. CFG 必须是 7.0 而不是 LCM 时代的 1.0/2.5——低 CFG 下画面会发散。
    #    3. ControlNet scale 0.5，1.0 会强制逐像素复刻 conditioning 图。
    SD15_USE_LCM: bool = False
    SD15_STEPS: int = 20                # LCM 下的 6-10 步在标准采样下不够
    SD15_GUIDANCE: float = 7.0          # 标准 SD1.5 的惯用值
    SD15_CONTROLNET_SCALE: float = 0.5
    IMAGE_SERIAL_LOCK: bool = True      # 全局串行锁，禁止并发调图（否则 OOM）

    # ⚠️ 实测（2026-09-22 bench_image.py）：offload 模式是显存与速度的**双赢**
    #     none（全量上卡）: 加载后占用 3.63GB，出图峰值 4.00GB / 空闲 0.00GB，6.58s
    #     model（模型级offload）: 加载后 0.82GB，出图峰值 0.91GB / 空闲 3.09GB，5.33s
    # 全量上卡时显存压力 100%，分配器颠簸反而更慢；offload 后压力低、跑得更顺。
    # 这推翻了 ADR-01 原假设「offload 会把单图拖到分钟级」——对 SD1.5@512 几乎零代价。
    SD15_OFFLOAD_MODE: Literal["model", "sequential", "none"] = "model"

    # AI 图上的热区 —— 默认关闭（ADR-10）
    #
    # 需求文档 2.2.6 曾担心「预设的 0.70 阈值会让所有 AI 图都不显示热区」，
    # 但当时那只是推测。2026-09-23 用 scripts/calibrate_hotspot_iou.py
    # 在 E:/quanyou/outputs 上实测，**推测被证实**：
    #
    #     样本                           IoU
    #     A_thickEdge_g1.0_s6           0.667   ← 消融实验里"人工判定最好"的那张
    #     B_thinLine_g1.0_s6            0.216
    #     B_thinLine_g1.5_s8            0.216
    #     B_thinLine_g2.0_s8            0.216
    #     预设阈值 0.70 → 达标 0/4
    #
    # ⚠️ 这个结果的**成色要说清楚**：n=4，其中 3 张是消融实验里故意做坏的
    # 对照组（thinEdge 分支本身就是失败的设计），它们的低分不代表正常出图水平。
    # 所以它能证明的只有一件事 —— **0.70 定高了，高到连最好的那张都过不了**。
    # 它**不能**用来定一个新阈值（标定规程要求 ≥ 20 张正常样本）。
    #
    # 因此在拿到足够样本之前，保持关闭：热区全部走矢量图（那里的热区是
    # 从几何量出来的，`precision` 真的是 exact），AI 图仅作风格示意。
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
