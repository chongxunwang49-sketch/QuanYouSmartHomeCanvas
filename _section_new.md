
---

## 4.5 本机运行时现状（**实测快照，2026-09-23**）

> ⚠️ 这是**快照**，不是永久事实。动手前用 §4.6 的命令重新核一遍。
>
> 这一节存在的理由：上一版交接文档只写了"代码为什么是这样"，
> 没写"这台机器现在是什么状态"。结果新窗口反复来问端口/进程/容器的事。
> **凡是新窗口可能问的运行时问题，都写在这里。**

### 4.5.1 当前在跑的服务（**都是上一个窗口起的**）

| 端口 | PID | 进程 | 启动时间 | 状态 | 归属 |
|---|---|---|---|---|---|
| 5173 | 26736 | node (vite) | 09-23 10:59 | HTTP 200 | **上一个窗口起的**，跨过一次上下文压缩 |
| 8000 | 28524 | python (uvicorn) | 09-23 16:12 | degraded（redis=false，**常态**） | **上一个窗口起的** |

**上一个窗口关掉后**：这两个进程会变成**孤儿**。Windows 不会因为父进程退出就杀子进程，
所以大概率还活着，但没人管。**新窗口的处置规则：**

```powershell
# 先看端口有没有人听
Get-NetTCPConnection -LocalPort 8000 -State Listen
```

- **有监听且服务有响应** → 直接复用，**别重启**（重启会撞端口）
- **有监听但服务没响应** → 是僵尸，先 `Stop-Process -Id <PID>` 再起
- **端口空着** → 自己起一套

> ⚠️ 第二条最容易踩：端口被占着但服务已经死了，直接起新的会**静默绑不上**，
> 而表现是"前端一直连不上后端"。

### 4.5.2 Docker（**已装、可用、但有个调用陷阱**）

```
Docker CLI      29.7.2
Compose         v5.5.1
WSL             Ubuntu(Stopped) + docker-desktop(Running)
VM 内存上限      8,189,022,208 B = 7.63 GiB   ← 与需求文档「坑 5」写的完全一致
VM CPU          16
容器总数         37（运行 8 / 停止 29）
镜像总数         40
OOM(exit 137)   8 个
```

#### ⚠️⚠️ 陷阱：`docker` 命令在 Git Bash 里**拿不到输出**

实测：`docker ps` / `docker info` / `docker --version` 在 Git Bash 里**全部返回空**，
**退出码还是 0** —— 看起来像"引擎挂了"，**其实是拿不到 stdout**。
同一条命令用 PowerShell 直接调 `docker.exe` 就完全正常。

**上一个窗口差点因此把"本机 Docker 引擎不稳定"写进交接文档 —— 那是错误结论。**

```bash
# ❌ 在 Git Bash 里这样调，输出是空的
docker ps -a

# ✅ 这样调
powershell -NoProfile -Command "& 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}'"
```

**新窗口如果发现 docker 命令"没反应"，先换 PowerShell 再下结论。**

### 4.5.3 正在运行的容器（**8 个，全都不是本项目的**）

| 容器 | 宿主端口 | 说明 |
|---|---|---|
| `zhirongxi-nginx` | **8088**→80 | 另一个项目，**别动** |
| `zhirongxi-frontend` | 无宿主映射（仅容器内 80） | 同上 |
| `zhirongxi-backend` | 无宿主映射（仅容器内 8000） | 同上 |
| `zhirongxi-neo4j` | 7475→7474 | 同上 |
| `zhirongxi-rerank` | 无宿主映射 | 同上 |
| `zhirongxi-mysql` | 3307→3306 | 同上 |
| `zhirongxi-redis` | 7382→6379 | 同上 |
| **`my-rerank`** | **9999**→8001 | ★ **这个和本项目有关**，见下 |

> ⚠️ **`zhirongxi-*` 是另一个在跑的项目。清理容器时只能删 Exited 的，
> 绝不能 `docker rm -f` 一把梭全清。**

**★ `my-rerank` 是本项目要复用的重排服务。**
需求文档 §3.2 写明：「Reranker → 不新增容器，复用已部署的 `my-rerank`
（即 bge-reranker-v2-m3），通过环境变量 `RERANK_URL` 开启/关闭」。
它现在 Up 2 hours (healthy)，宿主端口 **9999**。

⚠️ **容器内访问它必须用 `host.docker.internal:9999`，不能用 `127.0.0.1:9999`** ——
容器里的 `127.0.0.1` 指的是容器自己。这是容器化最经典的坑之一。

### 4.5.4 宿主端口占用（实测）

| 端口 | 状态 | 谁在用 |
|---|---|---|
| **80** | **空闲** ✓ | 需求文档 §3.2 要留给 `qy-nginx` |
| **8000** | **被占** | 上一个窗口的 uvicorn（PID 28524）← **AC-16 的冲突点** |
| 5173 | 被占 | 上一个窗口的 vite（PID 26736） |
| **5432** | **空闲** ✓ | §3.2 要留给 `qy-postgres` |
| **6379** | **空闲** ✓ | §3.2 要留给 `qy-redis` |
| 8080 / 3000 | 空闲 | — |

> 注意 `zhirongxi-nginx` 占的是 **8088** 不是 80，所以 80 是空的。

### 4.5.5 §3.2 需要的 4 个镜像 —— **本地全都有**

| 镜像 | 本地是否存在 |
|---|---|
| `postgres:15-alpine` | ✅ |
| `redis:7-alpine` | ✅ |
| `nginx:alpine` | ✅ |
| `python:3.11-slim` | ✅（另有 `docker.m.daocloud.io/library/python:3.11-slim`） |

**不用拉镜像，省一大截时间**（国内拉 Docker Hub 很容易卡住）。

### 4.5.6 29 个停止容器里的 OOM 记录

带 `exit 137`（被内核 OOM 击杀）的**有 8 个**：
`zft2-neo4j`、`rag-neo4j`、`ragstar-rerank`、`ragstar-milvus`、`ragstar-neo4j`、
`ragstar-mysql`、`rag-backend`、`stock-agent-backend`。

**这印证了需求文档「坑 5」的警告**：7.63GB VM 内存紧张，
文档自己写了「M0 第一步必须先清理历史容器，否则 7.63GB 预算无法保障」。

按 §3.2 的预算表，4 个容器合计约 **725MB** —— 理论上放得下。
但 `my-rerank` 和 `zhirongxi-*` 那 7 个还在跑。**动手前先 `docker stats` 看一眼实际余量。**

---

## 4.6 开工前的环境检查清单（**每次新窗口接手都跑一遍**）

```bash
# 1. 两个服务还活着吗
curl -s -o /dev/null -w "5173 → %{http_code}\n" --max-time 6 http://localhost:5173/
curl -s --max-time 6 http://127.0.0.1:8000/api/v1/system/health

# 2. 测试基线（改代码前先确认它是绿的）
cd "C:/Users/DELL/Desktop/全友·智绘家QuanYou Smart HomeCanvas" && python -m pytest -q

# 3. Docker（记住：必须走 PowerShell）
powershell -NoProfile -Command "& 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' ps --format '{{.Names}}|{{.Ports}}'"

# 4. 端口占用
powershell -NoProfile -Command "foreach ($p in 80,8000,5173,5432,6379) { $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if ($c) { Write-Output \"$p 被占 PID $($c.OwningProcess)\" } else { Write-Output \"$p 空闲\" } }"
```

**"正常"的基线长这样**（别把常态当故障去修）：

- 后端 health = **`degraded`**，其中 `redis: false` —— **Redis 一直没起，这是常态**
- 前端 5173 = `200`
- 测试 = `702 passed, 1 deselected`，约 35 秒
- git 工作区干净

---

## 4.7 AC-16 Docker Compose 专项预案

> 这是新窗口最可能先做的一件事，所以把已知的坑全列出来，**别临场才发现**。

### 4.7.1 需求文档 §3.2 已经定死了拓扑（**第 1072 行起**）

**AC-16 不是从零设计，是照图纸施工。动手前先读那一节。**

| # | 容器 | 镜像 | 内存预算 | 端口绑定 |
|---|---|---|---|---|
| 1 | `qy-postgres` | `postgres:15-alpine` | ~80 MB | `127.0.0.1:5432:5432` |
| 2 | `qy-redis` | `redis:7-alpine` | ~30 MB | `127.0.0.1:6379:6379`，`maxmemory 256mb` |
| 3 | `qy-backend` | 基于 `python:3.11-slim` 构建 | ~600 MB | `127.0.0.1:8000:8000` |
| 4 | `qy-nginx` | `nginx:alpine` | ~15 MB | `127.0.0.1:80:80` |

**合计 ≈ 725 MB。**
**端口一律绑回环地址，绝不用 `0.0.0.0`**（AC-37：本机存用户上传的户型图）。

**不在容器里跑的**（V1.0 的 6 服务砍成 4）：

- ChromaDB → 进程内 `chromadb.PersistentClient(path="E:/quanyou/data/chroma")`
- 前端 → 本地构建成静态产物，由 `qy-nginx` 托管，**不跑 node 容器**
- Reranker → 复用宿主机的 `my-rerank`

### 4.7.2 四个已知的坑（都有对策）

**① 端口 8000 被本地 uvicorn 占着（PID 28524）**

- **对策：腾出 8000，不要改端口。**
- 理由：文档 §3.2 钉死了 8000；而 dev 模式（vite + uvicorn）和 compose 模式
  **本来就是同一件事的两种跑法**，在 8000 上天然互斥。这是**模式切换**，不是要解决的冲突。
- `vite.config.ts:9` 的 `BACKEND` 是 `process.env.VITE_BACKEND || 'http://127.0.0.1:8000'`，
  所以"改 8001"技术上可行 —— **但别改**，改了 AC-16 验收要写脚注，不值当。
- 本地 uvicorn 重启只要一条命令，停掉零成本。

**② `qy-postgres` 在本项目里是"Healthy 但空"的容器**

- 项目**现在没有任何数据库层**（户型存内存 + Redis）。
- AC-16 只数 Healthy 容器数，所以**这不是问题**。
- ⚠️ **别被它带偏去写 schema / ORM** —— 那是 AC-14 审计日志的活。

**③ compose 模式下前端不在 5173，在 80**

- `qy-nginx` 托管构建产物。演示地址从 `localhost:5173` 变成 `http://127.0.0.1`。
- **换模式时同时换地址，别以为前端坏了。**

**④ `qy-backend` 要访问宿主机的 `my-rerank`**

- 必须用 `host.docker.internal:9999`，**不是** `127.0.0.1:9999`。
- 对应配置项：`RERANK_URL`（`backend/app/core/config.py`）。

### 4.7.3 建议的执行顺序

```
① 读文档 §3.2（第 1072 行起）                —— 拿到权威拓扑
② 写 Dockerfile / compose.yaml / nginx.conf   —— 这一步不需要任何端口
③ docker compose config                        —— 只验证语法，不启动
④ docker compose build qy-backend              —— 构建，不需要 8000
⑤ 【切换点】停掉本地 uvicorn（PID 28524）
⑥ docker compose up -d；docker compose ps      —— 验收：4 个 Healthy
⑦ 浏览器打开 http://127.0.0.1                  —— 端到端确认
   失败就回滚：docker compose down && 重启本地 uvicorn
```

**⑤ 是唯一的破坏性动作，做之前先确认 ①~④ 都过了。**

### 4.7.4 验收要看什么

- `docker compose ps` → **4 个服务全是 `healthy`**（AC-16 的字面判据）
- `netstat -ano | findstr LISTENING` → **没有 `0.0.0.0:8000` / `0.0.0.0:5432`**（AC-37）
- 前端能打开、能上传、能解析
- `DEEPSEEK_API_KEY` 通过环境变量注入，**不写进 compose 文件**

---

## 4.8 新窗口最容易问的 12 个问题（**预答**）

> 如果新窗口想问下面任何一条，说明这一节写漏了 —— 请补充它。

1. **项目是做什么的？** → §0 / §1
2. **先做哪件事？** → §8。推荐：AC-23（低风险、可演示）→ AC-16 → AC-14 → AC-11 → AC-01
3. **之前起的服务还留着吗？** → §4.5.1。都是我起的孤儿进程；端口有响应就复用，**别重启**
4. **`degraded` 是不是坏了？** → **不是，是常态**。Redis 一直没起。别去"修"
5. **Docker 命令没反应？** → §4.5.2 **换 PowerShell 调 `docker.exe`**
6. **8000 被占了怎么办？** → §4.7.2 ①。**腾出来，别改端口**
7. **能直接 `docker rm` 清空容器吗？** → **不能**。有 7 个 `zhirongxi-*` + `my-rerank` 在跑
8. **为什么后端测试能跑但没有 GPU？** → §4.2。base 环境没 CUDA；测试不需要
9. **改完前端怎么验证没把包变大？** → §4.4。`npm run build` 后看 `ParseView-*.js` 是否还在 ~45KB
10. **为什么 `walls_closed` 用洪水填充而不是找闭环？** → §7.1
11. **能看图片吗？** → §10.4。**PNG 能看**，WebGL 实时画面不能看，SVG 不能看
12. **改需求文档行不行？** → **不行**，见 §2
