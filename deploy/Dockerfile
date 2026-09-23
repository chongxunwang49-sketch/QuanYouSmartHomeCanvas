# ═══════════════════════════════════════════════════════════════════
# qy-backend —— 全友·智绘家 后端镜像
# ═══════════════════════════════════════════════════════════════════
# 拓扑依据：docs/需求/需求文档.md §3.2（第 1072 行起）
#   「3 | qy-backend | 基于 python:3.11-slim 构建 | ~600 MB | 127.0.0.1:8000:8000」
#
# ⚠️ 与需求文档的两处**有意偏离**（按对话交接.md §2 的规矩在注释里标注，不改文档）
#
#   ① 文档预算行写「~600 MB（**含 torch**，故偏大）」，本镜像**不装 torch**。
#      理由：ADR-01 已决定不接 SD1.5（对话交接.md §7.3）；torch/diffusers/cv2
#      在 services/image/local_sd15.py 里全是函数内 import，不装不影响任何其他功能；
#      容器内无 GPU 直通，装了也跑不出来。
#      完整取舍写在 backend/requirements.txt 的【C】段，那里是单一真源。
#
#   ② 需求文档 §2.3.1 的性能指标（AI 出图 P95 < 25s）在本镜像里无对象 ——
#      IMAGE_PROVIDER 固定为 vector。
#
# ⚠️ 构建期内存：本机 Docker VM 上限 7.63 GiB，实测当前已用约 6.13 GiB
#    （8 个别的项目的容器在跑）。`docker build` 会把上下文打包进 VM 并在其中
#    执行 pip —— 余量不足时**可能把构建进程 OOM 掉**。所以：
#      · 根目录的 .dockerignore 把 node_modules / dist / ui参考 全排除了；
#      · 构建前先看 `docker stats`，必要时先腾内存（见对话交接.md §4.5.6）。

FROM python:3.11-slim

# ── 环境变量 ──────────────────────────────────────────────────
# PYTHONUTF8 / PYTHONIOENCODING 在**这个项目里不是可选项**：
#   core/logger.py 的模块 docstring 记着一条实测坑 ——
#   Windows 控制台默认 GBK，loguru 输出中文/emoji 会抛 UnicodeEncodeError
#   并**中断业务流程**（不是只丢日志，是直接崩）。
#   容器基础镜像是 UTF-8 locale，本不至此；但显式钉死它，免得将来
#   镜像换成别的 base 之后以同一种方式坏掉。mcp_client.py 启动子进程时
#   也做同样的事，保持一致。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# pip 源默认走清华镜像。
# 依据：需求文档第零章 0.6 实测 —— 本机直连 pypi 未配镜像，而镜像源
# 实测 301/0.24s。做成 ARG 是为了**可覆盖**（换网络环境时不必改文件）：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple ...
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# ── 依赖层单独一层，便于缓存 ──────────────────────────────────
# requirements.txt 放在这一层的**唯一** COPY 里：只要依赖没变，
# 改业务代码不会让这层缓存失效，重建是秒级的。
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" \
        -r /app/backend/requirements.txt

# ── 代码 ──────────────────────────────────────────────────────
COPY backend/     /app/backend/
COPY mcp_servers/ /app/mcp_servers/
COPY seed_data/   /app/seed_data/
COPY skills/      /app/skills/

# 各目录的存在理由（少一个都会在运行时才炸，注意别顺手删）：
#   backend/      运行时本体
#   mcp_servers/  AC-11 还没做，但 COPY 成本极低；将来做 MCP 时
#                 不必回头发现镜像里缺文件。MCP_SERVERS_DIR =
#                 PROJECT_ROOT/"mcp_servers"，PROJECT_ROOT 在这里就是 /app。
#   seed_data/    **运行时真的会读** —— material/catalog.py 读
#                 material_catalog.json、budget/engine.py 读 pricing_demo.json。
#                 少了它，材料查询与预算两张表直接空。
#   skills/       6 份 Agent 规格文档（System Prompt 的出处）。代码不读它，
#                 但它是这个项目的「功能文件」，随镜像走，便于容器内自查。

# ── 运行 ──────────────────────────────────────────────────────
EXPOSE 8000

# 健康检查必须让容器报 healthy —— AC-16 的字面判据就是
# 「`docker compose ps` → 4 个服务全部 healthy」。
#
# 用 python -c 而不是 curl：python:3.11-slim **不带 curl**，
# 为一条健康检查装 curl 不值得（多一个包、多一层）。
#
# ⚠️ start-period 给 40s：启动钩子会去开 Chroma（一次 mkdir + SQLite 打开），
#    首次挂载 E: 盘的 bind mount 又偏慢。start-period 太短会让容器在
#    真正就绪前就被判 unhealthy，然后 compose 的 depends_on 连锁失败。
HEALTHCHECK --interval=15s --timeout=6s --start-period=40s --retries=5 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/system/health',timeout=4).status==200 else 1)"

# ⚠️ 这里绑 0.0.0.0 **不是** AC-37 的违规。
#    AC-37 管的是**资源暴露的那一层**（见附录 B.6 的原话）。容器内绑
#    0.0.0.0 只让同一 Docker 网络里的同伴能访问；真正对外的口子是
#    compose 里的 `127.0.0.1:8000:8000` —— 它把发布限制在宿主回环，
#    局域网依然看不见。两件事要分清，否则会为了「看起来合规」
#    把它改成 127.0.0.1，那会让 qy-nginx **连不上后端**。
CMD ["python", "-m", "uvicorn", "backend.app.main:app", \
     "--host", "0.0.0.0", "--port", "8000"]
