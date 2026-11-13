# DevFlow AI 的数据来源与源码核验

DevFlow AI 的源码和配置以当前 Git checkout 为权威来源。Git checkout 指开发者当前检出的本地工作树，可能包含尚未提交的修改。需要确定一个函数、配置或依赖的实际内容时，应读取当前工作区，而不是依靠知识库里旧版本的描述。

源码与配置优先使用 `workspace.search_code` 和 `workspace.read_file` 等工作区工具检索，不作为 `code_file` 写入 RAG Document。GitHub 仓库的远端信息不能替代当前检出版本，也不能替代尚未提交的本地修改。

适合进入 RAG 的是会反复使用的非结构化知识，例如 Issue 描述、PR 说明与 Review 评论、失败 CI 日志、项目说明、上传知识和已批准的长期记忆。团队成员、实时状态、统计和派生报告等结构化信息应查询数据库或 API。

本文件是用于课程固定问答回归的说明资料，不是对任意目标仓库实时源码的证明。需要验证具体代码行为时，仍应通过当前工作区和实际测试核对。

核对源码：`README.md` 的 RAG 知识问答系统；`backend/app/services/rag/source_policy.py`；`backend/app/services/rag/vector_store.py` 的源码写入限制。
