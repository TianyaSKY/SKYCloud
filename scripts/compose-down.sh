#!/usr/bin/env bash
# 关闭 SKYCloud 主服务栈，并一并清理动态申请的 OpenCode 工作区容器。
# 用法:
#   ./scripts/compose-down.sh
#   ./scripts/compose-down.sh -v
#   ./scripts/compose-down.sh --remove-orphans

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

collect_workspace_ids() {
  {
    # 优先按 label（新创建的工作区）
    docker ps -aq --filter "label=skycloud.component=workspace" 2>/dev/null || true
    # 兼容旧容器（仅有命名约定、无 label）
    docker ps -aq --filter "name=skycloud-workspace-" 2>/dev/null || true
  } | awk 'NF' | sort -u
}

echo "==> 清理 OpenCode 工作区容器 ..."
mapfile -t workspace_ids < <(collect_workspace_ids)

if [[ ${#workspace_ids[@]} -eq 0 || -z "${workspace_ids[0]:-}" ]]; then
  echo "    无工作区容器"
else
  for id in "${workspace_ids[@]}"; do
    name="$(docker inspect -f '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##')"
    echo "    删除 ${name:-$id} ($id)"
    docker rm -f "$id" >/dev/null
  done
  echo "    已删除 ${#workspace_ids[@]} 个工作区容器"
fi

echo "==> docker compose down $* ..."
docker compose down "$@"

echo "==> 完成"
