"""文件描述生成 (Worker 适配): 多模态 VL / Chat 产出中文描述供 embedding.

业务边界: 只负责「文件 -> 描述文本」; 索引落库与向量写入在 indexing_handler.
Worker LLM 调用统一走同步 sync_client.chat_completion, 便于记 Token.
"""

import base64
import mimetypes
import tempfile
import threading
from pathlib import Path

import cv2
import fitz
import pandas as pd
from docx import Document
from loguru import logger
from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
)

from app.config.worker import get_worker_config
from app.infra.indexing.chunking import TextSection
from app.infra.llm import sync_client

from .converter import (
    convert_office_to_pdf,
    convert_pdf_to_images,
    extract_video_frames,
)

# 走纯文本 Chat 路径的扩展名 (比 VL 更便宜)
TEXT_EXTENSIONS = {
    ".md",
    ".cpp",
    ".h",
    ".c",
    ".py",
    ".txt",
    ".java",
    ".go",
    ".rs",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".json",
    ".yaml",
    ".yml",
    ".sql",
    ".sh",
    ".bat",
}

# 走 VL 视觉路径的格式分类
DOCUMENT_EXTENSIONS = {
    ".docx",
    ".doc",
    ".xlsx",
    ".xls",
    ".pptx",
    ".ppt",
    ".pdf",
}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg"}
TABLE_EXTENSIONS = {".csv", ".xlsx", ".xls"}

MAX_VISUAL_PAGES = 20
MAX_VISUAL_FRAMES = 20
MAX_CONTENT_CHARS = 8000

TEXT_PROMPT = (
    "你是一个专业的文件分析助手。请根据以下文本内容, 生成准确、专业、简洁的描述。"
    "描述应包含: 核心主题、主要内容摘要和关键信息点。"
    "请直接用中文输出描述文本, 不要添加任何前缀或解释性文字。不要使用 Markdown 语法。"
)

VISUAL_PROMPT = (
    "你是一个专业的文件分析助手。请仔细观察提供的文件内容"
    "(可能是文档截图、视频关键帧或图片), 生成准确、专业、简洁的描述。"
    "描述应包含: 核心主题、主要内容摘要和关键信息点。"
    "请直接用中文输出描述文本, 不要添加任何前缀或解释性文字。不要使用 Markdown 语法。"
    "不需要描述图片的顺序。"
)

# 此处为本地工具资源限流而非任务并发控制,
# 用于防止多个索引线程同时处理大文件时耗尽转换工具资源。
_worker_cfg = get_worker_config()
_document_conversion_semaphore = threading.Semaphore(
    _worker_cfg.document_conversion_workers,
)
_video_processing_semaphore = threading.Semaphore(
    _worker_cfg.video_processing_workers,
)


def image_to_base64(image_path: str) -> str:
    """图片文件 -> Base64 Data URI, 供 VL image_url 字段."""
    mime_type, _ = mimetypes.guess_type(image_path)
    if not mime_type:
        mime_type = "image/jpeg"

    with Path(image_path).open("rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode("utf-8")

    return f"data:{mime_type};base64,{encoded_string}"


def _document_uris(local_path: str, ext: str, workdir: str) -> list[str]:
    """将 Office 文档或 PDF 转换为按页切分的 JPEG Data URI 列表."""
    with _document_conversion_semaphore:
        target_pdf = (
            local_path if ext == ".pdf" else convert_office_to_pdf(local_path, workdir)
        )
        images = convert_pdf_to_images(target_pdf, workdir, max_pages=MAX_VISUAL_PAGES)
    return [uri for uri in map(image_to_base64, images) if uri]


def _video_uris(local_path: str, workdir: str) -> list[str]:
    """提取等间隔视频抽帧并转换为 JPEG Data URI 列表."""
    with _video_processing_semaphore:
        frames = extract_video_frames(
            local_path,
            workdir,
            frame_count=MAX_VISUAL_FRAMES,
        )
    return [uri for uri in map(image_to_base64, frames) if uri]


def _visual_uris(local_path: str, ext: str, workdir: str) -> list[str]:
    """将文件扩展名家族映射为对应的 VL 多模态入参载荷."""
    if ext in DOCUMENT_EXTENSIONS:
        return _document_uris(local_path, ext, workdir)
    if ext in VIDEO_EXTENSIONS:
        return _video_uris(local_path, workdir)
    if ext in IMAGE_EXTENSIONS:
        return [image_to_base64(local_path)]
    if ext in TEXT_EXTENSIONS:
        return []  # 文本路径不需要视觉帧
    message = f"Unsupported file type: {ext}"
    raise ValueError(message)


def _get_visual_urls(local_path: str) -> list[str]:
    """将文件转换为 VL 可识别的图像 URI 列表; 音频格式将被拒绝."""
    ext = Path(local_path).suffix.lower()

    if ext in AUDIO_EXTENSIONS:
        message = "暂不支持音频文件的解析!!!"
        raise ValueError(message)

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            return _visual_uris(local_path, ext, tmpdir)
        except Exception as e:
            logger.error(
                "Visual content processing failed (error_type={})",
                type(e).__name__,
            )
            raise


def _require_answer(answer: str | None) -> str:
    """当模型返回空答案时抛出稳定的运行时异常."""
    if not answer:
        message = "未知错误 无法生成文件描述"
        raise RuntimeError(message)
    return answer


def _truncate_content(text_content: str) -> str:
    """截断 Prompt 载荷内容, 避免撑爆模型上下文窗口."""
    if len(text_content) > MAX_CONTENT_CHARS:
        return text_content[:MAX_CONTENT_CHARS] + "\n...(内容已截断)"
    return text_content


def _generate_text_description(
    local_path: str,
    config: dict[str, str],
    user_id: int = 0,
) -> str:
    """使用纯文本 Chat 模型生成描述 (调用成本低于 VL 多模态)."""
    text_content = _extract_text_content(local_path)
    if not text_content.strip():
        return "空文件"

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": TEXT_PROMPT},
        {"role": "user", "content": _truncate_content(text_content)},
    ]

    try:
        response = sync_client.chat_completion(
            messages=messages,
            config=config,
            user_id=user_id,
            action="describe_text",
            query_summary=Path(local_path).name,
        )
        answer = _require_answer(response.choices[0].message.content)
        logger.info(f"[Chat] Text description generated for {local_path}")
    except Exception as e:
        logger.error("Chat LLM API call failed (error_type={})", type(e).__name__)
        raise

    return answer


def generate_file_description(
    local_path: str,
    config: dict[str, str],
    chat_config: dict[str, str] | None = None,
    user_id: int = 0,
) -> str:
    """生成中文文件描述: 文本走 Chat, 其余走 VL.

    :param local_path: 本地绝对路径
    :param config: VL 模型配置 (api/key/model)
    :param chat_config: 可选 Chat 配置; 缺省回退到 config
    :param user_id: 用于 Token 记账
    """
    ext = Path(local_path).suffix.lower()

    if ext in TEXT_EXTENSIONS:
        return _generate_text_description(
            local_path,
            chat_config or config,
            user_id=user_id,
        )

    # 当未配置可用视觉语言模型时优雅降级:
    # - 可抽取文本的文档/媒体文件: 降级为纯文本抽取与 Chat 模型描述
    # - 纯视觉图片等无法提取文本的文件: 保留客观文件名与元数据, 绝不伪造虚假分析结果
    if not config.get("key"):
        extracted = _extract_text_content(local_path)
        if extracted.strip():
            chat_target = chat_config or config
            if chat_target.get("key"):
                return _generate_text_description(
                    local_path,
                    chat_target,
                    user_id=user_id,
                )
            return f"文件内容摘要: {_truncate_content(extracted.strip())}"
        name = Path(local_path).name
        return f"纯视觉文件: {name} (未配置多模态视觉模型, 无法提取图像内容)"

    visual_contents = _get_visual_urls(local_path)
    if not visual_contents:
        logger.info(f"No visual content for {local_path}")

    content: list[ChatCompletionContentPartParam] = [
        ChatCompletionContentPartTextParam(type="text", text=VISUAL_PROMPT),
    ]
    content.extend(
        ChatCompletionContentPartImageParam(
            type="image_url",
            image_url={"url": uri},
        )
        for uri in visual_contents
    )
    # 附带可抽取文本, 补 VL 对表格/代码的盲区
    content.append(
        ChatCompletionContentPartTextParam(
            type="text",
            text=_extract_text_content(local_path),
        ),
    )

    messages: list[ChatCompletionMessageParam] = [
        {"role": "user", "content": content},
    ]

    try:
        response = sync_client.chat_completion(
            messages=messages,
            config=config,
            user_id=user_id,
            action="describe_vl",
            query_summary=Path(local_path).name,
        )
        answer = _require_answer(response.choices[0].message.content)
    except Exception as e:
        logger.error("VL LLM API call failed (error_type={})", type(e).__name__)
        raise
    return answer


def _video_text(path: Path) -> str:
    """汇总视频元数据信息, 无需对视频帧进行解码."""
    try:
        cap = cv2.VideoCapture(str(path))
        width, height = (
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
        fps = cap.get(cv2.CAP_PROP_FPS)
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = count / fps if fps > 0 else 0
        cap.release()
    except BaseException:
        return f"Video File: {path.name}"
    return (
        f"Video Info: {path.name}, Resolution: {width}x{height}, "
        f"Duration: {duration:.2f}s, FPS: {fps:.2f}"
    )


def _docx_text(local_path: str) -> str:
    """将 docx 段落与表格单元格扁平化展开为纯文本."""
    try:
        doc = Document(local_path)
        content = [p.text for p in doc.paragraphs if p.text.strip()]
        content.extend(
            " | ".join(c.text.strip() for c in row.cells if c.text.strip())
            for table in doc.tables
            for row in table.rows
        )
        return "\n".join(content)
    except BaseException:
        return ""


def _pdf_text(local_path: str) -> str:
    """提取整个 PDF 文件的文本流内容."""
    try:
        document = fitz.open(local_path)
        try:
            return "\n".join(page.get_text("text") for page in document)
        finally:
            document.close()
    except Exception as exc:
        logger.error("PDF 文本提取失败 (error_type={})", type(exc).__name__)
        return ""


def _table_text(local_path: str, ext: str) -> str:
    """逐 Sheet 将 CSV/Excel 文件内容渲染为纯文本 CSV 格式."""
    try:
        if ext == ".csv":
            frame = pd.read_csv(local_path, dtype=str, keep_default_na=False)
            return frame.to_csv(index=False)
        with pd.ExcelFile(local_path) as workbook:
            sheets = workbook.parse(sheet_name=None, dtype=str)
        return "\n\n".join(
            f"工作表: {name}\n{frame.fillna('').to_csv(index=False)}"
            for name, frame in sheets.items()
        )
    except Exception as exc:
        logger.error("表格文本提取失败 (error_type={})", type(exc).__name__)
        return ""


def _plain_text(path: Path) -> str:
    """读取文本或代码文件, 忽略无法解码的字节."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except BaseException:
        return ""


def _extract_text_content(local_path: str) -> str:
    """提取可索引文本: 涵盖视频元数据、docx 与纯文本代码等."""
    path = Path(local_path)
    if not path.exists():
        return ""
    ext = path.suffix.lower()

    if ext in VIDEO_EXTENSIONS:
        return _video_text(path)
    if ext == ".docx":
        return _docx_text(local_path)
    if ext == ".pdf":
        return _pdf_text(local_path)
    if ext in TABLE_EXTENSIONS:
        return _table_text(local_path, ext)
    return _plain_text(path) if ext in TEXT_EXTENSIONS or not ext else ""


def _pdf_sections(local_path: str) -> list[TextSection]:
    """逐页提取 PDF 文本, 并保留真实物理页码."""
    document = fitz.open(local_path)
    try:
        return [
            TextSection(page.get_text("text"), page_number=index + 1)
            for index, page in enumerate(document)
            if page.get_text("text").strip()
        ]
    finally:
        document.close()


def extract_file_sections(local_path: str) -> list[TextSection]:
    """提取供分块索引使用的文本; PDF 保留真实页码, 其它格式页码未知."""
    path = Path(local_path)
    if path.suffix.lower() == ".pdf":
        try:
            return _pdf_sections(local_path)
        except Exception as exc:
            logger.error(
                "PDF 分页文本提取失败 (error_type={})",
                type(exc).__name__,
            )
    text = _extract_text_content(local_path)
    return [TextSection(text)] if text.strip() else []
