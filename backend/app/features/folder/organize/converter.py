"""格式转换工具: Office->PDF、PDF->图、视频抽帧, 供描述生成链路使用.

仅做本地转换, 不访问 LLM / DB; LibreOffice 路径可通过 LIBREOFFICE_PATH 覆盖.
"""

import os
import subprocess
from pathlib import Path

import cv2
import fitz
from loguru import logger

from app.infra.processes import run_converter

OFFICE_CONVERSION_TIMEOUT_SECONDS = 120


def get_libreoffice_command() -> str:
    """解析 soffice: 优先环境变量, 其次 Windows 安装路径, 最后系统 PATH."""
    env_path = os.environ.get("LIBREOFFICE_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    if os.name == "nt":
        possible_paths = [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
        for path in possible_paths:
            if Path(path).exists():
                return path

    return "soffice"


def convert_office_to_pdf(input_path: str, output_dir: str) -> str:
    """使用无头 LibreOffice 进行转换; 独立用户配置可避免实例死锁."""
    soffice_cmd = get_libreoffice_command()
    logger.info("Converting office document to PDF")

    user_profile_path = str(Path(output_dir) / "libreoffice_user_profile")
    user_profile_url = f"file:///{user_profile_path.replace(os.sep, '/')}"

    cmd = [
        soffice_cmd,
        f"-env:UserInstallation={user_profile_url}",
        "--headless",
        "--convert-to",
        "pdf",
        input_path,
        "--outdir",
        output_dir,
    ]
    try:
        run_converter(cmd, timeout=OFFICE_CONVERSION_TIMEOUT_SECONDS)
        logger.info("Office conversion finished")
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        logger.error("Office conversion failed (error_type={})", type(exc).__name__)
        raise

    return str(Path(output_dir) / f"{Path(input_path).stem}.pdf")


def convert_pdf_to_images(
    pdf_path: str,
    output_dir: str,
    max_pages: int = 5,
) -> list[str]:
    """以 2 倍缩放将前 `max_pages` 页 PDF 渲染为 JPEG 图像."""
    image_paths: list[str] = []
    doc = None
    try:
        doc = fitz.open(pdf_path)
        page_count = min(len(doc), max_pages)
        for i in range(page_count):
            page = doc.load_page(i)
            zoom = 2.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)

            image_path = str(Path(output_dir) / f"page_{i}.jpg")
            pix.save(image_path)
            image_paths.append(image_path)
    except Exception as e:
        logger.error("PDF rendering failed (error_type={})", type(e).__name__)
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception as exc:
                logger.warning(
                    "PDF document cleanup failed (error_type={})",
                    type(exc).__name__,
                )
    return image_paths


def extract_video_frames(
    video_path: str,
    output_dir: str,
    frame_count: int = 5,
) -> list[str]:
    """从整段视频中等间隔均匀抽取 `frame_count` 帧图像."""
    image_paths: list[str] = []
    cap = None
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return []

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            return []

        for i in range(frame_count):
            frame_idx = int(i * total_frames / frame_count)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if ret:
                frame_path = str(Path(output_dir) / f"frame_{i}.jpg")
                cv2.imwrite(frame_path, frame)
                image_paths.append(frame_path)
    except Exception as e:
        logger.error("Video frame extraction failed (error_type={})", type(e).__name__)
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception as exc:
                logger.warning(
                    "Video capture cleanup failed (error_type={})",
                    type(exc).__name__,
                )
    return image_paths
