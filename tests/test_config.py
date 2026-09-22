"""
配置层约束测试（对应 AC-37 与 ADR-10 / ADR-12）。

这些断言看起来琐碎，但它们守的是**不该被写错的那几行**：
端口绑定、CORS 白名单、需要标定后才能开启的开关。
这类配置一旦写错，后果是静默的——服务照常启动，只是在对的地方裸奔。
"""

from __future__ import annotations

from backend.app.core.config import Settings, settings


class TestLocalOnlyDeployment:
    """全部演示在本地 —— 不做任何公网暴露（附录 B.6 / AC-37）。"""

    def test_bind_host_is_loopback(self):
        assert settings.BIND_HOST in ("127.0.0.1", "localhost"), \
            "服务必须绑回环地址，不能是 0.0.0.0"

    def test_cors_has_no_wildcard(self):
        origins = settings.cors_origins
        assert "*" not in origins, \
            "CORS 不允许通配符 —— 本项目存用户上传的户型图"
        assert origins, "CORS 白名单不应为空"

    def test_cors_only_local_origins(self):
        for origin in settings.cors_origins:
            assert "localhost" in origin or "127.0.0.1" in origin, \
                f"白名单含非本机来源: {origin}"

    def test_lan_access_off_by_default(self):
        assert settings.ALLOW_LAN_ACCESS is False, \
            "局域网访问默认必须关闭"

    def test_lan_access_still_no_wildcard(self):
        """即使显式开启局域网访问，也不能退化成通配符。"""
        s = Settings(ALLOW_LAN_ACCESS=True, CORS_ALLOW_ORIGINS=["http://localhost"])
        assert "*" not in s.cors_origins


class TestUncalibratedFeaturesAreOff:
    """
    未经过实测标定的能力默认关闭（ADR-10）。

    IoU 阈值没有任何实测依据，预设它等于让功能整体失效——
    所以默认关闭，且阈值保持 None，直到 M5 标定完成。
    """

    def test_ai_image_hotspot_disabled_by_default(self):
        assert settings.IMAGE_HOTSPOT_ENABLED is False

    def test_iou_threshold_unset_until_calibrated(self):
        assert settings.IMAGE_HOTSPOT_IOU_THRESHOLD is None, \
            "阈值必须在 M5 实测标定后填入，不得预设"


class TestMeasuredConstraints:
    """把第零章的实测结论固化进配置，防止被无意改回。"""

    def test_max_tokens_high_enough_for_thinking_model(self):
        # 实测：deepseek-flash 单次 179 token 里 169 个是 reasoning，
        # max_tokens 给小了会返回空字符串而 HTTP 仍为 200。
        assert settings.LLM_MAX_TOKENS >= 2000

    def test_storage_on_e_drive(self):
        # D 盘仅 29GB 可用且已用 89%，模型与数据统一放 E 盘
        assert str(settings.CHROMA_PATH).startswith("E:"), \
            "Chroma 持久化目录应在 E 盘"

    def test_hf_endpoint_is_mirror(self):
        # huggingface.co 本机直连超时（12s, code 000）
        assert "hf-mirror" in settings.HF_ENDPOINT or "modelscope" in settings.HF_ENDPOINT, \
            "必须走镜像源，直连 huggingface.co 不通"

    def test_sd15_image_size_within_vram_budget(self):
        # 实测可用显存 3.22GB；768 会溢出
        assert settings.SD15_IMAGE_SIZE <= 512

    def test_no_effect_image_from_cloud(self):
        # 无 DashScope key；且云端出图与隐私条款冲突
        assert settings.IMAGE_PROVIDER in ("vector", "local_sd15"), \
            "图像生成只允许本地实现，不存在云端兜底"


class TestSd15TunedDefaults:
    """
    把 M0 出图基准调出来的参数固化，防止被无意改回。

    完整依据见 ADR-01「V2.3 最终结论」与 scripts/bench_image.py 的实测报告。
    """

    def test_lcm_disabled_by_default(self):
        # 实测：LCM-LoRA 跨种子极不稳定（seed=7 出 3D 图、seed=42 出噪声图）。
        # 演示不能赌运气，故默认关闭，走标准采样。
        assert settings.SD15_USE_LCM is False

    def test_steps_adequate_for_standard_sampler(self):
        # 标准采样下 6-10 步不够；实测 20 步 / CFG 7.0 稳定
        assert settings.SD15_STEPS >= 20

    def test_guidance_high_enough(self):
        # LCM 时代用的 1.0-2.5 在标准采样下会让画面发散
        assert settings.SD15_GUIDANCE >= 5.0

    def test_offload_enabled(self):
        # 实测：模型级 offload 既省 3GB 显存又更快（6.58s → 5.33s）
        assert settings.SD15_OFFLOAD_MODE in ("model", "sequential"), \
            "全量上卡会让显存撑满到 4.0GB、空闲 0.00GB"

    def test_controlnet_scale_not_full(self):
        # 1.0 会强制逐像素复刻 conditioning 图，输出退化成带纹理的平面图
        assert settings.SD15_CONTROLNET_SCALE <= 0.7
