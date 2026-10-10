param([Parameter(Mandatory=$true)][string]$Plan)
$ErrorActionPreference = 'Stop'
$p = Get-Content -LiteralPath $Plan -Raw -Encoding UTF8 | ConvertFrom-Json
$target = [IO.Path]::GetFullPath([string]$p.target)
$stage = [IO.Path]::GetFullPath([string]$p.stage)
$backup = [IO.Path]::GetFullPath([string]$p.backup)
$installDir = [IO.Path]::GetDirectoryName($target)
$movedOld = $false
$installed = $false
$started = $null
$verifiedPaths = $false
$parentExited = $false
function Save-Result([bool]$success, [string]$message) {
    $value = @{ success=$success; version=$p.version; message=$message; target=$target; time=[DateTime]::UtcNow.ToString('o') }
    $tempResult = [string]$p.result + '.tmp'
    $value | ConvertTo-Json -Compress | Set-Content -LiteralPath $tempResult -Encoding UTF8
    Move-Item -LiteralPath $tempResult -Destination $p.result -Force
}
function Move-Retry([string]$from, [string]$to) {
    for ($attempt=0; $attempt -lt 80; $attempt++) {
        try { Move-Item -LiteralPath $from -Destination $to -ErrorAction Stop; return }
        catch { if ($attempt -eq 79) { throw }; Start-Sleep -Milliseconds 250 }
    }
}
function Start-Assistant([bool]$updated) {
    # The old one-file parent deletes its extraction directory on exit.
    # Both the updated program and a rollback need a fresh independent unpack.
    Get-ChildItem Env: | Where-Object { $_.Name -like '_PYI_*' -or $_.Name -eq '_MEIPASS2' } |
        ForEach-Object { Remove-Item -LiteralPath ('Env:' + $_.Name) -ErrorAction SilentlyContinue }
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    $env:DESKRAWL_ASSISTANT_DATA_DIR = [string]$p.data_root
    if ($updated) { $env:DESKRAWL_ASSISTANT_UPDATE_ID = [string]$p.id }
    else { Remove-Item Env:DESKRAWL_ASSISTANT_UPDATE_ID -ErrorAction SilentlyContinue }
    return Start-Process -FilePath $target -ArgumentList @('--port', [string]$p.port) -WorkingDirectory $installDir -WindowStyle Hidden -PassThru
}
try {
    # Check all final absolute move/delete paths against the explicitly selected install directory.
    if ([string]$p.id -notmatch '^[0-9a-f]{32}$' -or
        [IO.Path]::GetDirectoryName($stage) -ne $installDir -or
        [IO.Path]::GetDirectoryName($backup) -ne $installDir -or
        [IO.Path]::GetFileName($stage) -ne ('.deskrawl-update-' + $p.id + '.exe') -or
        [IO.Path]::GetFileName($backup) -ne ('.deskrawl-backup-' + $p.id + '.exe') -or
        [IO.Path]::GetExtension($target) -ne '.exe' -or $target -eq $stage -or $target -eq $backup -or
        (Test-Path -LiteralPath $backup)) { throw '更新路径校验失败，未替换程序。' }
    $updateDir = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Plan))
    if ([IO.Path]::GetFileName($Plan) -ne ('plan-' + $p.id + '.json') -or
        [IO.Path]::GetFullPath([IO.Path]::Combine([string]$p.data_root, 'data\runtime\updates')) -ne $updateDir -or
        [IO.Path]::GetFullPath($PSCommandPath) -ne [IO.Path]::Combine($updateDir, 'replace-' + $p.id + '.ps1') -or
        [IO.Path]::GetFullPath([string]$p.receipt) -ne [IO.Path]::Combine($updateDir, 'ready-' + $p.id + '.json') -or
        [IO.Path]::GetFullPath([string]$p.helper_ready) -ne [IO.Path]::Combine($updateDir, 'helper-' + $p.id + '.json') -or
        [IO.Path]::GetFullPath([string]$p.result) -ne [IO.Path]::Combine($updateDir, 'last-result.json')) {
        throw '更新辅助文件路径无效。'
    }
    $verifiedPaths = $true
    @{ ready=$true; id=$p.id } | ConvertTo-Json -Compress | Set-Content -LiteralPath $p.helper_ready -Encoding UTF8
    try { $parent = [Diagnostics.Process]::GetProcessById([int]$p.parent_pid) }
    catch [ArgumentException] { $parent = $null }
    if ($null -ne $parent -and -not $parent.WaitForExit(60000)) { throw '旧程序尚未退出，未替换程序。' }
    $parentExited = $true
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $hashStream = [IO.File]::OpenRead($stage)
    try { $actualHash = [BitConverter]::ToString($algorithm.ComputeHash($hashStream)).Replace('-', '').ToLowerInvariant() }
    finally { $hashStream.Dispose(); $algorithm.Dispose() }
    if ((Get-Item -LiteralPath $stage).Length -ne [long]$p.size -or $actualHash -ne $p.sha256) {
        throw '待安装文件校验失败，未替换程序。'
    }
    $version = [Diagnostics.FileVersionInfo]::GetVersionInfo($stage).ProductVersion
    if ($version -ne [string]$p.version) { throw '待安装程序版本不一致，未替换程序。' }
    Move-Retry $target $backup
    $movedOld = $true
    Move-Retry $stage $target
    $installed = $true
    $started = Start-Assistant $true
    $ready = $false
    for ($attempt=0; $attempt -lt 240; $attempt++) {
        if (Test-Path -LiteralPath $p.receipt) {
            $receipt = Get-Content -LiteralPath $p.receipt -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($receipt.ready -eq $true -and $receipt.id -eq $p.id -and $receipt.version -eq $p.version) { $ready=$true; break }
        }
        $started.Refresh()
        if ($started.HasExited) { break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw '新版本未确认启动，正在恢复旧程序。' }
    Save-Result $true ('已更新到 v' + $p.version + '，规则和日志保留。')
    # A confirmed new window has loaded; only remove this operation's own backup.
    Remove-Item -LiteralPath $backup -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $p.receipt -ErrorAction SilentlyContinue
} catch {
    $failure = $_.Exception.Message
    try {
        if ($installed -and $null -ne $started) {
            $started.Refresh()
            # Only the process launched here and its same-exe one-file child can be stopped.
            $owned = @()
            if (-not $started.HasExited) {
                Add-Type -AssemblyName System.Management
                $searcher = New-Object System.Management.ManagementObjectSearcher -ArgumentList ('SELECT ProcessId, ExecutablePath FROM Win32_Process WHERE ParentProcessId = ' + $started.Id)
                try { $owned = @($searcher.Get()) } finally { $searcher.Dispose() }
            }
            foreach ($child in $owned) {
                if ($child.ExecutablePath -eq $target) { Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue }
            }
            $started.Refresh()
            if (-not $started.HasExited -and $started.MainModule.FileName -eq $target) { $started.Kill(); $started.WaitForExit(10000) | Out-Null }
        }
        if ($movedOld -and (Test-Path -LiteralPath $backup)) {
            if (Test-Path -LiteralPath $target) { Move-Retry $target $stage }
            Move-Retry $backup $target
            $movedOld = $false
            $null = Start-Assistant $false
        } elseif ($verifiedPaths -and $parentExited -and -not $installed -and (Test-Path -LiteralPath $target)) {
            $null = Start-Assistant $false
        }
    } catch { $failure += '；自动恢复未完成，备份保留在：' + $backup }
    if ($verifiedPaths) { Save-Result $false $failure }
} finally {
    # All deletes are exact files whose resolved parent was checked above.
    if ($verifiedPaths -and -not $movedOld -and [IO.Path]::GetDirectoryName($stage) -eq $installDir -and
        [IO.Path]::GetFileName($stage) -eq ('.deskrawl-update-' + $p.id + '.exe')) {
        Remove-Item -LiteralPath $stage -ErrorAction SilentlyContinue
    }
    if ($verifiedPaths) {
        Remove-Item -LiteralPath $Plan -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $PSCommandPath -ErrorAction SilentlyContinue
    }
}
