# DevFlow AI 的混合 RAG 与证据装配

RAG 是检索增强生成：系统先从项目知识中检索候选证据，再让模型依据证据回答。上传的知识文件经过文本提取、结构感知切片和 Embedding，切片及来源元数据保存在关系数据库中，向量按配置写入 Milvus。

在 ChatAgent 开始推理前，ContextAssembler 根据原始问题检索并装配证据。这个预检索阶段把证据和历史消息一起放入模型上下文。在推理循环中，ChatAgent 仍可以调用 `rag.search_similar_documents` 工具，改写问题并继续检索。

因此，这条混合 RAG 链路同时包含预检索和 Agent 的按需补查。评测运行的是生产 ChatAgent，而不是只单独测一次检索函数。每条评测用例都使用隔离会话，收集检索证据、工具调用和最终回答；用例结束时回滚运行期间的会话写入。

关闭 Milvus 后，系统可以使用关键词分支验证基础调用链，但向量检索处于降级状态。deterministic Embedding 只用于演示和离线测试，不代表真实语义向量质量。完整向量检索需要可用的 Embedding 配置与 Milvus 服务。

核对源码：`backend/app/services/chat_memory.py` 的 `ContextAssembler`；`backend/app/services/evals/ragas_runner.py` 的 `_run_chatagent_case`；`backend/app/api/routes/chat.py` 的工具装配。
