# 校园AI问答助手（基于 RAG，多智能体）

为学校、学院、企业部门打造的 AI 问答助手 **阶段一 MVP**：基于自有文档的检索增强问答（RAG）。
问答全部基于你放入知识库的资料，**不编造答案**。已内置多个可切换的智能体，并做成
**可复用的 `agentkit` 包**——同一套代码能直接搬到别的项目里用。

## 团队协作

仓库地址：https://github.com/fengtangs/school-ai-assistant

| 分支 | 用途 |
|------|------|
| `main` | 稳定版本，部署到服务器 / 华技云集成的分支 |
| `dev` | 集成主线，各成员把自己的开发分支合并到这里 |
| `dev_<姓名>` | 个人开发分支（如 `dev_nsf`），每人一个，互不干扰 |

### 成员首次拉取
```bash
git clone -b dev git@github.com:fengtangs/school-ai-assistant.git
cd school-ai-assistant

# 建自己的个人分支（避免多人直接改 dev 造成冲突）
git checkout -b dev_你的名字
git push -u origin dev_你的名字

python -m venv venv
# Windows:  venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # Windows: copy .env.example .env
```

### 日常提交流程
```bash
git add .
git commit -m "feat: 新增XX功能"
git push origin dev_你的名字          # 推到自己的分支
# 然后在 GitHub 上开 PR：dev_你的名字 → dev
# 验证通过后，再由 dev 合并到 main
```

### 配置要求
- `.env` **不入库**（已在 `.gitignore`），每人本地自行从 `.env.example` 复制并填写
- `venv/`、`data/processed/`（自动生成的索引）、`.idea/` 均不入库，克隆后需本地重建索引
- 新增依赖务必同步更新 `requirements.txt`


## 内置智能体
| id | 名称 | 知识范围 |
|----|------|----------|
| `campus` | 校园小助手 🏫 | 学校概况、教务、学生管理、奖助学金、校园生活 |
| `huajiyun` | 华小智（华技云ICT） 🤖 | 华技云华为ICT实训平台功能、华为 HCIA/HCIP/HCIE 认证与考点、项目背景 |

> 想加新智能体？只需在 `agentkit/agents.json` 里加一条（id / 名称 / 知识库目录 / 系统提示词），
> 把文档放进对应目录，运行 `python scripts/build_index.py` 即可。

## 功能
- 基于 DeepSeek 大模型生成自然语言回答
- 基于自有知识库的语义检索（RAG），回答有据可查、缺失即如实说明
- 自带网页聊天界面，支持**智能体切换**下拉
- 支持 .md / .txt / .pdf；中文语义向量检索（custom 远程接口 / hf 本地模型 / mock 演示三模式）
- 开箱即用：未配置 API Key 也能跑通检索流程（仅不生成智能回答）

## 目录结构
```
school-ai-assistant/
├── app.py                  # FastAPI 服务（多智能体 + 聊天界面）
├── config.py               # 配置（读 .env，密钥不留存源码）
├── agentkit/               # ★ 可复用智能体包（可整体复制到别的项目）
│   ├── agent.py            # Agent 类：参数化知识库 + 系统提示词 + 索引
│   ├── registry.py         # 从 agents.json 加载/缓存多智能体
│   ├── agents.json         # 智能体登记（id/名称/目录/提示词）
│   └── __init__.py
├── core/                   # 底层引擎（被 agentkit 复用）
│   ├── embeddings.py       # 向量化（custom 远程 / hf 本地 / mock 演示）
│   ├── vector_store.py     # 轻量持久化向量库（按 agent 隔离 + 维度校验）
│   ├── document_loader.py  # 文档加载与切分
│   └── llm.py              # DeepSeek 调用
├── ui/index.html           # 聊天界面（含智能体切换）
├── scripts/build_index.py  # 构建/重建所有智能体索引
├── data/raw/               # 校园助手知识库（.md/.txt/.pdf）
├── data/raw_huajiyun/      # 华小智知识库（平台介绍/认证考点/项目背景）
├── data/processed/agents/  # 自动生成的分智能体索引
├── requirements.txt
└── .env.example
```

## 快速开始

### 1. 准备环境（Python 3.10+）
```bash
cd school-ai-assistant
python -m venv venv
# Windows:  venv\Scripts\activate
# macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置
复制 `.env.example` 为 `.env`：
```
cp .env.example .env      # Windows: copy .env.example .env
```

#### 大模型后端（三选一，改 `LLM_PROVIDER` 切换）

| `LLM_PROVIDER` | 说明 | 需填写的变量 |
|---|---|---|
| `custom` | **推荐**，自定义 OpenAI 兼容网关 | `CUSTOM_API_KEY` / `CUSTOM_BASE_URL` / `CUSTOM_MODEL` |
| `ollama` | 本地/局域网 Ollama，免密钥、数据不出校 | `OLLAMA_BASE_URL` / `OLLAMA_MODEL` |
| `deepseek` | DeepSeek 官方 API | `DEEPSEEK_API_KEY` |

当前默认对接**山东省综合金融服务平台智算服务专区**：
```
LLM_PROVIDER=custom
CUSTOM_BASE_URL=https://www.tokensd.com.cn/v1
CUSTOM_MODEL=deepseek-v4-flash
CUSTOM_API_KEY=sk-...            # 在该平台申请
```
> ⚠️ 该网关提供 50+ 模型（deepseek-v4-flash/pro、glm-5.3、kimi-k2.7、qwen3.7-max 等），
> 查看全部：`python -c "from core.llm import list_custom_models; print(list_custom_models())"`
>
> ⚠️ **`LLM_MAX_TOKENS` 建议 ≥ 4096**：`deepseek-v4-*` 等思考型模型会先消耗
> reasoning token，值太小会出现「思考完了但正式回答为空」。

> 不填 Key 也能运行（演示模式），但回答仅展示检索片段。

### 3. 放入知识文档
把文档放进对应智能体的知识库目录（`data/raw/` 或 `data/raw_huajiyun/`，支持 .md/.txt/.pdf）。
项目已内置样例文档，可直接体验。

### 4. 构建索引
```bash
python scripts/build_index.py
```

### 5. 启动服务
> 端口说明：本项目默认使用 **10086**，刻意避开华技云（springboot-huawei-ict）自带的 8080，
> 以便两者可同时运行——华技云的 AI 助学智能体（华小智）通过 `app.ai.agent-api-base-url`
> 直接调用本服务的 `/api/chat`（agent=huajiyun）。
```bash
python app.py
# 或
uvicorn app:app --host 0.0.0.0 --port 10086
```
浏览器打开 http://localhost:10086 即可对话（右上角切换智能体）。
API 文档：http://localhost:10086/docs

## 向量嵌入（决定检索准不准）

检索质量几乎完全取决于这一步。三种模式：

| `EMBEDDING_PROVIDER` | 维度 | 说明 |
|---|---|---|
| `custom` | 4096 | **推荐**，走远程 `/v1/embeddings`，语义检索准确（复用 `CUSTOM_API_KEY`） |
| `hf` | 512 | 本地 `sentence-transformers`，需下载 ~130MB 权重，离线可用 |
| `mock` | 512 | 字符 n-gram 哈希，**不是语义检索**，只是字面重合度，仅适合演示 |

```bash
# 推荐配置
EMBEDDING_PROVIDER=custom
CUSTOM_EMBEDDING_MODEL=lucion-qwen3-embedding-8b   # 4096 维
```

> ⚠️ **切换 provider 后必须重建索引**，因为向量维度会变（mock 512 → custom 4096）。
> 忘了重建会抛出 `StaleIndexError` 并提示你执行重建命令，不会静默给出错误结果。
>
> ```bash
> python scripts/build_index.py
> ```
>
> 实测对比（同一问题「华为HCIA-Datacom考试重点」）：
> `mock` 完全召回不到考点手册 → 模型只能回答"参考信息中没有"；
> `custom` 精准命中该手册（相似度 0.72 vs 次名 0.55）→ 完整答出 6 大考点及分值。

## 接口说明
- `GET  /api/agents`   列出所有智能体（id/名称/图标）与默认智能体
- `POST /api/chat`      body: `{"question": "...", "agent": "huajiyun"}` → 答案、来源、所用智能体
- `POST /api/rebuild`   body 可选 `{"agent": "huajiyun"}` 重建指定智能体，或不带 body 重建全部
- `GET  /api/health`   服务状态、各智能体索引条数、模型就绪情况

## ★ 把智能体复用到别的项目
`agentkit` 是一个与具体业务解耦的包，复制即可用：
1. 把 `agentkit/` 和 `core/`（引擎）两个目录一起复制到目标 Python 项目；
2. 准备你的知识库目录（放 .md/.txt/.pdf），在 `agentkit/agents.json` 里登记一个或多个智能体；
3. 配置环境变量（`.env`：`LLM_PROVIDER` + 对应后端的 `CUSTOM_*` / `OLLAMA_*` / `DEEPSEEK_*`，参见 `.env.example`）；
4. 在代码里调用：
```python
from agentkit import build_all, get_agent
build_all()                                   # 首次或更新文档后构建索引
a = get_agent("huajiyun")                      # 拿到某个智能体
print(a.ask("华技云有哪些功能模块？"))         # 基于知识库回答
```
`Agent` 本身也可直接实例化（`from agentkit import Agent`），传入知识库目录与系统提示词即可，
不依赖任何项目专属配置，方便嵌入到 Flask / Django / 企业微信 / 小程序后端等任意 Python 服务。

## 后续扩展路线
- 多轮对话 + 用户身份区分
- 对接教务系统、OA、一卡通等 API，实现“办事”（查课表、查成绩、请假）
- Agent 工具调用（Function Calling）：主动推送选课冲突提醒、成绩预警等

## 数据安全提示
- 敏感个人信息、成绩等不要直接放入公开知识库
- 调用大模型前对提问做必要脱敏
- 部署在校园内网或可信环境，做好访问控制与日志审计
