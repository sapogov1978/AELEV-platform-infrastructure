param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $StationArguments
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'USE_NATIVE_UNIX_BOOTSTRAP'
}
$python = $null
if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    $python = & py.exe -3.13 -c 'import sys; print(sys.executable)'
}
if (-not $python) {
    throw 'APPROVED_PYTHON_313_REQUIRED'
}
& $python (Join-Path $PSScriptRoot 'station.py') @StationArguments
exit $LASTEXITCODE
