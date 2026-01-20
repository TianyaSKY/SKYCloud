import asyncio

from flask import Blueprint, request, Response, current_app

from app.services.chat_service import generate_chat_events
from app.utils.decorators import token_required

chat_bp = Blueprint('chat', __name__)


@chat_bp.route('/chat', methods=['POST'])
@token_required
def chat(current_user):
    """聊天接口：返回流式响应"""
    request_data = request.get_json()
    query = request_data.get("query")
    history = request_data.get("history", [])

    if not query:
        return {"error": "Query is required"}, 400

    # 获取当前的 app 实例，以便在生成器线程中使用
    app = current_app._get_current_object()

    def stream():
        # 在同步 Flask 线程中运行异步生成器
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # 使用 app_context 运行异步生成器
        with app.app_context():
            gen = generate_chat_events(current_user.id, query, history)
            try:
                while True:
                    try:
                        # 驱动异步生成器获取下一个值
                        yield loop.run_until_complete(gen.__anext__())
                    except StopAsyncIteration:
                        break
            finally:
                loop.close()

    return Response(stream(), mimetype='text/event-stream')
