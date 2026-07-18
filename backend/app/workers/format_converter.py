"""格式转换工具：Office→PDF、PDF→图、视频抽帧，供描述生成链路使用。

仅做本地转换，不访问 LLM / DB；LibreOffice 路径可通过 LIBREOFFICE_PATH 覆盖。
"""

import locale
import logging
import os
import subprocess

import cv2
import fitz

logger = logging.getLogger(__name__)


def get_libreoffice_command():
    """解析 soffice 可执行路径：环境变量 → Windows 常见安装位 → PATH 中的 soffice。"""
    env_path = os.environ.get("LIBREOFFICE_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    if os.name == "nt":
        possible_paths = [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
        for path in possible_paths:
            if os.path.exists(path):
                return path

    return "soffice"


def convert_office_to_pdf(input_path, output_dir):
    """用 LibreOffice headless 转 PDF；独立 UserInstallation 避免并发锁 profile。"""
    soffice_cmd = get_libreoffice_command()
    logger.info(f"Converting office document to PDF: {input_path}")

    user_profile_path = os.path.join(output_dir, "libreoffice_user_profile")
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
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        logger.info(f"Successfully converted to PDF: {input_path}")
    except FileNotFoundError:
        logger.error(f"Error: '{soffice_cmd}' command not found.")
        raise
    except subprocess.CalledProcessError as e:
        stderr_content = e.stderr if e.stderr else b""
        try:
            error_msg = stderr_content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                error_msg = stderr_content.decode(locale.getpreferredencoding())
            except:
                error_msg = stderr_content.decode("utf-8", errors="replace")

        logger.error(f"LibreOffice conversion failed: {error_msg}")
        raise e

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    return os.path.join(output_dir, base_name + ".pdf")


def convert_pdf_to_images(pdf_path, output_dir, max_pages=5):
    """PDF 前 max_pages 页渲成 JPEG；2x zoom 兼顾清晰度与体积。"""
    image_paths = []
    try:
        doc = fitz.open(pdf_path)
        page_count = min(len(doc), max_pages)
        for i in range(page_count):
            page = doc.load_page(i)
            zoom = 2.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)

            image_path = os.path.join(output_dir, f"page_{i}.jpg")
            pix.save(image_path)
            image_paths.append(image_path)
        doc.close()
    except Exception as e:
        logger.error(f"Error converting PDF to images: {e}")
    return image_paths


def extract_video_frames(video_path, output_dir, frame_count=5):
    """等间隔抽取 frame_count 帧，覆盖片头到片尾。"""
    image_paths = []
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
                frame_path = os.path.join(output_dir, f"frame_{i}.jpg")
                cv2.imwrite(frame_path, frame)
                image_paths.append(frame_path)
        cap.release()
    except Exception as e:
        logger.error(f"Error extracting video frames: {e}")
    return image_paths
