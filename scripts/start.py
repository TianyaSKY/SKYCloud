"""SKYCloud 统一跨平台初始化与启动入口.

负责:
1. 校验用户 .env 中的 6 项模型基础配置
2. 自动生成并持久化系统机密至 .skycloud/generated.env (不覆盖既有凭证)
3. 自动校验或构建所需的 MinIO 与 OpenCode 不可变镜像
4. 挂载系统与用户配置, 执行 Docker Compose 编排启动
"""

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("generate_env", ROOT / "scripts/generate-env.py")
if spec is None or spec.loader is None:
    msg = "无法加载 scripts/generate-env.py"
    raise RuntimeError(msg)
_gen_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_gen_mod)
ensure_generated_env = _gen_mod.ensure_generated_env
load_env_file = _gen_mod.load_env_file

REQUIRED_MODEL_CONFIGS = (
    "CHAT_API_URL",
    "CHAT_API_KEY",
    "CHAT_API_MODEL",
    "EMB_API_URL",
    "EMB_API_KEY",
    "EMB_MODEL_NAME",
)


def check_user_env(env_path: Path) -> tuple[bool, list[str]]:
    """校验用户 .env 是否已配置 6 项核心模型参数."""
    if not env_path.is_file():
        return False, [f"配置文件不存在: {env_path}"]

    env_data = load_env_file(env_path)
    missing = [key for key in REQUIRED_MODEL_CONFIGS if not env_data.get(key)]
    return len(missing) == 0, missing


def is_image_present(image_id: str) -> bool:
    """检查 Docker 引擎中是否存在指定本地镜像."""
    if not image_id or not image_id.strip():
        return False
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image_id.strip()],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def ensure_system_images(
    generated_env_path: Path, *, force_rebuild: bool = False,
) -> None:
    """检查并按需构建 MinIO 和 OpenCode 不可变镜像."""
    gen_data = load_env_file(generated_env_path)

    minio_image = gen_data.get("MINIO_IMAGE", "")
    if force_rebuild or not is_image_present(minio_image):
        print("构建 MinIO 本地安全存储镜像...")
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/build-minio-image.py"),
                "--env-file",
                str(generated_env_path),
            ],
            check=True,
        )
        if proc.returncode != 0:
            msg = "MinIO 镜像构建失败"
            raise RuntimeError(msg)
    else:
        print(f"MinIO 镜像就绪: {minio_image[:19]}...")

    opencode_image = gen_data.get("OPENCODE_IMAGE", "")
    if force_rebuild or not is_image_present(opencode_image):
        print("构建 OpenCode 运行时沙箱镜像...")
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/build-runtime-image.py"),
                "--env-file",
                str(generated_env_path),
            ],
            check=True,
        )
        if proc.returncode != 0:
            msg = "OpenCode 运行时镜像构建失败"
            raise RuntimeError(msg)
    else:
        print(f"OpenCode 镜像就绪: {opencode_image[:19]}...")


def build_compose_command(
    *,
    user_env: Path,
    generated_env: Path,
    advanced_env: Path | None = None,
    dev_mode: bool = False,
    down_mode: bool = False,
    compose_build: bool = False,
) -> list[str]:
    """组装 Docker Compose 命令参数列表."""
    cmd = ["docker", "compose"]

    # 优先加载系统生成的内部凭证
    if generated_env.is_file():
        cmd.extend(["--env-file", str(generated_env)])

    # 加载可选的高级部署配置
    if advanced_env and advanced_env.is_file():
        cmd.extend(["--env-file", str(advanced_env)])

    # 加载用户填写的模型配置 (优先级最高)
    if user_env.is_file():
        cmd.extend(["--env-file", str(user_env)])

    cmd.extend(["-f", str(ROOT / "docker-compose.yml")])
    if dev_mode:
        cmd.extend(["-f", str(ROOT / "docker-compose.dev.yml")])

    if down_mode:
        cmd.append("down")
    else:
        cmd.extend(["up", "-d"])
        if compose_build:
            cmd.append("--build")

    return cmd


def main(argv: list[str] | None = None) -> int:
    """启动脚本执行主入口."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dev",
        action="store_true",
        help="以开发模式启动, 暴露数据库/Redis/RabbitMQ/MinIO 本地环回端口",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="启动 Compose 时重新构建应用服务容器",
    )
    parser.add_argument(
        "--down",
        action="store_true",
        help="停止并移除当前已启动的容器与内部网络",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="仅校验配置与镜像, 不启动 Docker 容器",
    )
    parser.add_argument(
        "--rebuild-images",
        action="store_true",
        help="强制重新构建 MinIO 与 OpenCode 基础沙箱镜像",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / ".env",
        help="用户模型配置文件路径 (默认: .env)",
    )
    parser.add_argument(
        "--generated-env-file",
        type=Path,
        default=ROOT / ".skycloud/generated.env",
        help="系统内部机密文件路径 (默认: .skycloud/generated.env)",
    )
    parser.add_argument(
        "--advanced-env-file",
        type=Path,
        default=ROOT / ".env.advanced",
        help="可选高级部署配置文件路径 (默认: .env.advanced)",
    )
    args = parser.parse_args(argv)

    user_env: Path = args.env_file
    generated_env: Path = args.generated_env_file
    advanced_env: Path = args.advanced_env_file

    if args.down:
        cmd = build_compose_command(
            user_env=user_env,
            generated_env=generated_env,
            advanced_env=advanced_env,
            dev_mode=args.dev,
            down_mode=True,
        )
        print(f"正在停止服务: {' '.join(cmd)}")
        return subprocess.run(cmd, check=False).returncode

    # 1. 检查并校验用户 .env
    if not user_env.is_file():
        example_path = ROOT / ".env.example"
        print(f"[ERROR] 配置文件未找到: {user_env}")
        if example_path.is_file():
            print(f"请复制模板并填写 6 项模型配置:\n  cp {example_path} {user_env}")
        return 1

    ok, missing = check_user_env(user_env)
    if not ok:
        print("[ERROR] 用户 .env 中以下必要模型配置项未填写:")
        for key in missing:
            print(f"  - {key}")
        print("请在 .env 中补充上述参数后再启动。\n")
        return 1

    # 2. 自动生成并维护系统密钥 (不覆盖已有凭据, 继承旧部署数据卷)
    print("检查系统私有凭证...")
    ensure_generated_env(output=generated_env, legacy_env=user_env)

    # 3. 准备所需不可变本地镜像
    print("检查系统镜像依赖...")
    try:
        ensure_system_images(generated_env, force_rebuild=args.rebuild_images)
    except Exception as exc:
        print(f"[ERROR] 镜像准备失败: {exc}")
        return 1

    if args.check_only:
        print("[OK] 配置与镜像环境检查全部通过。")
        return 0

    # 4. 执行 Docker Compose 编排启动
    cmd = build_compose_command(
        user_env=user_env,
        generated_env=generated_env,
        advanced_env=advanced_env,
        dev_mode=args.dev,
        down_mode=False,
        compose_build=args.build,
    )
    print(f"启动 SKYCloud 服务集群:\n  {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode == 0:
        print("\n[SUCCESS] SKYCloud 启动完成!")
        print("访问前端界面: http://localhost (默认端口: 80)")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
