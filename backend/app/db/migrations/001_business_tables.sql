-- ══════════════════════════════════════════════════════════════════
-- 001 业务表：户型、方案、审计
-- ══════════════════════════════════════════════════════════════════
--
-- 字段基本照抄需求文档 §5.2 / §5.5，**有三处刻意的偏离**，都写在这里，
-- 因为"照抄文档"和"能跑起来"在这三处对不上 —— 而偏离如果只存在于代码里，
-- 下一个人会以为是实现偷懒。
--
-- 偏离 1：`house_layouts.original_image_url` **不建**。
--   文档把它写成 NOT NULL，前提是"户型图会传到一个对象存储"。本项目
--   从第一天起就没有这件事，而且是有意的：户型图是用户的房屋结构，
--   隐私承诺（AC-24 与 §2.3.2）说的是"不上传第三方"，数据库虽然在本机，
--   但把一张 base64 图存进业务表还会让每次读写多背上百 KB —— 而
--   我们真正需要长期保留的是**结构化的解析结果**，不是那张图。
--   所以这一列不存在，而不是建一个永远是 NULL 的列。
--
-- 偏离 2：`design_plans` 的主键是 **(layout_id, plan_id)**，不是 `id`。
--   ⚠️ 文档写的 `id VARCHAR(64) PRIMARY KEY` 在这个系统里**建不起来**：
--   运行时的 plan_id 是 `plan_{style}_{grade}`（workflow.py:215），
--   **不含 layout** —— 两个不同用户都生成"现代简约·经济型"，拿到的
--   plan_id 都是 `plan_modern_economy`。用它当全局主键，第二个人的方案
--   要么插不进去，要么把第一个人的覆盖掉。
--   plan_id 的真正唯一域是"某个户型下的某一套"，所以主键就是这两列。
--   列名也从 `id` 改成 `plan_id`，免得它看起来像个全局标识。
--
-- 偏离 3：**没有一条外键指向 `users`。** 用户还在 `seed_data/users.json`
--   里（把认证搬进库是另一个改动，本轮明确不做）。建不了的外键写成
--   `REFERENCES users(id)` 只会让第一次写入就失败。所以 `user_id` 是裸
--   INTEGER，并在下面标注了它引用的是什么 —— 完整性暂时由应用层保证。
--   `design_plans.layout_id` 的引用**保留**：它指向本文件里的表，
--   而且解析总是先于生成（同一份数据、同一个库），能建得起来就该建。
--
-- 为什么不用 `TIMESTAMPTZ` 而用文档里的 `TIMESTAMP`：
--   文档口径就是 TIMESTAMP，且本项目全部时间戳都在同一台机器上产生。
--   换成 TIMESTAMPTZ 会让 `created_at` 的显示口径与文档/前端不一致，
--   那是另一个决定，不在这轮里顺手改。
--
-- 偏离 4：`design_plans.vector_image_url` 建了但**永远不写**。
--   矢量图不是一个存下来的文件：它是 `render_plan_for(layout)` 的产物，
--   纯函数、实测毫秒级，接口 `GET /layout/{id}/plan.svg` 按需现渲染
--   （见 routes.py 的接口说明）。而且它由 **layout** 决定，与 plan_id 无关 ——
--   给三套方案各存一行相同 URL，等于把同一个值复制三遍。
--   列留着是因为将来真把图落成文件时它才有内容，那时填也不迟；
--   `effect_image_url`（AI 图）没建 —— AC-08 已作废，建了就是空列。
--
-- 偏离 5：`design_plans.hotspots` 建了，但**这一版流程里恒为 NULL**。
--   热区是 `services/render/hotspots.py` **按需现算**的（纯函数、毫秒级），
--   由 `GET /layout/{id}/hotspots` 直接返回；**没有任何图节点**写
--   `state["hotspots"]`（实测 `grep -rn '"hotspots"' backend/app`，
--   它只在 state.py 里被声明并初始化成 `{}`）。
--   列留着是因为"某个方案的悬停价格分布"确实是值得长期保留的数据，
--   而接口是按需算的、算完就丢 —— 将来决定持久化时这一列就在。
--   ⚠️ 与偏离 4 同一条纪律：**不写就不假装写了**，如实记在这里，
--      免得下一个人花了半小时去查"为什么热区列是空的、是不是写挂了"。
--
-- ⚠️ 上面几段都是**注释**，改它不会造成"文件与库不一致"（001 已经应用过）。
--   但要改 DDL —— 加列、改类型、加约束 —— 就必须新开一个文件。
--   这条界线值得记住：注释是文档，DDL 是状态。

-- ── 户型 ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS house_layouts (
    id                 VARCHAR(64) PRIMARY KEY,   -- layout_20260922_001
    user_id            INTEGER,                   -- 引用 seed_data/users.json 的 id（无外键，见偏离 3）
    parsed_data        JSONB,                     -- 结构化解析结果（房间/墙体/门窗/面积）
    diagnosis_data     JSONB,                     -- A-02 诊断结果
    confidence         REAL,
    model_used         VARCHAR(32),
    degraded           BOOLEAN DEFAULT FALSE,
    degrade_reason     VARCHAR(128),
    parse_elapsed_ms   INTEGER,
    created_at         TIMESTAMP DEFAULT NOW(),
    updated_at         TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_house_layouts_user
    ON house_layouts (user_id, created_at DESC);

-- ── 方案 ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS design_plans (
    layout_id          VARCHAR(64) NOT NULL
                       REFERENCES house_layouts(id) ON DELETE CASCADE,
    plan_id            VARCHAR(64) NOT NULL,      -- plan_{style}_{grade}，**不含 layout**，见偏离 2
    style              VARCHAR(32) NOT NULL,
    budget_grade       VARCHAR(16) NOT NULL,
    total_budget_min   DECIMAL(12,2),
    total_budget_max   DECIMAL(12,2),
    breakdown          JSONB,
    budget_computed_by VARCHAR(32),               -- rule_engine_v1：标明数字不是模型编的
    risks              JSONB,
    environment        JSONB,                     -- AC-20：甲醛/TVOC 风险档（只给档，不给浓度）
    materials          JSONB,                     -- A-05 选材产物（含 AC-18 覆盖率与替换记录）
    quanyou_products   JSONB,
    vector_image_url   VARCHAR(512),
    hotspots           JSONB,
    status             VARCHAR(16) DEFAULT 'processing',
    created_at         TIMESTAMP DEFAULT NOW(),
    updated_at         TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (layout_id, plan_id)
);

CREATE INDEX IF NOT EXISTS idx_design_plans_layout
    ON design_plans (layout_id);

-- ── 审计（AC-14）──────────────────────────────────────────────────
--
-- 「可查询」是这一条的原文要求。落文件时只能按字段 grep；落库之后
-- "昨天有多少次生成是降级的"才真的能一条 SQL 问出来 —— 索引就是为这个建的。
CREATE TABLE IF NOT EXISTS audit_logs (
    id             BIGSERIAL PRIMARY KEY,
    user_id        INTEGER,                       -- 同偏离 3，无外键
    action         VARCHAR(64) NOT NULL,          -- login / task_created / task_finished / ...
    resource_type  VARCHAR(32),
    resource_id    VARCHAR(64),
    model_used     VARCHAR(32),
    degraded       BOOLEAN DEFAULT FALSE,
    degrade_reason VARCHAR(128),
    token_usage    JSONB,
    latency_ms     INTEGER,
    ip_address     VARCHAR(45),
    created_at     TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_created
    ON audit_logs (created_at DESC);
-- 按事件名 + 时间聚合是最常见的问法（"今天有几次降级生成"）
CREATE INDEX IF NOT EXISTS idx_audit_logs_action_created
    ON audit_logs (action, created_at DESC);
