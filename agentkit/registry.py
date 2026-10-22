"""多智能体登记与获取。

agents.json 描述有哪些智能体（id、名称、知识库目录、系统提示词）。
本模块负责加载配置、惰性地构建并缓存 Agent 实例，供 Web 服务或别的项目调用。
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agentkit.agent import Agent  # noqa: E402

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents.json")

_cache = {}


def _load_config() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def list_agents() -> list:
    """返回所有智能体的简要信息列表（含 id/name/icon）。"""
    cfg = _load_config()
    out = []
    for a in cfg.get("agents", []):
        out.append({
            "id": a["id"],
            "name": a["name"],
            "icon": a.get("icon", "🤖"),
        })
    return out


def get_agent(agent_id: str) -> Agent:
    """按 id 获取（并缓存）一个智能体实例；不存在则抛 KeyError。"""
    if agent_id in _cache:
        return _cache[agent_id]
    cfg = _load_config()
    for a in cfg.get("agents", []):
        if a["id"] == agent_id:
            agent = Agent(
                agent_id=a["id"],
                name=a["name"],
                knowledge_dir=a["knowledge_dir"],
                system_prompt=a["system_prompt"],
                icon=a.get("icon", "🤖"),
                school_name=a.get("school_name", ""),
                use_rag=a.get("rag", True),
            )
            _cache[agent_id] = agent
            return agent
    raise KeyError(f"未找到智能体: {agent_id}")


def get_default_agent_id() -> str:
    cfg = _load_config()
    agents = cfg.get("agents", [])
    return agents[0]["id"] if agents else "campus"


def build_all() -> dict:
    """重建所有智能体的索引，返回 {id: chunk_count}。"""
    cfg = _load_config()
    result = {}
    for a in cfg.get("agents", []):
        agent = get_agent(a["id"])
        result[a["id"]] = agent.build_index()
    return result
