"""构建/重建所有智能体的知识库索引。

用法：
    python scripts/build_index.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentkit import list_agents, build_all


def main():
    print(f"[开始] 将为以下智能体构建索引：{', '.join(a['name'] for a in list_agents())}")
    result = build_all()
    for agent_id, n in result.items():
        if n == 0:
            print(f"  [提示] 智能体 {agent_id}: 未读取到任何文档，请先在对应 knowledge_dir 放入 .md/.txt/.pdf。")
        else:
            print(f"  [完成] 智能体 {agent_id}: 成功构建 {n} 个文本片段。")
    print("[全部完成]")


if __name__ == "__main__":
    main()
