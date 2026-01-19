import datetime
import logging

from app.extensions import db
from app.models.file import File
from app.services import sys_dict_service, file_service, inbox_service
from worker.util import desc_file

logger = logging.getLogger(__name__)


def handle_file_process(file_id):
    """
    处理文件的具体业务逻辑
    """
    try:
        file: File = file_service.get_file(file_id)
        if not file:
            logger.error(f"File ID {file_id} not found.")
            return

        logger.info(f"Starting to process file: {file.name} (ID: {file_id})")

        # 更新状态为处理中
        file.status = 'processing'
        db.session.commit()

        # 需要使用VL模型
        vl_url = sys_dict_service.get_sys_dict_by_key('vl_api_url').value
        vl_key = sys_dict_service.get_sys_dict_by_key('vl_api_key').value
        vl_model = sys_dict_service.get_sys_dict_by_key('vl_api_model').value
        emb_url = sys_dict_service.get_sys_dict_by_key("emb_api_url").value
        emb_key = sys_dict_service.get_sys_dict_by_key("emb_api_key").value
        emb_model = sys_dict_service.get_sys_dict_by_key("emb_model_name").value

        # 使用 get_abs_path 获取完整路径
        abs_path = file.get_abs_path()
        description = desc_file(abs_path, vl_url, vl_key, vl_model)
        file.description = description
        db.session.commit()
        file.vector_info = file_service.embedding_desc(description, emb_url, emb_key, emb_model)
        # 更新状态为成功
        file.status = 'success'
        db.session.commit()
        logger.info(f"Finished processing file ID: {file_id} successfully.")

    except Exception as e:
        logger.error(f"Error processing file {file_id}: {e}")
        db.session.rollback()
        file = File.query.get(file_id)
        if file:
            file.status = 'fail'
            db.session.commit()
        # 发送信息给用户
        inbox_service.create_inbox_message({
            'type': 'system',
            'user_id': file.uploader_id,
            'title': '文件处理失败',
            'content':'处理文件时出现了错误\n'
            f'时间:{datetime.datetime.now()}\n'
            f'文件id:{file_id}\n'
            f'{e}\n'
        })
