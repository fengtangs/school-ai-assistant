"""agentkit —— 可复用的学生答疑智能体包。

典型用法：
    from agentkit import list_agents, get_agent, build_all
    build_all()                      # 首次或更新文档后重建索引
    a = get_agent("huajiyun")        # 获取某个智能体
    print(a.ask("华技云有哪些功能模块？"))
"""
from agentkit.agent import Agent
from agentkit.registry import list_agents, get_agent, get_default_agent_id, build_all

__all__ = [
    "Agent",
    "list_agents",
    "get_agent",
    "get_default_agent_id",
    "build_all",
]
