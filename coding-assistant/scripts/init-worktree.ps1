param(
    [Parameter(Mandatory = $true)]
    [string]$TaskName,
    [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
    [string[]]$WritablePath
)

$ErrorActionPreference = 'Stop'
$projectRoot = (git rev-parse --show-toplevel).Trim()
if (-not $projectRoot) { throw 'Run from a Git worktree.' }

$branch = "feature/$TaskName"
$worktreeRoot = Join-Path (Split-Path $projectRoot -Parent) '.worktrees'
$worktreePath = Join-Path $worktreeRoot $TaskName
if (Test-Path -LiteralPath $worktreePath) { throw "Worktree exists: $worktreePath" }
if (git branch --list $branch) { throw "Branch exists: $branch" }

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
