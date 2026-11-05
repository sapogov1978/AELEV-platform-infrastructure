param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-z][a-z0-9_-]{2,63}$')][string]$Project,
    [Parameter(Mandatory=$true)][string]$ExportOperatorImage
)
$ErrorActionPreference = 'Stop'
if ($env:DRP_SPIRE_DOCKER_SOCKET_APPROVAL -ne 'operator-root-equivalent-v1') { throw 'SPIRE_EXPORT_DOCKER_SOCKET_APPROVAL_REQUIRED' }
if ($env:OS -ne 'Windows_NT') { throw 'SPIRE_EXPORT_USE_SHELL_ENTRY' }
$spireEndpoint = if ($env:DOCKER_HOST) { $env:DOCKER_HOST } else { docker context inspect --format '{{.Endpoints.docker.Host}}' }
if ($spireEndpoint -notmatch '^(npipe|unix)://') { throw 'SPIRE_EXPORT_LOCAL_DAEMON_REQUIRED' }
if ((docker info --format '{{.OSType}}') -ne 'linux') { throw 'SPIRE_EXPORT_LINUX_ENGINE_REQUIRED' }
$spireDaemon = docker info --format '{{.ID}}'
if ($LASTEXITCODE -ne 0) { throw 'SPIRE_EXPORT_DOCKER_UNAVAILABLE' }
$spireVolume = $Project + '_installer'
$spireOwner = docker volume inspect --format '{{index .Labels "io.aelev.owner"}}' $spireVolume
if ($LASTEXITCODE -ne 0) { throw 'SPIRE_EXPORT_STATE_MISSING' }
$spireInstallation = docker volume inspect --format '{{index .Labels "io.aelev.installation"}}' $spireVolume
$spireRole = docker volume inspect --format '{{index .Labels "io.aelev.role"}}' $spireVolume
if ($spireOwner -ne 'infrastructure-spire' -or $spireInstallation -ne $Project -or $spireRole -ne 'installer') { throw 'SPIRE_EXPORT_FOREIGN_STATE_REFUSED' }
$spireEnv = Join-Path $PSScriptRoot '.env'
$spireBefore = $null
if (Test-Path -LiteralPath $spireEnv) {
    if ((Get-Item -LiteralPath $spireEnv).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'SPIRE_EXPORT_EXISTING_ENV_PRESERVED' }
    $spireLines = @(Get-Content -LiteralPath $spireEnv)
    if ($spireLines.Count -ne 2 -or $spireLines[0] -ne '# AELEV SPIRE installed composition v1' -or
        $spireLines[1] -notmatch '^AELEV_INFRASTRUCTURE_SPIRE_INSTALLED_COMPOSE=\./runtime/installed-composition/[a-f0-9]{64}/compose\.json$') { throw 'SPIRE_EXPORT_EXISTING_ENV_PRESERVED' }
    $spireBefore = [IO.File]::ReadAllBytes($spireEnv)
}
$spireOutput = Join-Path $PSScriptRoot 'runtime/installed-composition'
New-Item -ItemType Directory -Force -Path $spireOutput | Out-Null
$spireHelper = $Project + '-composition-' + [Guid]::NewGuid().ToString('N')
docker run --rm --name $spireHelper --user 0:0 --read-only --network none --pids-limit 128 --cap-drop ALL --security-opt no-new-privileges:true `
    --label io.aelev.owner=infrastructure-spire --label io.aelev.role=operator-composition-export `
    --label "io.aelev.installation=$Project" --memory 256m --tmpfs /tmp:mode=1777,size=16m `
    --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock `
    --mount "type=volume,source=$spireVolume,target=/installation,readonly" `
    --mount "type=bind,source=$spireOutput,target=/output" --entrypoint python `
    $ExportOperatorImage -m aelev_spire.composition --project $Project --daemon-id $spireDaemon --output /output
if ($LASTEXITCODE -ne 0) { throw 'SPIRE_EXPORT_FAILED' }
if (Test-Path -LiteralPath $spireEnv) {
    $spireCurrent = [IO.File]::ReadAllBytes($spireEnv)
    if ($null -eq $spireBefore -or [Convert]::ToBase64String($spireCurrent) -ne [Convert]::ToBase64String($spireBefore)) { throw 'SPIRE_EXPORT_ENV_CHANGED' }
} elseif ($null -ne $spireBefore) { throw 'SPIRE_EXPORT_ENV_CHANGED' }
$spireNext = Join-Path $spireOutput ('env-next-' + [Guid]::NewGuid().ToString())
[IO.File]::Copy((Join-Path $spireOutput 'binding.env'), $spireNext)
try {
    if (Test-Path -LiteralPath $spireEnv) { [IO.File]::Replace($spireNext, $spireEnv, $null) }
    else { [IO.File]::Move($spireNext, $spireEnv) }
} finally {
    if (Test-Path -LiteralPath $spireNext) { [IO.File]::Delete($spireNext) }
}
