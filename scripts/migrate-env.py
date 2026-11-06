"""SKYCloud 配置文件迁移工具: 将旧版 86 项 .env 安全迁移至现代化精简配置.

迁移过程:
1. 将当前 .env 完整备份至 .env.bak
2. 将系统凭证与不可变镜像迁移至 .skycloud/generated.env (0600 私有权限)
3. 将用户自定义高级配置 (如非默认端口、域名、VL/Rerank 等) 迁移至 .env.advanced
4. 将 .env 精简为仅包含 6 项核心模型参数
5. 保证既有数据库密码、MinIO 凭证与数据卷完全一致, 绝不删除或丢失数据
"""

import argparse
import importlib.util
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("generate_env", ROOT / "scripts/generate-env.py")
if spec is None or spec.loader is None:
    msg = "无法加载 scripts/generate-env.py"
    raise RuntimeError(msg)
_gen_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_gen_mod)
SECRET_NAMES = _gen_mod.SECRET_NAMES
load_env_file = _gen_mod.load_env_file

MODEL_KEYS = (
    "CHAT_API_URL",
    "CHAT_API_KEY",
    "CHAT_API_MODEL",
    "EMB_API_URL",
    "EMB_API_KEY",
    "EMB_MODEL_NAME",
)

SYSTEM_SECRET_KEYS = frozenset(
    SECRET_NAMES | {"MINIO_IMAGE", "OPENCODE_IMAGE", "MINIO_ROOT_PASSWORD", "OPENCODE_SERVER_TOKEN"},
)


def migrate(
    env_file: Path = ROOT / ".env",
    generated_env_file: Path = ROOT / ".skycloud/generated.env",
    advanced_env_file: Path = ROOT / ".env.advanced",
    backup_file: Path = ROOT / ".env.bak",
) -> int:
    """执行 .env 配置文件迁移."""
    if not env_file.is_file():
        print(f"[ERROR] 目标环境文件不存在: {env_file}")
        return 1

    old_env = load_env_file(env_file)
    if not old_env:
        print(f"[ERROR] 环境文件内容为空: {env_file}")
        return 1

    # 检查是否已经是精简后的配置
    non_model_keys = set(old_env.keys()) - set(MODEL_KEYS)
    if not non_model_keys:
        print("[INFO] 当前 .env 已经是精简配置 (仅含模型参数), 无需重复迁移。")
        return 0

    # 1. 备份原配置
    shutil.copy2(env_file, backup_file)
    print(f"[1/4] 原配置文件已安全备份至: {backup_file}")

    # 2. 迁移系统凭证与不可变镜像到 .skycloud/generated.env
    generated_env_file.parent.mkdir(parents=True, exist_ok=True)
    existing_generated = load_env_file(generated_env_file) if generated_env_file.is_file() else {}

    # 继承旧凭证 (不覆盖已有生成凭证)
    merged_generated: dict[str, str] = dict(existing_generated)
    for k in SYSTEM_SECRET_KEYS:
        if k in old_env and old_env[k] and k not in merged_generated:
            merged_generated[k] = old_env[k]

    # 兼容历史键名互转
    if "MINIO_ROOT_PASSWORD" in old_env and "MINIO_SECRET_KEY" not in merged_generated:
        merged_generated["MINIO_SECRET_KEY"] = old_env["MINIO_ROOT_PASSWORD"]
    if "MINIO_SECRET_KEY" in old_env and "MINIO_ROOT_PASSWORD" not in merged_generated:
        merged_generated["MINIO_ROOT_PASSWORD"] = old_env["MINIO_SECRET_KEY"]
    if "OPENCODE_SERVER_TOKEN" in old_env and "OPENCODE_SERVER_SECRET" not in merged_generated:
        merged_generated["OPENCODE_SERVER_SECRET"] = old_env["OPENCODE_SERVER_TOKEN"]
    if "OPENCODE_SERVER_SECRET" in old_env and "OPENCODE_SERVER_TOKEN" not in merged_generated:
        merged_generated["OPENCODE_SERVER_TOKEN"] = old_env["OPENCODE_SERVER_SECRET"]

    gen_lines = [
        "# SKYCloud 内部系统密钥与镜像标识 (自动生成/迁移自旧部署, 勿提交版本库)",
    ]
    for k, v in sorted(merged_generated.items()):
        gen_lines.append(f"{k}={v}")

    generated_env_file.write_text("\n".join(gen_lines) + "\n", encoding="utf-8")
    generated_env_file.chmod(0o600)
    print(f"[2/4] 系统凭据与镜像标识已保存至: {generated_env_file} (mode 0600)")

    # 3. 收集用户可能自定义的高级部署参数并迁移至 .env.advanced
    advanced_settings: dict[str, str] = {}
    for k, v in old_env.items():
        if k not in MODEL_KEYS and k not in SYSTEM_SECRET_KEYS and k != "DEFAULT_MODEL_PWD":
            advanced_settings[k] = v

    if advanced_settings:
        adv_lines = [
            "# SKYCloud 高级可选部署配置 (迁移自旧版 .env)",
            "# 此处变量将按需覆盖系统默认值, 普通部署无需修改",
        ]
        for k, v in sorted(advanced_settings.items()):
            adv_lines.append(f"{k}={v}")
        advanced_env_file.write_text("\n".join(adv_lines) + "\n", encoding="utf-8")
        print(f"[3/4] 既有高级/部署参数已提取至: {advanced_env_file}")
    else:
        print("[3/4] 无额外高级参数需要提取。")

    # 4. 重写 .env 为仅包含 6 项核心模型参数
    clean_lines = [
        "# =================================================================",
        "# SKYCloud 用户模型配置",
        "# 仅需配置 Chat 与 Embedding; 其他由系统自动管理",
        "# =================================================================",
        "",
        "# Chat 模型",
        f"CHAT_API_URL={old_env.get('CHAT_API_URL', '')}",
        f"CHAT_API_KEY={old_env.get('CHAT_API_KEY', '')}",
        f"CHAT_API_MODEL={old_env.get('CHAT_API_MODEL', '')}",
        "",
        "# Embedding 向量模型",
        f"EMB_API_URL={old_env.get('EMB_API_URL', '')}",
        f"EMB_API_KEY={old_env.get('EMB_API_KEY', '')}",
        f"EMB_MODEL_NAME={old_env.get('EMB_MODEL_NAME', '')}",
    ]
    env_file.write_text("\n".join(clean_lines) + "\n", encoding="utf-8")
    env_file.chmod(0o600)
    print(f"[4/4] {env_file} 已成功精简为 6 项核心模型配置!")
    print("\n[SUCCESS] 迁移成功! 现有数据库数据与凭据均完整保留。")
    print("后续可通过统一启动入口启动服务: python scripts/start.py")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--generated-env-file", type=Path, default=ROOT / ".skycloud/generated.env")
    parser.add_argument("--advanced-env-file", type=Path, default=ROOT / ".env.advanced")
    parser.add_argument("--backup-file", type=Path, default=ROOT / ".env.bak")
    args = parser.parse_args()
    sys.exit(
        migrate(
            env_file=args.env_file,
            generated_env_file=args.generated_env_file,
            advanced_env_file=args.advanced_env_file,
            backup_file=args.backup_file,
        )
    )
