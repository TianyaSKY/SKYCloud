import datetime
import logging
import os
import sys
import time

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from app.services import change_log_service
from app.services.model_config import get_chat_model_config
from .organize_tools import (
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

logger = logging.getLogger(__name__)

MAX_INCREMENTAL_EVENTS = 200
MAX_VALIDATION_RETRIES = 2
RECURSION_LIMIT = 40


def get_llm_config():
    """从环境变量获取 LLM 配置。"""
    config = get_chat_model_config()
    return config["api"], config["key"], config["model"]


def _build_full_prompt(user_id: int) -> str:
    return f"""
You are an intelligent file organization assistant. Your goal is to keep the user's file system well-organized.

Current User ID: {user_id}

Your task consists of the following parts:
1. **Information Gathering**:
   - Call `get_all_files` to get the list of all files.
   - Call `get_folder_tree` to view the current folder structure.
   - Call `find_duplicate_folders` to check for duplicates.
   - Call `find_mixed_content_folders` to check for folders violating the "Single Content Principle".
   - Call `find_empty_folders` to check for empty folders.

2. **Structure Optimization**:
   - **Single Content Principle (CRITICAL)**: A folder must NOT contain both files and subfolders simultaneously.
   - **Root Directory Cleanup**: If multiple sibling folders belong to one same category, create a parent category folder and move them with `move_folder`.
   - **Avoid Extension-based Categorization**: Prefer topic/project-based folders over file-type folders.
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
- All operations are only for user_id: {user_id}.
- You can only move/read files; do NOT modify or delete file content.
- Step-by-step: perform 3-5 operations per turn, observe results, then continue.
"""


def _build_incremental_prompt(user_id: int, context: dict) -> str:
    changed_file_ids = context.get("changed_file_ids") or []
    changed_folder_ids = context.get("changed_folder_ids") or []
    summary_text = context.get("summary_text") or "No summary."
    checkpoint_event_id = context.get("checkpoint_event_id", 0)
    target_event_id = context.get("target_event_id", 0)

    return f"""
You are an intelligent file organization assistant. Your goal is to keep the user's file system well-organized.

Current User ID: {user_id}
Mode: Incremental organization based on change events.
Event range to process: ({checkpoint_event_id}, {target_event_id}]

Change Summary:
{summary_text}

Priority Scope:
- Changed file IDs: {changed_file_ids}
- Changed folder IDs: {changed_folder_ids}

Execution strategy:
1. Start from changed folders/files first. Avoid scanning the whole library unless absolutely required.
2. Use `get_file_information` only when classification is unclear.
3. Keep the Single Content Principle: folders should not contain both files and subfolders.
4. If duplicates are found around the changed scope, merge with `merge_folders`.
5. Clean up empty folders with `delete_folder`.

General Constraints:
- All operations are only for user_id: {user_id}.
- You can only move/read files; do NOT modify or delete file content.
- Step-by-step: perform 3-5 operations per turn, observe results, then continue.
"""


def organize_files(user_id: int):
    """
    使用 ReAct 代理进行文件整理。
    优先增量整理：仅处理上次 checkpoint 之后的变化；变化过多时回退到全量整理。
    """
    url, key, model = get_llm_config()
    llm = ChatOpenAI(
        api_key=key,
        base_url=url,
        model=model,
        temperature=0,
    )

    tools = [
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
    agent_executor = create_react_agent(llm, tools)

    results: list[str] = []
    total_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    checkpoint_target_event_id: int | None = None
    full_scan_mode = False

    try:
        incremental_context = change_log_service.load_incremental_context(
            user_id=user_id, max_events=MAX_INCREMENTAL_EVENTS
        )
    except Exception as exc:
        logger.warning(f"Load incremental context failed, fallback to full scan: {exc}")
        incremental_context = {
            "has_changes": True,
            "overflow": True,
            "summary_text": f"incremental_context_error: {exc}",
            "target_event_id": 0,
        }

    if not incremental_context.get("has_changes"):
        if incremental_context.get("has_checkpoint"):
            target_event_id = int(incremental_context.get("target_event_id") or 0)
            change_log_service.update_checkpoint(
                user_id, target_event_id, mark_full_scan=False
            )
            results.append("未检测到上次整理后的文件变更，已跳过本次整理。")
            return total_usage, "\n\t".join(results)

        # 新功能上线前已有文件但尚无 checkpoint/事件日志时，首次仍执行一次全量整理
        results.append("未检测到增量事件且无历史 checkpoint，执行首次全量整理。")
        checkpoint_target_event_id = int(incremental_context.get("target_event_id") or 0)
        full_scan_mode = True
        prompt = _build_full_prompt(user_id)
        current_messages = [("user", prompt)]
    else:
        checkpoint_target_event_id = int(incremental_context.get("target_event_id") or 0)
        full_scan_mode = bool(incremental_context.get("overflow"))

        if full_scan_mode:
            results.append(
                f"增量事件过多（{incremental_context.get('total_events')} 条），回退到全量整理。"
            )
            prompt = _build_full_prompt(user_id)
        else:
            results.append(
                f"进入增量整理模式，处理事件区间 ({incremental_context.get('checkpoint_event_id')}, "
                f"{incremental_context.get('target_event_id')}]。"
            )
            prompt = _build_incremental_prompt(user_id, incremental_context)

        current_messages = [("user", prompt)]
    validation_passed = False

    for attempt in range(MAX_VALIDATION_RETRIES):
        config = {
            "configurable": {"thread_id": f"file_org_{user_id}_{int(time.time() * 1000)}_{attempt}"},
            "recursion_limit": RECURSION_LIMIT,
        }

        for chunk in agent_executor.stream({"messages": current_messages}, config):
            if "agent" in chunk:
                message = chunk["agent"]["messages"][0]
                results.append(f"Agent: {message.tool_calls}")

                if hasattr(message, "usage_metadata") and message.usage_metadata:
                    usage = message.usage_metadata
                    total_usage["input_tokens"] += usage.get("input_tokens", 0)
                    total_usage["output_tokens"] += usage.get("output_tokens", 0)
                    total_usage["total_tokens"] += usage.get("total_tokens", 0)

            elif "tools" in chunk:
                results.append(f"Tool: {chunk['tools']['messages'][0].content}")

        mixed_info = check_mixed_folders_internal(user_id)
        empty_info = check_empty_folders_internal(user_id)

        is_mixed_clean = "未发现" in mixed_info or "not found" in mixed_info.lower()
        is_empty_clean = "未发现" in empty_info or "not found" in empty_info.lower()

        if is_mixed_clean and is_empty_clean:
            validation_passed = True
            results.append("校验通过：所有文件夹均符合原则且无空文件夹。任务完成。")
            break

        error_details = ""
        if not is_mixed_clean:
            error_details += f"\n违反单一内容原则：\n{mixed_info}"
        if not is_empty_clean:
            error_details += f"\n发现空文件夹：\n{empty_info}"

        results.append(f"校验失败，继续处理...{error_details}")
        current_messages = [
            (
                "user",
                "Organization is not yet complete. Please resolve the following issues:\n"
                f"{error_details}\nConfirm again once fixed.",
            )
        ]

    if validation_passed and checkpoint_target_event_id is not None:
        change_log_service.update_checkpoint(
            user_id, checkpoint_target_event_id, mark_full_scan=full_scan_mode
        )
        results.append(f"已推进整理 checkpoint 至事件 ID: {checkpoint_target_event_id}")
    elif checkpoint_target_event_id is not None:
        results.append("整理未通过最终校验，本次未推进 checkpoint。")

    return total_usage, "\n\t".join(results)


def handle_organize_process(user_id: int):
    """整理流程入口：处理计时、Token 使用和用户通知。"""
    time_start = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timestamp_start = time.time()
    try:
        token_usage, results = organize_files(user_id)
        content = (
            f"用时 {time.time() - timestamp_start:.2f} 秒\n"
            "Token 计费详情：\n"
            f"输入 Token：{token_usage.get('input_tokens')}\n"
            f"输出 Token：{token_usage.get('output_tokens')}\n"
            f"总 Token：{token_usage.get('total_tokens')}\n"
            "请注意 Token 消耗和第三方平台的额度。\n"
            "以下为模型调用的详细操作：\n"
            f"{results}"
        )
    except Exception as exc:
        logger.exception("文件整理失败")
        token_usage = {}
        content = f"文件整理任务执行失败: {str(exc)}"

    from app.services import inbox_service

    inbox_service.create_inbox_message(
        {
            "user_id": user_id,
            "title": f"您于 {time_start} 开始的文件整理任务已结束",
            "content": content,
        }
    )
    return token_usage
