from flask import Blueprint

api_bp = Blueprint('api', __name__)

from . import user, file, folder, sys_dict, auth, share, inbox

# Register auth blueprint
api_bp.register_blueprint(auth.auth_bp, url_prefix='/auth')
api_bp.register_blueprint(share.share_bp)
