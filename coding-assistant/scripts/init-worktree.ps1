<#!
.SYNOPSIS
  编码-Agent模板 V3.2 的 Windows worktree 初始化器。

.DESCRIPTION
  创建 feature/<task> worktree，并写入未跟踪的 .claude/worktree-scope.json，
  将模板的全局可写区进一步收窄到本任务的文件范围。任务完成报告仍由 Agent
  正常提交为 WORKTREE-COMPLETION.md，供主线审核和归档。
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$TaskName,
    [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
    [string[]]$WritablePath
)

$ErrorActionPreference = 'Stop'
$projectRoot = (git rev-parse --show-toplevel).Trim()
if (-not $projectRoot) { throw '请在 Git 项目根目录运行此脚本。' }

$branch = "feature/$TaskName"
$worktreeRoot = Join-Path (Split-Path $projectRoot -Parent) '.worktrees'
$worktreePath = Join-Path $worktreeRoot $TaskName
if (Test-Path -LiteralPath $worktreePath) { throw "worktree 已存在: $worktreePath" }
if (git branch --list $branch) { throw "分支已存在: $branch" }

New-Item -ItemType Directory -Path $worktreeRoot -Force | Out-Null
git worktree add -b $branch $worktreePath HEAD

$scope = [ordered]@{
    task = $TaskName
    writable = @($WritablePath) + @('WORKTREE-COMPLETION.md')
    forbidden = @('.env', 'evals/runs/**', 'Dockerfile', '.gitignore', 'CLAUDE.md', '.claude/settings.json')
} | ConvertTo-Json -Depth 3
$scopePath = Join-Path $worktreePath '.claude\worktree-scope.json'
New-Item -ItemType Directory -Path (Split-Path $scopePath -Parent) -Force | Out-Null
[System.IO.File]::WriteAllText($scopePath, $scope + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))

Write-Output "Worktree: $worktreePath"
Write-Output "Branch: $branch"
Write-Output "Scope: $scopePath"
Write-Output '下一步：在该 worktree 启动 Claude Code，并按主项目任务清单执行；完成后必须提交 WORKTREE-COMPLETION.md，禁止自行合并。'
