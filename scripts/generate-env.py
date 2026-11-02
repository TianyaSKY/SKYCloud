"""创建私有部署配置, 且不向终端打印任何机密信息."""

import argparse
import os
from pathlib import Path
import secrets


SECRET_NAMES = {
    "SECRET_KEY", "OPENCODE_SERVER_SECRET", "RUNTIME_CONTROLLER_TOKEN",
    "POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "MINIO_SECRET_KEY",
}


def generate(output: Path, template: Path):
    lines = []
    for line in template.read_text(encoding="utf-8").splitlines():
        name = line.split("=", 1)[0]
        if name in SECRET_NAMES:
            line = name + "=" + secrets.token_urlsafe(48)
        lines.append(line)
    # 独占创建防止意外覆盖运行中环境的已有凭据。
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".env"))
    args = parser.parse_args()
    try:
        generate(args.output, Path(__file__).resolve().parents[1] / ".env.example")
    except FileExistsError:
        parser.error("Output already exists; credentials were not changed")
    print("Private configuration created. Configure your model API credentials before startup.")
