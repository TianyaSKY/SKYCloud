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
    # OpenCode 运行时容器同样挂载在 compose 网络上
    docker ps -aq --filter "label=skycloud.component=opencode-runtime" 2>/dev/null || true
    # 兼容旧容器（仅有命名约定、无 label）
    docker ps -aq --filter "name=skycloud-workspace-" 2>/dev/null || true
    docker ps -aq --filter "name=skycloud-opencode-" 2>/dev/null || true
  } | awk 'NF' | sort -u
}

echo "==> 清理 OpenCode 工作区容器 ..."
workspace_ids=()
while IFS= read -r workspace_id; do
  if [[ -n "$workspace_id" ]]; then
    workspace_ids[${#workspace_ids[@]}]="$workspace_id"
  fi
done < <(collect_workspace_ids)

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

compose_network="${COMPOSE_PROJECT_NAME:-skycloud}_skycloud-network"
if docker network inspect "$compose_network" >/dev/null 2>&1; then
  attached_containers="$(
    docker network inspect "$compose_network" \
      --format '{{range $id, $container := .Containers}}{{$container.Name}}{{"\n"}}{{end}}' 2>/dev/null \
      | awk 'NF' || true
  )"
  if [[ -n "$attached_containers" ]]; then
    echo "错误：网络 ${compose_network} 仍被以下容器占用：" >&2
    while IFS= read -r container_name; do
      if [[ -n "$container_name" ]]; then
        echo "    ${container_name}" >&2
      fi
    done <<< "$attached_containers"
    echo "如需清理旧的 Compose 服务，请使用 --remove-orphans 参数重试。" >&2
    exit 1
  fi
fi

echo "==> 完成"
