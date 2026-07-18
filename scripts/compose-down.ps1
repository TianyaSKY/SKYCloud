# 关闭 SKYCloud 主服务栈，并一并清理动态申请的 OpenCode 工作区容器。
# 用法:
#   .\scripts\compose-down.ps1
#   .\scripts\compose-down.ps1 -v          # 同时删除 compose volumes
#   .\scripts\compose-down.ps1 --remove-orphans

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Get-WorkspaceContainerIds {
    $ids = New-Object System.Collections.Generic.HashSet[string]

    # 优先按 label（新创建的工作区）
    docker ps -aq --filter "label=skycloud.component=workspace" 2>$null | ForEach-Object {
        if ($_ -and $_.Trim()) { [void]$ids.Add($_.Trim()) }
    }

    # 兼容旧容器（仅有命名约定、无 label）
    docker ps -aq --filter "name=skycloud-workspace-" 2>$null | ForEach-Object {
        if ($_ -and $_.Trim()) { [void]$ids.Add($_.Trim()) }
    }

    return @($ids)
}

Write-Host "==> 清理 OpenCode 工作区容器 ..." -ForegroundColor Cyan
$workspaceIds = Get-WorkspaceContainerIds
if ($workspaceIds.Count -eq 0) {
    Write-Host "    无工作区容器" -ForegroundColor DarkGray
} else {
    foreach ($id in $workspaceIds) {
        $name = (docker inspect -f "{{.Name}}" $id 2>$null)
        if ($name) { $name = $name.TrimStart("/") } else { $name = $id }
        Write-Host "    删除 $name ($id)"
        docker rm -f $id | Out-Null
    }
    Write-Host "    已删除 $($workspaceIds.Count) 个工作区容器" -ForegroundColor Green
}

Write-Host "==> docker compose down $($args -join ' ') ..." -ForegroundColor Cyan
if ($args.Count -gt 0) {
    & docker compose down @args
} else {
    & docker compose down
}

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "==> 完成" -ForegroundColor Green
