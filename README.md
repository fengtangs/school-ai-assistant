# 校园AI问答助手（基于 DeepSeek + RAG，多智能体）

为学校、学院、企业部门打造的 AI 问答助手 **阶段一 MVP**：基于自有文档的检索增强问答（RAG）。
问答全部基于你放入知识库的资料，**不编造答案**。已内置多个可切换的智能体，并做成
**可复用的 `agentkit` 包**——同一套代码能直接搬到别的项目里用。

## 团队协作

仓库地址：https://github.com/fengtangs/school-ai-assistant

| 分支 | 用途 |
|------|------|
| `main` | 稳定版本，部署到服务器 / 华技云集成的分支 |
| `dev` | 日常开发分支（本项目当前开发主线） |

### 成员首次拉取
```bash
git clone -b dev git@github.com:fengtangs/school-ai-assistant.git
cd school-ai-assistant
python -m venv venv
# Windows:  venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # Windows: copy .env.example .env
```

### 日常提交流程
```bash
git add .
git commit -m "feat: 新增XX功能"
git push origin dev                 # 推到 dev 分支
# 验证通过后，再开 PR 合并到 main
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
- 支持 .md / .txt / .pdf；中文向量检索（mock 零下载 / hf 真实模型两模式）
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
│   ├── embeddings.py       # 向量化（mock / hf）
│   ├── vector_store.py     # 轻量持久化向量库（索引按 agent 隔离）
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
复制 `.env.example` 为 `.env`，填写 DeepSeek API Key：
```
cp .env.example .env
```
在 [DeepSeek 开放平台](https://platform.deepseek.com) 创建 API Key，填入 `DEEPSEEK_API_KEY`。
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

## 切换到真实中文向量模型（推荐生产）
默认 `EMBEDDING_PROVIDER=mock` 使用无需下载的轻量向量，适合演示。生产建议改真实模型以获得更准的语义检索：
```
EMBEDDING_PROVIDER=hf
EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
```
首次运行自动下载权重（约 130MB），之后重新运行 `python scripts/build_index.py` 即可。

## 接口说明
- `GET  /api/agents`   列出所有智能体（id/名称/图标）与默认智能体
- `POST /api/chat`      body: `{"question": "...", "agent": "huajiyun"}` → 答案、来源、所用智能体
- `POST /api/rebuild`   body 可选 `{"agent": "huajiyun"}` 重建指定智能体，或不带 body 重建全部
- `GET  /api/health`   服务状态、各智能体索引条数、模型就绪情况

## ★ 把智能体复用到别的项目
`agentkit` 是一个与具体业务解耦的包，复制即可用：
1. 把 `agentkit/` 和 `core/`（引擎）两个目录一起复制到目标 Python 项目；
2. 准备你的知识库目录（放 .md/.txt/.pdf），在 `agentkit/agents.json` 里登记一个或多个智能体；
3. 配置环境变量（`.env`：`DEEPSEEK_API_KEY` 等，参见 `.env.example`）；
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
