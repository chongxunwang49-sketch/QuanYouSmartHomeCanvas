-- ═══════════════════════════════════════════════════════════════════
-- 智友问答：会话与消息
-- ═══════════════════════════════════════════════════════════════════
-- 需求方 2026-09-27：在「方案生成」与「知识库管理」之间加一个智能体问答模块，
-- 界面按 DeepSeek 网页那种格局 —— 左侧历史（可置顶/重命名/删除）、右侧对话。
--
-- 为什么落库而不是塞 Redis：
--   历史记录是**跨会话**的东西（今天问过、明天还要看），而 Redis 在本项目里
--   是"当前进程的活状态"（任务进度、额度计数），都带 TTL。
--   落库的另一个好处：回答里的引用来源要跟着消息一起留下来 ——
--   用户回头翻这条对话时，仍然看得到"当时依据的是哪几份文档"。

CREATE TABLE IF NOT EXISTS chat_conversations (
    conversation_id VARCHAR(64)  PRIMARY KEY,
    user_id         INTEGER      NOT NULL,
    --: 会话标题。第一条提问自动截断生成，之后可以手动重命名
    title           VARCHAR(120) NOT NULL DEFAULT '新对话',
    --: 置顶。列表排序先看它，再看更新时间
    pinned          BOOLEAN      NOT NULL DEFAULT FALSE,
    --: 这次对话挂在哪个户型/方案上（可为空 —— 用户可以不选方案直接问）
    layout_id       VARCHAR(64),
    plan_id         VARCHAR(64),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 列表查询固定是"某人的会话，按置顶 + 更新时间"
CREATE INDEX IF NOT EXISTS idx_chat_conv_user
    ON chat_conversations (user_id, pinned DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              BIGSERIAL    PRIMARY KEY,
    conversation_id VARCHAR(64)  NOT NULL
                    REFERENCES chat_conversations (conversation_id) ON DELETE CASCADE,
    role            VARCHAR(16)  NOT NULL,          -- user / assistant
    content         TEXT         NOT NULL,
    --: 回答引用了哪些知识库文档。**只对 assistant 消息有意义**
    sources         JSONB,
    --: 这一轮用了哪个模型（含降级信息）。排查"回答质量变了"时要看它
    model_used      VARCHAR(64),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 读一条会话的消息：按 id 顺序，且只按会话过滤
CREATE INDEX IF NOT EXISTS idx_chat_msg_conv
    ON chat_messages (conversation_id, id);
