import datetime
import logging
import os
import sys
import time

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

from app.services import sys_dict_service
from .organize_tools import (
    get_all_files, get_folder_tree, create_folder, rename_folder, move_file,
    get_file_information, delete_folder, merge_folders, find_duplicate_folders,
    move_folder, find_mixed_content_folders, check_mixed_folders_internal,
    find_empty_folders, check_empty_folders_internal
)

# 配置日志
logger = logging.getLogger(__name__)


def get_llm_config():
    """从系统字典服务获取 LLM 配置。"""
    url = sys_dict_service.get_sys_dict_by_key_sync("chat_api_url").value
    key = sys_dict_service.get_sys_dict_by_key_sync("chat_api_key").value
    model = sys_dict_service.get_sys_dict_by_key_sync("chat_api_model").value
    return url, key, model


def organize_files(user_id: int):
    """
    使用 ReAct 代理进行文件整理的核心逻辑。
    包含一个校验循环，以确保满足所有整理原则。
    """
    url, key, model = get_llm_config()
    llm = ChatOpenAI(
        api_key=key,
        base_url=url,
        model=model,
        temperature=0
    )

    # 定义代理可用的工具
    tools = [get_all_files, get_folder_tree, create_folder, rename_folder, move_file, get_file_information,
             delete_folder, merge_folders, find_duplicate_folders, move_folder, find_mixed_content_folders,
             find_empty_folders]
    memory = InMemorySaver()
    agent_executor = create_react_agent(llm, tools, checkpointer=memory)

    # 使用英文 Prompt 以获得更好的 LLM 性能
    prompt = f"""
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
       - **Single Content Principle (CRITICAL)**: A folder must NOT contain both files and subfolders simultaneously. If this occurs, you MUST resolve it by creating new folders and moving items.
       - **Root Directory Cleanup**: Check if there are multiple folders of the same category in the root. For example, if you see "Sorting", "Bit Manipulation", and "Algorithms", you MUST create a parent folder named "Algorithms" and move them inside using `move_folder`.
       - **Avoid Extension-based Categorization**: If existing folders are named after file types (e.g., "Word Docs", "Images"), try to reorganize them into meaningful topic-based folders.
       - If duplicate folders are found, use `merge_folders` to combine them.
    
    3. **File Classification**:
       - After optimizing the structure, move files into appropriate folders.
       - **Principle**: Prioritize classification by **Content Topic** or **Project** rather than file format.
       - If file content is unclear, call `get_file_information`.
       - If no suitable folder exists, use `create_folder`.
       - If a folder name is inaccurate, use `rename_folder`.
    
    4. **Cleanup**:
       - You MUST delete all empty or unnecessary folders using `delete_folder`. Before finishing, call `find_empty_folders` to ensure none are left.
    
    General Constraints:
    - All operations are only for user_id: {user_id}.
    - You can only move/read files; do NOT modify or delete file content.
    - **Step-by-step Execution**: Do not output all operations at once. Perform 3-5 operations per turn, observe the results, and then continue.
    """

    config = {"configurable": {"thread_id": f"file_org_{user_id}"}, "recursion_limit": 120}

    results = []
    total_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    current_messages = [("user", prompt)]

    # 校验循环：确保满足原则，如果校验失败，最多允许重试 3 次
    for attempt in range(3):
        for chunk in agent_executor.stream({"messages": current_messages}, config):
            if "agent" in chunk:
                message = chunk['agent']['messages'][0]
                results.append(f"Agent: {message.tool_calls}")

                if hasattr(message, 'usage_metadata') and message.usage_metadata:
                    usage = message.usage_metadata
                    total_usage["input_tokens"] += usage.get("input_tokens", 0)
                    total_usage["output_tokens"] += usage.get("output_tokens", 0)
                    total_usage["total_tokens"] += usage.get("total_tokens", 0)

            elif "tools" in chunk:
                results.append(f"Tool: {chunk['tools']['messages'][0].content}")

        # 对混合内容和空文件夹进行硬校验
        mixed_info = check_mixed_folders_internal(user_id)
        empty_info = check_empty_folders_internal(user_id)

        is_mixed_clean = "未发现" in mixed_info or "not found" in mixed_info.lower()
        is_empty_clean = "未发现" in empty_info or "not found" in empty_info.lower()

        if is_mixed_clean and is_empty_clean:
            results.append("校验通过：所有文件夹均符合原则且无空文件夹。任务完成。")
            break
        else:
            error_details = ""
            if not is_mixed_clean:
                error_details += f"\n违反单一内容原则：\n{mixed_info}"
            if not is_empty_clean:
                error_details += f"\n发现空文件夹：\n{empty_info}"

            results.append(f"校验失败，继续处理...{error_details}")
            # 使用英文向代理重新发送发现的具体问题
            current_messages = [("user",
                                 f"Organization is not yet complete. Please resolve the following issues:\n{error_details}\nConfirm again once fixed.")]

    return total_usage, "\n\t".join(results)


def handle_organize_process(user_id: int):
    """整理流程的入口点，处理计时、Token 使用情况和用户通知。"""
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
    except Exception as e:
        logger.exception("文件整理失败")
        token_usage = {}
        content = f"文件整理任务执行失败: {str(e)}"

    # 向用户发送通知
    from app.services import inbox_service
    inbox_service.create_inbox_message({
        'user_id': user_id,
        'title': f"您于 {time_start} 开始的文件整理任务已结束",
        'content': content
    })
    return token_usage
