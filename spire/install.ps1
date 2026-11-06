param(
    [Parameter(Mandatory=$true)][ValidateSet('server','agent','authorize','revoke','inspect','observation','export-bootstrap')][string]$Role,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-z][a-z0-9_-]{2,63}$')][string]$Project,
    [Parameter(Mandatory=$true)][string]$RuntimeImage,
    [Parameter(Mandatory=$true)][string]$InstallerImage,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$ServerOptions
)
$ErrorActionPreference = 'Stop'
if ($env:DRP_SPIRE_DOCKER_SOCKET_APPROVAL -ne 'operator-root-equivalent-v1') { throw 'SPIRE_INSTALL_DOCKER_SOCKET_APPROVAL_REQUIRED' }
if ($env:OS -ne 'Windows_NT') { throw 'SPIRE_INSTALL_USE_SHELL_ENTRY' }
$spireEndpoint = if ($env:DOCKER_HOST) { $env:DOCKER_HOST } else { docker context inspect --format '{{.Endpoints.docker.Host}}' }
if ($spireEndpoint -notmatch '^(npipe|unix)://') { throw 'SPIRE_INSTALL_LOCAL_DAEMON_REQUIRED' }
if ((docker info --format '{{.OSType}}') -ne 'linux') { throw 'SPIRE_INSTALL_LINUX_ENGINE_REQUIRED' }
$spireDaemon = docker info --format '{{.ID}}'
if ($LASTEXITCODE -ne 0) { throw 'SPIRE_INSTALL_DOCKER_UNAVAILABLE' }
foreach ($spireImage in @($RuntimeImage,$InstallerImage)) {
    docker image inspect $spireImage *> $null
    if ($LASTEXITCODE -ne 0) {
        docker pull $spireImage
        if ($LASTEXITCODE -ne 0) { throw 'SPIRE_INSTALL_IMAGE_UNAVAILABLE' }
    }
}
$spireStateVolume = $Project + '_installer'
docker volume inspect $spireStateVolume *> $null
if ($LASTEXITCODE -eq 0) {
    $spireOwner = docker volume inspect --format '{{index .Labels "io.aelev.owner"}}' $spireStateVolume
    $spireInstallation = docker volume inspect --format '{{index .Labels "io.aelev.installation"}}' $spireStateVolume
    $spireVolumeRole = docker volume inspect --format '{{index .Labels "io.aelev.role"}}' $spireStateVolume
    if ($spireOwner -ne 'infrastructure-spire' -or $spireInstallation -ne $Project -or $spireVolumeRole -ne 'installer') { throw 'SPIRE_INSTALL_FOREIGN_VOLUME_REFUSED' }
} else {
    docker volume create --label io.aelev.owner=infrastructure-spire --label "io.aelev.installation=$Project" --label io.aelev.role=installer $spireStateVolume | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'SPIRE_INSTALL_VOLUME_FAILED' }
}
$spireEmpty = Join-Path ([System.IO.Path]::GetTempPath()) ('spire-input-' + [Guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $spireEmpty | Out-Null
try {
    $spireInput = if ($env:AELEV_INFRASTRUCTURE_SPIRE_INITIAL_INPUT) { $env:AELEV_INFRASTRUCTURE_SPIRE_INITIAL_INPUT } else { $spireEmpty }
    $spireAuthority = $env:AELEV_INFRASTRUCTURE_SPIRE_AUTHORITY_SOURCE
    if (-not $spireAuthority) {
        $spireAuthority = docker run --rm --user 0:0 --network none --read-only --entrypoint python `
            --mount "type=volume,source=$spireStateVolume,target=/installation,readonly" `
            $RuntimeImage -S -c 'import json; from pathlib import Path; p=Path("/installation/deployment.json"); print((json.loads(p.read_bytes()).get("authority_source") or "") if p.is_file() else "")'
        if ($LASTEXITCODE -ne 0) { throw 'SPIRE_INSTALL_STATE_UNAVAILABLE' }
    }
    if (-not $spireAuthority) { $spireAuthority = $spireEmpty }
    docker run --rm --user 0:0 --read-only --network none --pids-limit 128 --memory 512m --cap-drop ALL --security-opt no-new-privileges:true `
        --label io.aelev.owner=infrastructure-spire --label io.aelev.role=operator-installer --label "io.aelev.installation=$Project" `
        --tmpfs /tmp:size=16m,mode=1777 `
        --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock `
        --mount "type=volume,source=$spireStateVolume,target=/installation" `
        --mount "type=bind,source=$spireInput,target=/run/input,readonly" `
        --mount "type=bind,source=$spireAuthority,target=/run/authority-source,readonly" `
        $InstallerImage $Role --project $Project --runtime-image $RuntimeImage `
        --daemon-id $spireDaemon --host-os Windows --authority-source $spireAuthority @ServerOptions
    if ($LASTEXITCODE -ne 0) { throw 'SPIRE_INSTALL_FAILED' }
} finally {
    # Only the exact empty directory created here, never recursive deletion.
    [System.IO.Directory]::Delete($spireEmpty)
}
