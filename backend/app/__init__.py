import logging
import time

from flask import Flask
from flask_restx import Api
from sqlalchemy import text

from app.api import api_bp
from app.extensions import *
from app.models import User, File, Folder, SysDict

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)

    # 配置数据库连接
    app.config['SQLALCHEMY_DATABASE_URI'] = f'postgresql://tianya:pwd@{DB_HOST}:5432/skycloud_db'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = 'sky_cloud_secret_key'

    # 配置上传文件夹路径
    app.config['UPLOAD_FOLDER'] = os.getenv("UPLOAD_FOLDER")

    # 确保上传目录存在
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])

    db.init_app(app)

    app.register_blueprint(api_bp, url_prefix='/api')

    with app.app_context():
        # 数据库连接重试逻辑
        max_retries = 5
        for attempt in range(max_retries):
            try:
                # 尝试连接数据库
                db.session.execute(text('SELECT 1'))
                logger.info("Database connection successful.")
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Database connection failed, retrying in 5 seconds... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(5)
                else:
                    logger.error(f"Failed to connect to database after {max_retries} attempts.")
                    raise e

        # 尝试创建 vector 扩展（如果尚未存在）
        try:
            db.session.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            db.session.commit()
        except Exception as e:
            logger.warning(f"Warning: Could not create vector extension: {e}")

        # 自动创建所有表
        db.create_all()

        # 尝试创建 HNSW 索引（如果尚未存在）
        try:
            db.session.execute(
                text('CREATE INDEX IF NOT EXISTS file_vector_idx ON files USING hnsw (vector_info vector_l2_ops)'))
            db.session.commit()
        except Exception as e:
            logger.warning(f"Warning: Could not create vector index: {e}")

        # 初始化数据
        init_data()
    return app


def init_data():
    """初始化数据库数据"""
    try:
        # 初始化管理员用户
        admin_user = User.query.filter_by(username='admin').first()
        if not admin_user:
            admin_user = User(username='admin', role='admin')
            admin_user.set_password('admin123')  # 设置默认密码
            db.session.add(admin_user)
            logger.info("Initialized admin user.")
        # 初始化系统字典配置
        default_configs = [
            {'key': 'site_name', 'value': 'SKYCloud', 'des': "系统名称"},

            {'key': 'vl_api_url', 'value': 'https://api.siliconflow.cn/v1', 'des': "多模态视觉模型 API 地址"},
            {'key': 'vl_api_key', 'value': '', 'des': "多模态视觉模型 API 密钥"},
            {'key': 'vl_api_model', 'value': 'Qwen/Qwen3-VL-30B-A3B-Instruct',
             'des': "多模态视觉模型名称（需具备图像理解能力）"},

            {'key': 'emb_api_url', 'value': 'https://api.siliconflow.cn/v1', 'des': "向量嵌入模型 API 地址"},
            {'key': 'emb_api_key', 'value': '', 'des': "向量嵌入模型 API 密钥"},
            {'key': 'emb_model_name', 'value': 'Qwen/Qwen3-Embedding-8B', 'des': "向量嵌入模型名称 text-embedding即可"},

            {'key': 'chat_api_url', 'value': 'https://api.siliconflow.cn/v1',
             'des': "对话模型 API 地址（建议选择支持高频调用的平台）"},
            {'key': 'chat_api_key', 'value': '', 'des': "对话模型 API 密钥"},
            {'key': 'chat_api_model', 'value': 'deepseek-ai/DeepSeek-V3.2',
             'des': "对话模型名称（建议选择具备强大逻辑推理具有工具调用能力的模型以及较为便宜的模型）"},
        ]

        for config in default_configs:
            sys_dict = SysDict.query.filter_by(key=config['key']).first()
            if not sys_dict:
                sys_dict = SysDict(key=config['key'], value=config['value'], des=config['des'])
                db.session.add(sys_dict)
                logger.info(f"Initialized config: {config['key']}")
        # 初始化管理员用户的根文件夹
        root_folder = Folder.query.filter_by(name="/", parent_id=None).first()
        if not root_folder:
            root_folder = Folder(name="/", user_id=1, parent_id=None)
            db.session.add(root_folder)
            logger.info(f"Initialized root folder: /")
        # 初始化管理员用户的头像文件夹
        image_folder = Folder.query.filter_by(parent_id=1).first()
        if not image_folder:
            image_folder = Folder(name="所有用户头像", user_id=1, parent_id=1)
            db.session.add(image_folder)
            logger.info(f"Initialized image folder: 所有用户头像")

        db.session.commit()
        logger.info("Data initialization completed.")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Data initialization failed: {e}")
