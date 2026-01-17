import datetime
import logging
import time
import sys
import os

# Add the current directory to sys.path to ensure 'app' module can be found
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from langchain.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent
from sqlalchemy.orm import sessionmaker

from app.extensions import db, redis_client
from app.models.file import File
from app.models.folder import Folder
from app.services import sys_dict_service

# 配置日志
logger = logging.getLogger(__name__)


def get_llm_config():
    url = sys_dict_service.get_sys_dict_by_key("chat_api_url").value
    key = sys_dict_service.get_sys_dict_by_key("chat_api_key").value
    model = sys_dict_service.get_sys_dict_by_key("chat_api_model").value
    return url, key, model


def get_session():
    """Create a new independent session for tool execution"""
    SessionLocal = sessionmaker(bind=db.engine)
    return SessionLocal()


def clear_user_cache(user_id):
    """Clear user related caches"""
    try:
        # Clear folder cache
        redis_client.delete(f"user:folders:{user_id}")
        # Clear search cache
        keys = redis_client.keys(f"search:*:{user_id}:*")
        if keys:
            redis_client.delete(*keys)
    except Exception as e:
        logger.error(f"Error clearing cache for user {user_id}: {e}")


@tool
def get_all_files(user_id: int) -> str:
    """
    获取指定用户的所有文件列表。
    """
    session = get_session()
    try:
        files = session.query(File).filter_by(uploader_id=user_id).all()
        if not files:
            return "没有找到文件。"

        file_list = []
        for f in files:
            # 包含 Parent Folder ID 以便 LLM 知道文件当前位置
            file_list.append(f"ID: {f.id}, Name: {f.name}, Parent Folder ID: {f.parent_id}")
        return "\n".join(file_list)
    except Exception as e:
        return f"获取文件列表失败: {str(e)}"
    finally:
        session.close()


@tool
def get_folder_tree(user_id: int) -> str:
    """
    获取指定用户的文件夹树结构。
    """
    session = get_session()
    try:
        folders = session.query(Folder).filter_by(user_id=user_id).all()
        paths = []
        for folder in folders:
            # 使用简单的 ID 和 Parent ID 表示树结构，避免复杂的递归查询
            paths.append(f"ID: {folder.id}, Name: {folder.name}, Parent ID: {folder.parent_id}")
        return "\n".join(paths)
    except Exception as e:
        return f"获取文件夹结构失败: {str(e)}"
    finally:
        session.close()


@tool
def create_folder(name: str, parent_id: int, user_id: int) -> str:
    """
    创建新文件夹。
    """
    session = get_session()
    try:
        # 处理 parent_id 为 0 或 None 的情况
        pid = parent_id if parent_id and parent_id != 0 else None

        # 检查文件夹是否已存在
        existing_folder = session.query(Folder).filter_by(
            name=name,
            user_id=user_id,
            parent_id=pid
        ).first()

        if existing_folder:
            return f"文件夹 '{name}' 已存在 (ID: {existing_folder.id})"

        new_folder = Folder(
            name=name,
            user_id=user_id,
            parent_id=pid
        )
        session.add(new_folder)
        session.commit()

        session.refresh(new_folder)
        folder_id = new_folder.id

        clear_user_cache(user_id)
        return f"已创建文件夹 '{name}' (ID: {folder_id})"
    except Exception as e:
        session.rollback()
        return f"创建文件夹失败: {str(e)}"
    finally:
        session.close()


@tool
def rename_folder(folder_id: int, new_name: str) -> str:
    """
    重命名文件夹。
    """
    session = get_session()
    try:
        folder = session.get(Folder, folder_id)
        if not folder:
            return f"文件夹 {folder_id} 不存在"

        # 检查同级目录下是否存在同名文件夹
        existing_folder = session.query(Folder).filter_by(
            name=new_name,
            parent_id=folder.parent_id,
            user_id=folder.user_id
        ).first()

        if existing_folder and existing_folder.id != folder_id:
            return f"重命名失败：文件夹 '{new_name}' 已存在 (ID: {existing_folder.id})。请考虑将文件移动到该文件夹。"

        folder.name = new_name
        session.commit()

        clear_user_cache(folder.user_id)
        return f"已将文件夹 {folder_id} 重命名为 '{new_name}'"
    except Exception as e:
        session.rollback()
        return f"重命名失败: {str(e)}"
    finally:
        session.close()


@tool
def move_file(file_id: int, target_folder_id: int) -> str:
    """
    将文件移动到目标文件夹。
    """
    session = get_session()
    try:
        file = session.get(File, file_id)
        if not file:
            return f"文件 {file_id} 不存在"

        # 处理 target_folder_id 为 0 的情况（移动到根目录）
        target_id = target_folder_id if target_folder_id and target_folder_id != 0 else None

        file.parent_id = target_id
        session.commit()

        clear_user_cache(file.uploader_id)
        return f"已将文件 {file_id} 移动到文件夹 {target_folder_id}"
    except Exception as e:
        session.rollback()
        return f"移动文件失败: {str(e)}"
    finally:
        session.close()


@tool
def get_file_information(file_id: int) -> str:
    """
    获取文件的详细信息（如描述、类型、大小等）。
    """
    session = get_session()
    try:
        file = session.get(File, file_id)
        if not file:
            return f"文件 {file_id} 不存在"

        # 返回精简信息以节省 Token
        description = file.description or ""
        if len(description) > 200:
            description = description[:200] + "..."

        info = {
            "id": file.id,
            "name": file.name,
            "parent_id": file.parent_id,
            "size": file.file_size,
            "mime_type": file.mime_type,
            "description": description
        }
        return str(info)
    except Exception as e:
        return f"获取文件信息失败: {str(e)}"
    finally:
        session.close()


@tool
def delete_folder(folder_id: int) -> str:
    """
    删除文件夹。注意：只能删除空文件夹。
    """
    session = get_session()
    try:
        folder = session.get(Folder, folder_id)
        if not folder:
            return f"文件夹 {folder_id} 不存在"

        # 检查子文件夹
        subfolders_count = session.query(Folder).filter_by(parent_id=folder.id).count()
        if subfolders_count > 0:
            return f"删除失败：文件夹 {folder_id} 不为空（包含子文件夹）。"

        # 检查文件
        files_count = session.query(File).filter_by(parent_id=folder.id).count()
        if files_count > 0:
            return f"删除失败：文件夹 {folder_id} 不为空（包含文件）。"

        session.delete(folder)
        session.commit()

        clear_user_cache(folder.user_id)
        return f"已删除文件夹 {folder_id}"
    except Exception as e:
        session.rollback()
        return f"删除失败：{str(e)}"
    finally:
        session.close()


@tool
def merge_folders(source_folder_id: int, target_folder_id: int) -> str:
    """
    将源文件夹的所有内容（文件和子文件夹）移动到目标文件夹，并删除源文件夹。
    用于合并两个同名或相似的文件夹。
    """
    session = get_session()
    try:
        source = session.get(Folder, source_folder_id)
        target = session.get(Folder, target_folder_id)

        if not source or not target:
            return "源文件夹或目标文件夹不存在"

        if source.id == target.id:
            return "不能合并同一个文件夹"

        # 检查目标文件夹是否为源文件夹的子文件夹，防止循环
        current = target
        while current and current.parent_id:
            if current.parent_id == source.id:
                return "无法合并：目标文件夹是源文件夹的子文件夹"
            current = session.get(Folder, current.parent_id)

        # 移动文件
        files = session.query(File).filter_by(parent_id=source.id).all()
        for f in files:
            f.parent_id = target.id

        # 移动子文件夹
        subfolders = session.query(Folder).filter_by(parent_id=source.id).all()
        for sub in subfolders:
            if sub.id != target.id:
                sub.parent_id = target.id

        session.delete(source)
        session.commit()

        clear_user_cache(source.user_id)
        return f"已将文件夹 {source_folder_id} 合并到 {target_folder_id}"
    except Exception as e:
        session.rollback()
        return f"合并失败: {str(e)}"
    finally:
        session.close()


@tool
def find_duplicate_folders(user_id: int) -> str:
    """
    查找具有相同名称和相同父文件夹的重复文件夹。
    """
    session = get_session()
    try:
        # 获取所有文件夹
        folders = session.query(Folder).filter_by(user_id=user_id).all()

        # 按 (name, parent_id) 分组
        grouped = {}
        for f in folders:
            key = (f.name, f.parent_id)
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(f)

        # 找出重复项
        duplicates = []
        for (name, parent_id), group in grouped.items():
            if len(group) > 1:
                ids = [str(f.id) for f in group]
                duplicates.append(f"Name: '{name}', Parent ID: {parent_id}, IDs: {', '.join(ids)}")

        if not duplicates:
            return "未发现重复文件夹。"

        return "发现以下重复文件夹：\n" + "\n".join(duplicates)
    except Exception as e:
        return f"查找重复文件夹失败: {str(e)}"
    finally:
        session.close()


@tool
def move_folder(folder_id: int, target_folder_id: int) -> str:
    """
    将文件夹移动到目标文件夹中（作为子文件夹）。
    """
    session = get_session()
    try:
        folder = session.get(Folder, folder_id)

        if not folder:
            return f"文件夹 {folder_id} 不存在"

        if folder.id == target_folder_id:
            return "不能将文件夹移动到自身"

        # 处理移动到根目录
        if not target_folder_id or target_folder_id == 0:
            folder.parent_id = None
            session.commit()
            clear_user_cache(folder.user_id)
            return f"已将文件夹 {folder_id} 移动到根目录"

        target = session.get(Folder, target_folder_id)
        if not target:
            return f"目标文件夹 {target_folder_id} 不存在"

        # 检查循环引用：目标文件夹不能是源文件夹的子文件夹
        current = target
        while current:
            if current.id == folder.id:
                return "无法移动：目标文件夹是源文件夹的子文件夹"
            if current.parent_id:
                current = session.get(Folder, current.parent_id)
            else:
                break

        folder.parent_id = target.id
        session.commit()

        clear_user_cache(folder.user_id)
        return f"已将文件夹 {folder_id} 移动到 {target.name} (ID: {target.id})"
    except Exception as e:
        session.rollback()
        return f"移动失败: {str(e)}"
    finally:
        session.close()


def organize_files(user_id: int):
    url, key, model = get_llm_config()
    llm = ChatOpenAI(
        api_key=key,
        base_url=url,
        model=model,
        temperature=0
    )

    tools = [get_all_files, get_folder_tree, create_folder, rename_folder, move_file, get_file_information,
             delete_folder, merge_folders, find_duplicate_folders, move_folder]
    memory = InMemorySaver()
    agent_executor = create_react_agent(llm, tools, checkpointer=memory)

    prompt = f"""
    你是一个智能文件整理助手。你的目标是让用户的文件系统井井有条。
    
    当前用户 ID: {user_id}
    
    你的任务包含两个部分：
    1. **获取信息**：
       - 调用 `get_all_files` 获取所有文件列表。
       - 调用 `get_folder_tree` 查看当前文件夹结构。
       - 调用 `find_duplicate_folders` 检查重复项。

    2. **优化文件夹结构**：
       - **关键**：检查根目录下是否有多个属于同一大类的文件夹。例如，如果你看到 "排序算法"、"位运算"、"算法题目" 等，**必须**创建一个名为 "算法" 的父文件夹，然后使用 `move_folder` 将这些子文件夹移动进去。不要让根目录充斥着琐碎的分类。
       - **避免按扩展名分类**：如果发现现有的文件夹是按文件类型命名的（如 "Word文档"、"图片"），请尝试根据文件内容将其重组到更有意义的主题文件夹中，或者至少不要再创建此类新文件夹，除非文件确实杂乱无章。
       - 如果发现重复文件夹，使用 `merge_folders` 合并。
    
    3. **文件归类**
       - 结构优化后，将文件移动到合适的文件夹。
       - **重要原则**：优先按**内容主题**或**项目**分类，而不是按文件格式分类。例如，将 "ProjectA_Report.pdf" 和 "ProjectA_Data.xlsx" 放在 "Project A" 文件夹中，而不是分别放在 "PDF" 和 "Excel" 文件夹中。仅在无法识别主题时才按格式归档。
       - 如果文件内容不明确，调用 `get_file_information`。
       - 如果没有合适的文件夹，使用 `create_folder` 创建。
       - 如果文件夹名称不准确，使用 `rename_folder`。
    
    4. **清理**
       - 删除空的或不再需要的文件夹 (`delete_folder`)。
    
    通用限制：
    - 所有操作仅针对 user_id: {user_id}。
    - 只能移动/读取文件，不可修改/删除文件内容。
    - **分步执行**：不要一次性输出所有操作。每次思考后执行 3-5 个操作，观察结果后再继续。
    """

    config = {"configurable": {"thread_id": f"file_org_{user_id}"}, "recursion_limit": 120}

    results = []
    total_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    for chunk in agent_executor.stream({"messages": [("user", prompt)]}, config):
        if "agent" in chunk:
            message = chunk['agent']['messages'][0]
            results.append(f"Agent: {message.tool_calls}")

            # 统计 Token 使用情况
            if hasattr(message, 'usage_metadata') and message.usage_metadata:
                usage = message.usage_metadata
                total_usage["input_tokens"] += usage.get("input_tokens", 0)
                total_usage["output_tokens"] += usage.get("output_tokens", 0)
                total_usage["total_tokens"] += usage.get("total_tokens", 0)

        elif "tools" in chunk:
            message = chunk['tools']['messages'][0]
            results.append(f"Tool: {chunk['tools']['messages'][0].content}")

    return total_usage, "\n\t".join(results)


def handle_organize_process(user_id: int):
    time_start = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timestamp_start = time.time()
    try:
        token_usage, results = organize_files(user_id)
        content = (
            f"用时{time.time() - timestamp_start:.2f}秒\n"
            "token 计费\n"
            f"输入token ：{token_usage.get('input_tokens')}\n"
            f"输出token ：{token_usage.get('output_tokens')}\n"
            f"总token ：{token_usage.get('total_tokens')}\n"
            "请注意token消耗和在第三方平台的额度\n"
            "以下为模型调用的详细操作\n"
            f"{results}"
        )
    except Exception as e:
        logger.exception("Organize files failed")
        token_usage = {}
        content = f"文件整理任务执行失败: {str(e)}"

    # 发送信息给用户
    from app.services import inbox_service
    inbox_service.create_inbox_message({
        'user_id': user_id,
        'title': f"您于{time_start}开始的文件整理任务已结束",
        'content': content
    })
    return token_usage
