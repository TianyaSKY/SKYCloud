"""AI 目录整理 Worker: ReAct Agent 驱动的增量/全量整理.

只做编排与校验收尾; 具体改目录调用 organize_tools, 变更日志与 checkpoint 在
change_log_service. 增量事件过多或无 checkpoint 时回退全量扫描, 避免 Agent
上下文爆炸.
"""

import datetime
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict, cast

from langchain import agents
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from loguru import logger
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.features.folder import change_log as change_log_service
from app.features.folder.organize.tasks import assert_task_claim
from app.features.inbox import service as inbox_service
from app.infra.datetime_utils import BEIJING_TZ, utc_now
from app.infra.extensions import SessionLocal
from app.infra.llm.config import get_chat_model_config
from app.infra.llm.sync_client import record_llm_usage
from app.infra.llm.usage import TokenUsage

from .scope import OrganizeScope, authorize_session, bind_tools, organize_scope
from .tools import (
    check_empty_folders_internal,
    check_mixed_folders_internal,
    create_folder,
    delete_folder,
    find_duplicate_folders,
    find_empty_folders,
    find_mixed_content_folders,
    get_all_files,
    get_file_information,
    get_folder_tree,
    merge_folders,
    move_file,
    move_folder,
    rename_folder,
)

MAX_INCREMENTAL_EVENTS = 200
MAX_VALIDATION_RETRIES = 2
RECURSION_LIMIT = 40


def _organize_tools() -> list[BaseTool]:
    """按既定顺序返回整理器所用的工具列表."""
    return [
        get_all_files,
        get_folder_tree,
        create_folder,
        rename_folder,
        move_file,
        get_file_information,
        delete_folder,
        merge_folders,
        find_duplicate_folders,
        move_folder,
        find_mixed_content_folders,
        find_empty_folders,
    ]


class AgentUpdate(TypedDict):
    """由 `stream(..., stream_mode="updates")` 发射的单节点状态增量."""

    messages: list[BaseMessage]


class StreamingAgent(Protocol):
    """整理器所消费的 Agent 图精简协议接口."""

    def stream(
        self,
        state_input: dict[str, list[BaseMessage]],
        config: RunnableConfig,
        *,
        stream_mode: Literal["updates"],
    ) -> Iterator[dict[str, AgentUpdate]]:
        """流式执行单次图调用并生成各节点的状态更新."""
        ...


class AgentFactory(Protocol):
    """整理器所使用的 LangChain Agent 模块接口子集."""

    def create_agent(
        self, model: BaseChatModel, tools: list[BaseTool],
    ) -> StreamingAgent:
        """创建流式输出整理器状态更新的图结构."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class OrganizePlan:
    """单次整理运行选定的 Prompt 与检查点目标."""

    messages: list[BaseMessage]
    checkpoint_target_event_id: int | None
    full_scan_mode: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamState:
    """流式处理与校验辅助函数共享的运行状态记录."""

    user_id: int
    model: str
    results: list[str]
    messages: list[BaseMessage]
    usage: dict[str, int]


def get_llm_config() -> tuple[str, str, str]:
    """读取 Chat 模型配置三元组 (api, key, model)."""
    config = get_chat_model_config()
    return config["api"], config["key"], config["model"]


def _build_full_prompt(workspace_id: int) -> str:
    """全量整理系统提示 (英文指令供 LLM; workspace_id 必须写入约束)."""
    return f"""
You are an intelligent file organization assistant. Your goal is to keep the \
workspace's file system well-organized.

Current Workspace ID: {workspace_id}

Your task consists of the following parts:
1. **Information Gathering**:
   - Call `get_all_files` to get the list of all files.
   - Call `get_folder_tree` to view the current folder structure.
   - Call `find_duplicate_folders` to check for duplicates.
   - Call `find_mixed_content_folders` to check for folders \
violating the "Single Content Principle".
   - Call `find_empty_folders` to check for empty folders.

2. **Structure Optimization**:
   - **Single Content Principle (CRITICAL)**: A folder must NOT contain both files \
and subfolders simultaneously.
   - **Root Directory Cleanup**: If multiple sibling folders belong to one same \
category, create a parent category folder and move them with `move_folder`.
   - **Avoid Extension-based Categorization**: Prefer \
topic/project-based folders over \
file-type folders.
   - If duplicate folders are found, use `merge_folders`.

3. **File Classification**:
   - Move files into suitable topic/project folders.
   - If file content is unclear, call `get_file_information`.
   - If no suitable folder exists, use `create_folder`.
   - If a folder name is inaccurate, use `rename_folder`.

4. **Cleanup**:
   - Delete empty/unnecessary folders using `delete_folder`.
   - Before finishing, call `find_empty_folders`.

General Constraints:
- All operations are only for workspace_id: {workspace_id}.
- IMPORTANT: Every tool that modifies data requires a `workspace_id` parameter. \
Always pass {workspace_id}.
- You can only move/read files; do NOT modify or delete file content.
- Step-by-step: perform 3-5 operations per turn, observe results, then continue.
"""


def _format_files_detail(
    files_detail: list[change_log_service.FileChangeDetail],
) -> str:
    """增量提示中的变更文件列表文本."""
    if not files_detail:
        return "  (none)"
    return "\n".join(
        f'  - File "{f["name"]}" (ID: {f["id"]}) → currently at: {f["path"]}'
        for f in files_detail
    )


def _format_folders_detail(
    folders_detail: list[change_log_service.FolderChangeDetail],
) -> str:
    """增量提示中的变更文件夹列表文本."""
    if not folders_detail:
        return "  (none)"
    return "\n".join(
        f'  - Folder "{f["name"]}" (ID: {f["id"]}) → path: {f["path"]}'
        for f in folders_detail
    )


def _build_incremental_prompt(
    workspace_id: int,
    context: change_log_service.IncrementalContext,
) -> str:
    """增量整理提示: 仅聚焦变更项, 禁止全库扫描 (防 Token 浪费)."""
    summary_text = context["summary_text"] or "No summary."
    checkpoint_event_id = context["checkpoint_event_id"]
    target_event_id = context["target_event_id"]
    files_section = _format_files_detail(context["changed_files_detail"])
    folders_section = _format_folders_detail(context["changed_folders_detail"])

    return f"""
You are an intelligent file organization assistant performing INCREMENTAL organization.

Current Workspace ID: {workspace_id}
Event range: ({checkpoint_event_id}, {target_event_id}]

## What Changed (event log summary)
{summary_text}

## Files That Changed (current state)
{files_section}

## Folders That Changed (current state)
{folders_section}

## Your Task
1. **Focus ONLY on the items listed above.** Ensure each changed file is in the \
correct folder based on its name and content.
2. If a changed file's current location seems wrong, move it to a better folder.
3. If no suitable folder exists for a changed file, create one with `create_folder`.
4. If a changed folder has an inaccurate name, rename it.
5. After moving files, check if the source or target folders now violate the \
Single Content Principle (a folder must NOT contain both files and subfolders \
simultaneously).
6. Clean up any empty folders created as a side effect.

## Constraints
- **SCOPE RESTRICTION**: Do NOT scan or reorganize the entire file library. Only use \
`get_all_files` or `get_folder_tree` if you need to find a suitable destination \
for a changed file.
- If file content is unclear from the name alone, use `get_file_information` to \
read its description.
- All operations are only for workspace_id: {workspace_id}.
- IMPORTANT: Every tool that modifies data requires a `workspace_id` parameter. \
Always pass {workspace_id}.
- You can only move/read files; do NOT modify or delete file content.
- Step-by-step: perform 3-5 operations per turn, observe results, then continue.
"""


def _build_agent(model: BaseChatModel, tools: list[BaseTool]) -> StreamingAgent:
    """基于绑定的工具构建整理 Agent 图.

    SDK 的类型重载使未使用的 checkpointer 保持泛型 unknown。
    此处仅对消费的模型/工具调用与更新流进行投影,
    不引入任何运行时包装层或替代图实现。
    """
    factory = cast("AgentFactory", agents)
    return factory.create_agent(model, tools)


def _load_incremental_context(
    session: Session,
    workspace_id: int,
) -> change_log_service.IncrementalContext:
    """加载增量上下文信息, 失败时回退至全量扫描."""
    try:
        return change_log_service.load_incremental_context(
            session,
            workspace_id=workspace_id,
            max_events=MAX_INCREMENTAL_EVENTS,
        )
    except Exception as exc:
        logger.warning(
            "Load incremental context failed, fallback to full scan (error_type={})",
            type(exc).__name__,
        )
        return {
            "has_changes": True,
            "has_checkpoint": False,
            "overflow": True,
            "checkpoint_event_id": 0,
            "target_event_id": 0,
            "total_events": None,
            "events": [],
            "summary_text": "Incremental context unavailable; full scan required",
            "changed_file_ids": [],
            "changed_folder_ids": [],
            "action_breakdown": {},
            "changed_files_detail": [],
            "changed_folders_detail": [],
        }


def _prepare_plan(
    session: Session,
    workspace_id: int,
    results: list[str],
) -> OrganizePlan | None:
    """选择全量或增量 Prompt 以及检查点目标."""
    context = _load_incremental_context(session, workspace_id)

    if not context["has_changes"]:
        if context["has_checkpoint"]:
            change_log_service.update_checkpoint(
                session,
                workspace_id,
                context["target_event_id"],
                mark_full_scan=False,
            )
            results.append("未检测到上次整理后的文件变更, 已跳过本次整理。")
            return None

        # 无 checkpoint 时先做一次全量, 建立基线
        results.append("未检测到增量事件且无历史 checkpoint, 执行首次全量整理。")
        return OrganizePlan(
            messages=[HumanMessage(content=_build_full_prompt(workspace_id))],
            checkpoint_target_event_id=context["target_event_id"],
            full_scan_mode=True,
        )

    full_scan_mode = context["overflow"]
    if full_scan_mode:
        results.append(
            f"增量事件过多 ({context['total_events']} 条), 回退到全量整理。",
        )
        prompt = _build_full_prompt(workspace_id)
    else:
        results.append(
            f"进入增量整理模式, 处理事件区间 ({context['checkpoint_event_id']}, "
            f"{context['target_event_id']}]。",
        )
        prompt = _build_incremental_prompt(workspace_id, context)
    return OrganizePlan(
        messages=[HumanMessage(content=prompt)],
        checkpoint_target_event_id=context["target_event_id"],
        full_scan_mode=full_scan_mode,
    )


def _record_usage(message: AIMessage, state: StreamState) -> None:
    """累加单条模型消息的用量, 并确保只精确记录一次."""
    if not message.usage_metadata:
        return
    usage = message.usage_metadata
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)
    state.usage["input_tokens"] += input_tokens
    state.usage["output_tokens"] += output_tokens
    state.usage["total_tokens"] += total_tokens
    record_llm_usage(
        usage=TokenUsage(
            user_id=state.user_id,
            action="organize",
            model_name=state.model,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=total_tokens,
            query_summary="文件整理模型调用",
        ),
    )


def _stream_updates(
    agent: StreamingAgent,
    messages: list[BaseMessage],
    config: RunnableConfig,
    state: StreamState,
) -> None:
    """以 `updates` 模式消费单轮 Agent 输出, 记录消息与用量."""
    for chunk in agent.stream(
        {"messages": messages},
        config,
        stream_mode="updates",
    ):
        if "model" in chunk:
            # 模型节点产生的消息为 AIMessage 实例, 按 SDK 顺序排列。
            for model_message in chunk["model"]["messages"]:
                message = cast("AIMessage", model_message)
                state.results.append(f"Agent: {message.tool_calls}")
                state.messages.append(message)
                _record_usage(message, state)
        elif "tools" in chunk:
            for tool_message in chunk["tools"]["messages"]:
                state.results.append(f"Tool: {tool_message.content}")
                state.messages.append(tool_message)


def _validate_structure(workspace_id: int, results: list[str]) -> tuple[bool, str]:
    """校验单一内容原则与空目录约束, 并记录校验结果."""
    is_mixed_clean, mixed_info = check_mixed_folders_internal(workspace_id)
    is_empty_clean, empty_info = check_empty_folders_internal(workspace_id)

    if is_mixed_clean and is_empty_clean:
        results.append("校验通过: 所有文件夹均符合原则且无空文件夹。任务完成。")
        return True, ""

    error_details = ""
    if not is_mixed_clean:
        error_details += f"\n违反单一内容原则:\n{mixed_info}"
    if not is_empty_clean:
        error_details += f"\n发现空文件夹:\n{empty_info}"
    results.append(f"校验失败, 继续处理...{error_details}")
    return False, error_details


def _fix_message(error_details: str) -> HumanMessage:
    """构建要求 Agent 修复违规结构的跟进轮次 Prompt."""
    return HumanMessage(
        content=(
            "Organization is not yet complete. "
            "Please resolve the following issues:\n"
            f"{error_details}\nConfirm again once fixed."
        ),
    )


def _is_recursion_error(exc: Exception) -> bool:
    """从异常类型名称中识别 LangGraph 的递归上限异常."""
    exc_name = type(exc).__name__
    return "RecursionError" in exc_name or "GraphRecursionError" in exc_name


def _run_validation_loop(
    agent: StreamingAgent,
    state: StreamState,
    workspace_id: int,
    current_messages: list[BaseMessage],
) -> bool:
    """流式运行 Agent 直至目录树校验通过或重试次数耗尽."""
    for attempt in range(MAX_VALIDATION_RETRIES):
        config: RunnableConfig = {
            "configurable": {
                "thread_id": (
                    f"file_org_{workspace_id}_{int(time.time() * 1000)}_{attempt}"
                ),
            },
            "recursion_limit": RECURSION_LIMIT,
        }

        try:
            _stream_updates(agent, current_messages, config, state)
        except Exception as exc:
            if not _is_recursion_error(exc):
                raise
            state.results.append(
                f"Agent 达到最大步数限制 ({RECURSION_LIMIT}), 停止当前轮次。",
            )
            logger.warning(
                "Recursion limit reached for workspace {} on attempt {}",
                workspace_id,
                attempt,
            )

        is_clean, error_details = _validate_structure(workspace_id, state.results)
        if is_clean:
            return True

        state.messages.append(_fix_message(error_details))
        current_messages = list(state.messages)
    return False


def _commit_checkpoint(
    scope: OrganizeScope,
    workspace_id: int,
    plan: OrganizePlan,
    results: list[str],
) -> None:
    """为通过校验的运行持久化推进后的检查点."""
    target_event_id = plan.checkpoint_target_event_id
    if target_event_id is None:
        return
    session = SessionLocal()
    try:
        authorize_session(session, scope)
        change_log_service.update_checkpoint(
            session,
            workspace_id,
            target_event_id,
            mark_full_scan=plan.full_scan_mode,
        )
    finally:
        session.close()
    results.append(f"已推进整理 checkpoint 至事件 ID: {target_event_id}")


def organize_files(
    workspace_id: int,
    user_id: int,
    *,
    task_id: str | None = None,
    execution_token: str | None = None,
) -> tuple[dict[str, int], str]:
    """在构建模型及并行工具前绑定任务身份作用域."""
    scope = OrganizeScope(
        workspace_id=workspace_id,
        user_id=user_id,
        session_factory=SessionLocal,
        task_id=task_id,
        execution_token=execution_token,
    )
    with organize_scope(scope):
        return _organize_files(workspace_id, user_id, scope)


def _organize_files(
    workspace_id: int,
    user_id: int,
    scope: OrganizeScope,
) -> tuple[dict[str, int], str]:
    """在 Worker 线程中同步执行 ReAct 整理工作流."""
    url, key, model = get_llm_config()
    llm = ChatOpenAI(
        api_key=SecretStr(key),
        base_url=url,
        model=model,
        temperature=0,
    )
    agent = _build_agent(llm, bind_tools(_organize_tools(), scope))

    results: list[str] = []
    total_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    session = SessionLocal()
    try:
        authorize_session(session, scope)
        plan = _prepare_plan(session, workspace_id, results)
    finally:
        # 长耗时 Agent 跑之前先释放连接, 工具/校验各自开 session
        session.close()

    if plan is None:
        return total_usage, "\n\t".join(results)

    state = StreamState(
        user_id=user_id,
        model=model,
        results=results,
        messages=list(plan.messages),
        usage=total_usage,
    )
    validation_passed = _run_validation_loop(
        agent,
        state,
        workspace_id,
        plan.messages,
    )
    if validation_passed:
        _commit_checkpoint(scope, workspace_id, plan, results)
    if not validation_passed:
        message = "Organization failed final structure validation"
        raise RuntimeError(message)

    return total_usage, "\n\t".join(results)


def handle_organize_process(
    workspace_id: int,
    user_id: int,
    *,
    task_id: str | None = None,
    execution_token: str | None = None,
) -> dict[str, int]:
    """整理入口: 计时、记 Token、无论成败都写收件箱 (锁释放在 tasks 层)."""
    time_start = utc_now().astimezone(BEIJING_TZ).strftime(
        "%Y-%m-%d %H:%M:%S 北京时间",
    )
    timestamp_start = time.time()
    succeeded = True
    try:
        token_usage, results = organize_files(
            workspace_id,
            user_id,
            task_id=task_id,
            execution_token=execution_token,
        )
        content = (
            f"用时 {time.time() - timestamp_start:.2f} 秒\n"
            "Token 计费详情:\n"
            f"输入 Token: {token_usage.get('input_tokens')}\n"
            f"输出 Token: {token_usage.get('output_tokens')}\n"
            f"总 Token: {token_usage.get('total_tokens')}\n"
            "请注意 Token 消耗和第三方平台的额度。\n"
            "以下为模型调用的详细操作:\n"
            f"{results}"
        )
    except Exception as exc:
        logger.error("文件整理失败 (error_type={})", type(exc).__name__)
        succeeded = False
        token_usage = {}
        content = "文件整理任务执行失败, 请稍后重试或联系管理员。"

    session = SessionLocal()
    try:
        if task_id:
            task = assert_task_claim(
                session,
                task_id,
                execution_token,
                workspace_id,
                user_id,
            )
            task.state = "completed" if succeeded else "failed"
            task.finished_at = datetime.datetime.now(datetime.UTC)
        inbox_service.create_inbox_message(
            session,
            {
                "user_id": user_id,
                "title": f"您于 {time_start} 开始的文件整理任务已结束",
                "content": content,
            },
        )
    finally:
        session.close()
    return token_usage
