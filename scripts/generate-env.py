"""创建或维护私有部署系统密钥, 且不向终端打印任何机密信息."""

import argparse
import os
from pathlib import Path
import secrets

SECRET_NAMES = {
    "SECRET_KEY",
    "OPENCODE_SERVER_SECRET",
    "RUNTIME_CONTROLLER_TOKEN",
    "POSTGRES_PASSWORD",
    "RABBITMQ_PASSWORD",
    "MINIO_SECRET_KEY",
}


def load_env_file(path: Path) -> dict[str, str]:
    """读取指定 env 文件中的键值对."""
    if not path.is_file():
        return {}
    res: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        res[k.strip()] = v.strip()
    return res


def generate(
    output: Path,
    template: Path | None = None,
    legacy_env: Path | None = None,
) -> dict[str, str]:
    """独占创建包含强随机密钥的私有环境配置文件."""
    output = Path(output)
    if output.exists():
        msg = f"Output already exists; credentials were not changed: {output}"
        raise FileExistsError(msg)

    # 优先从现有环境配置中继承有效凭据以保持已有数据卷与会话兼容
    inherited: dict[str, str] = {}
    legacy_path = legacy_env
    if legacy_path is None:
        potential = output.parent.parent / ".env" if output.parent.name == ".skycloud" else Path(".env")
        if potential.is_file() and potential.resolve() != output.resolve():
            legacy_path = potential

    if legacy_path and legacy_path.is_file():
        inherited = load_env_file(legacy_path)

    # 生成相互独立的强密钥
    generated_secrets: dict[str, str] = {}
    used_values: set[str] = set()

    # 首先继承有效的旧凭据
    for name in sorted(SECRET_NAMES):
        val = inherited.get(name, "")
        if val and len(val) >= 32 and val not in used_values:
            generated_secrets[name] = val
            used_values.add(val)

    # 为缺失的凭据生成高强度随机密钥
    for name in sorted(SECRET_NAMES):
        if name not in generated_secrets:
            while True:
                token = secrets.token_urlsafe(48)
                if token not in used_values:
                    generated_secrets[name] = token
                    used_values.add(token)
                    break

    # 严格校验密钥强度与独立性
    if len({generated_secrets[k] for k in SECRET_NAMES}) != len(SECRET_NAMES):
        msg = "Generated secrets must be distinct"
        raise RuntimeError(msg)
    if any(len(generated_secrets[k]) < 32 for k in SECRET_NAMES):
        msg = "Generated secrets must be at least 32 characters"
        raise RuntimeError(msg)

    lines: list[str] = []
    if template and template.is_file():
        written_secrets: set[str] = set()
        for line in template.read_text(encoding="utf-8").splitlines():
            name = line.split("=", 1)[0].strip()
            if name in SECRET_NAMES:
                line = f"{name}={generated_secrets[name]}"
                written_secrets.add(name)
            lines.append(line)
        for name in sorted(SECRET_NAMES - written_secrets):
            lines.append(f"{name}={generated_secrets[name]}")
    else:
        lines.append("# SKYCloud 内部系统密钥与镜像标识 (自动生成，勿提交版本库)")
        for name in sorted(SECRET_NAMES):
            lines.append(f"{name}={generated_secrets[name]}")
        if inherited.get("MINIO_IMAGE"):
            lines.append(f"MINIO_IMAGE={inherited['MINIO_IMAGE']}")
        if inherited.get("OPENCODE_IMAGE"):
            lines.append(f"OPENCODE_IMAGE={inherited['OPENCODE_IMAGE']}")

    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")

    return generated_secrets


def ensure_generated_env(
    output: Path = Path(".skycloud/generated.env"),
    legacy_env: Path | None = None,
) -> Path:
    """确保系统密钥配置文件存在, 若不存在则安全初始化, 不覆盖已有凭据."""
    output = Path(output)
    if not output.is_file():
        generate(output=output, legacy_env=legacy_env)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".skycloud/generated.env"),
        help="目标系统密钥路径 (默认: .skycloud/generated.env)",
    )
    parser.add_argument(
        "--legacy-env",
        type=Path,
        default=None,
        help="已有旧配置路径 (可选, 用于继承既有数据库与存储密码)",
    )
    args = parser.parse_args()
    try:
        ensure_generated_env(args.output, legacy_env=args.legacy_env)
    except FileExistsError:
        parser.error("Output already exists; credentials were not changed")
    print(f"System secrets initialized in {args.output}")
