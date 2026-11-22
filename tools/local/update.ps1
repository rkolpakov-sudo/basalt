<#
.SYNOPSIS
    Update this install to a newer Basalt release, with an automatic rollback.

.DESCRIPTION
    Started by serve/local/updater.py (POST /api/local/update) as a separate process, outside the
    server's job object: a running .exe cannot be replaced, and bin/basalt.exe is the process serving
    the page that starts this.  Every step is appended to logs/update.log, which the page tails; the
    last word is logs/update.result.json.

    Steps: refuse a dirty tree -> fetch upstream -> pick the newest release tag -> back up bin/ ->
    merge -> if the only conflict is our page, take upstream's and rebuild ours -> download the
    release archive -> stop, install its .exe, start -> smoke test -> tag the result.

    Any failure after the merge is rolled back: the tree to the previous commit, bin/ from the backup,
    the server restarted, the result written with ok=false.  A failed update therefore ends where it
    started instead of leaving a broken install.

    This file is ASCII on purpose: Windows PowerShell 5.1 reads a .ps1 without a byte-order mark as
    ANSI, so any non-ASCII character here would be mangled.  Russian text belongs to the page.

.PARAMETER Root
    The install folder: the one holding bin/, serve/ and basalt_config.json.

.PARAMETER Target
    The release tag to install, e.g. v0.6.0.  Default: the newest tag upstream has.

.PARAMETER Port
    The server's port, for the smoke test.  Default 8080.
#>
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string]$Target,
    [int]$Port = 8080
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

$Log        = Join-Path $Root 'logs\update.log'
$PidFile    = Join-Path $Root 'logs\update.pid'
$ResultFile = Join-Path $Root 'logs\update.result.json'
$Backup     = Join-Path $Root 'bin.prev'
$Ctl        = Join-Path $Root 'serve_ctl.ps1'
$Bin        = Join-Path $Root 'bin'
$RepoUrl    = 'https://github.com/jesdga95/basalt'
$Utf8       = New-Object System.Text.UTF8Encoding $false

$OldCommit = $null
$OldTag    = $null
$BackedUp  = $false
$Merged    = $false
$Stopped   = $false

New-Item -ItemType Directory -Force -Path (Join-Path $Root 'logs') | Out-Null
[System.IO.File]::WriteAllText($Log, '', $Utf8)

function Write-Log([string]$Text) {
    $line = '{0}  {1}' -f (Get-Date -Format 'HH:mm:ss'), $Text
    [System.IO.File]::AppendAllText($Log, $line + "`n", $Utf8)
    Write-Host $line
}

function Fail([string]$Text) { throw $Text }

function Invoke-Git([string[]]$GitArgs) {
    $out  = & git -C $Root @GitArgs 2>&1
    $code = $LASTEXITCODE
    $text = (($out | Out-String).Trim())
    if ($code -ne 0) { Fail ('git ' + ($GitArgs -join ' ') + ' failed: ' + $text) }
    return $text
}

function Try-Git([string[]]$GitArgs) {
    $out  = & git -C $Root @GitArgs 2>&1
    return @{ Code = $LASTEXITCODE; Text = (($out | Out-String).Trim()) }
}

function ConvertTo-TagVersion([string]$Tag) {
    $core = ($Tag -replace '^v', '') -replace '[-+].*$', ''
    try { return [version]$core } catch { return [version]'0.0.0' }
}

function Get-Python {
    $venv = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path $venv) { return $venv }
    return 'python'
}

function Complete([bool]$Ok, [string]$Reason, [hashtable]$Extra = @{}) {
    $now = [int]([datetimeoffset](Get-Date)).ToUnixTimeSeconds()
    $result = @{
        ok          = $Ok
        reason      = $Reason
        from        = $OldCommit
        from_tag    = $OldTag
        to          = $Target
        finished_at = $now
    }
    foreach ($k in $Extra.Keys) { $result[$k] = $Extra[$k] }
    [System.IO.File]::WriteAllText($ResultFile, ($result | ConvertTo-Json -Depth 4), $Utf8)
    if ($Ok) { Write-Log 'result: ok' }
    else { Write-Log ('result: FAILED -- ' + $Reason) }
}

try {
    $now = [int]([datetimeoffset](Get-Date)).ToUnixTimeSeconds()
    [System.IO.File]::WriteAllText($PidFile,
        (@{ pid = $PID; started = $now; target = $Target } | ConvertTo-Json -Compress), $Utf8)

    Write-Log ('Basalt update: root ' + $Root)
    if ($Target) { Write-Log ('target: ' + $Target) } else { Write-Log 'target: the newest release upstream has' }

    # --- 1. preconditions ---------------------------------------------------
    Write-Log '[1/8] checking the install'
    if (-not (Test-Path (Join-Path $Root '.git'))) { Fail ('not a git checkout: ' + $Root + ' has no .git') }
    $dirty = (Try-Git @('status', '--porcelain', '--untracked-files=no')).Text
    if ($dirty) { Fail ("the working tree has uncommitted changes; commit or discard them first:`n" + $dirty) }
    $OldCommit = (Try-Git @('rev-parse', 'HEAD')).Text
    $tagOut = Try-Git @('describe', '--tags', '--match', 'v*', '--abbrev=0', 'HEAD')
    if ($tagOut.Code -eq 0) { $OldTag = $tagOut.Text }
    Write-Log ('  at ' + $OldCommit.Substring(0, [Math]::Min(12, $OldCommit.Length)) + ' on ' + $OldTag)
    if (-not (Test-Path $Bin)) { Fail ('no bin/ folder in ' + $Root) }

    # --- 2. fetch and pick the target --------------------------------------
    Write-Log '[2/8] fetching upstream'
    if (-not (Try-Git @('remote', 'get-url', 'upstream')).Text) {
        Invoke-Git @('remote', 'add', 'upstream', $RepoUrl) | Out-Null
        Write-Log ('  added the upstream remote: ' + $RepoUrl)
    }
    Invoke-Git @('fetch', '--tags', '--prune', 'upstream') | Out-Null
    $tags = (& git -C $Root tag --list 'v*')
    $newest = $tags | Where-Object { $_ -match '^v\d+\.\d+\.\d+' } |
        Sort-Object { ConvertTo-TagVersion $_ } | Select-Object -Last 1
    if (-not $Target) { $Target = $newest }
    if (-not $Target) { Fail 'no release tags were found upstream' }
    Write-Log ('  newest release: ' + $newest)

    if ((Try-Git @('merge-base', '--is-ancestor', $Target, 'HEAD')).Code -eq 0) {
        Write-Log ('already on ' + $Target + ' or newer: nothing to do')
        Complete $true 'already up to date' @{ changed = $false }
        exit 0
    }

    # --- 3. back up the engines -------------------------------------------
    Write-Log '[3/8] backing up bin'
    if (Test-Path $Backup) { Remove-Item $Backup -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $Backup | Out-Null
    Get-ChildItem $Bin -Filter *.exe | ForEach-Object { Copy-Item $_.FullName $Backup -Force }
    $BackedUp = $true
    Write-Log ('  ' + (Get-ChildItem $Backup -File).Count + ' .exe files in bin.prev')

    # --- 4. merge ----------------------------------------------------------
    Write-Log ('[4/8] merging ' + $Target)
    $merge = Try-Git @('merge', '--no-edit', $Target)
    if ($merge.Code -ne 0) {
        $unmerged = @((& git -C $Root diff --name-only --diff-filter=U) | Where-Object { $_ })
        if ($unmerged.Count -eq 1 -and $unmerged[0].Trim() -eq 'serve/web/index.html') {
            Write-Log '  the only conflict is our page: taking upstream and rebuilding ours'
            Invoke-Git @('checkout', '--theirs', '--', 'serve/web/index.html') | Out-Null
            $py = Get-Python
            & $py (Join-Path $Root 'serve\web\local\build_index.py') 2>&1 | ForEach-Object { Write-Log ('  ' + $_) }
            if ($LASTEXITCODE -ne 0) { Fail 'build_index.py failed: the page could not be rebuilt' }
            Invoke-Git @('add', 'serve/web/index.html') | Out-Null
            Invoke-Git @('commit', '--no-edit') | Out-Null
            Write-Log '  merge finished'
        } else {
            Fail ('merge conflict outside serve/web/index.html: ' + ($unmerged -join ', '))
        }
    }
    $Merged = $true
    Write-Log ('  now at ' + (Try-Git @('rev-parse', '--short', 'HEAD')).Text)

    # --- 5. download the release -----------------------------------------
    Write-Log ('[5/8] downloading the ' + $Target + ' engine')
    $asset = 'basalt-' + $Target + '-windows-x64.zip'
    $url   = 'https://github.com/jesdga95/basalt/releases/download/' + $Target + '/' + $asset
    $tmp   = Join-Path $env:TEMP ('basalt-update-' + $Target)
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $zip = Join-Path $tmp $asset
    Write-Log ('  ' + $url)
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 900
    Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp 'x') -Force
    $exes = @(Get-ChildItem (Join-Path $tmp 'x') -Recurse -Filter *.exe)
    if ($exes.Count -eq 0) { Fail ('the archive has no .exe files: ' + $asset) }
    Write-Log ('  ' + $exes.Count + ' engines, ' + [Math]::Round(((Get-Item $zip).Length / 1MB), 1) + ' MiB')

    # --- 6. install and restart ------------------------------------------
    Write-Log '[6/8] stopping the server, installing the engines, starting it again'
    & powershell -NoProfile -ExecutionPolicy Bypass -File $Ctl stop 2>&1 | ForEach-Object { Write-Log ('  ' + $_) }
    $Stopped = $true
    foreach ($e in $exes) {
        Copy-Item $e.FullName $Bin -Force
        Write-Log ('  installed ' + $e.Name)
    }
    Remove-Item $tmp -Recurse -Force
    & powershell -NoProfile -ExecutionPolicy Bypass -File $Ctl start -WaitSeconds 300 2>&1 | ForEach-Object { Write-Log ('  ' + $_) }
    if ($LASTEXITCODE -ne 0) { Fail 'the server did not answer after the update' }

    # --- 7. smoke test ---------------------------------------------------
    Write-Log '[7/8] smoke test'
    $base = 'http://127.0.0.1:' + $Port
    $st = Invoke-RestMethod ($base + '/v1/status') -TimeoutSec 20
    if ($st.service -ne 'basalt') { Fail 'GET /v1/status does not look like Basalt' }
    $ls = Invoke-RestMethod ($base + '/api/local/state') -TimeoutSec 20
    if (-not $ls.version.local) { Fail '/api/local/state reports no local tag' }
    Write-Log ('  local tag: ' + $ls.version.local)
    $page = (Invoke-WebRequest ($base + '/') -UseBasicParsing -TimeoutSec 20).Content
    if ($page -notmatch 'local-ui:views') { Fail 'the served page has no local views' }
    foreach ($f in @('chat.js', 'settings.js', 'extra.css', 'app.js')) {
        $r = Invoke-WebRequest ($base + '/web/' + $f) -UseBasicParsing -TimeoutSec 20
        if ($r.StatusCode -ne 200) { Fail ('/web/' + $f + ' did not load') }
    }
    Write-Log '  the page and its assets load'

    # A new .exe that cannot load a model is not an update: ask for one token.
    $engineVersion = $null
    try {
        $models = (Invoke-RestMethod ($base + '/v1/models') -TimeoutSec 20).data
        if (-not $models) { Fail '/v1/models lists nothing' }
        $body = @{ model = $models[0].id; messages = @(@{ role = 'user'; content = 'ping' })
                   max_tokens = 1; stream = $false } | ConvertTo-Json -Depth 6
        $reply = Invoke-RestMethod ($base + '/v1/chat/completions') -Method Post `
            -ContentType 'application/json' -Body $body -TimeoutSec 900
        if (-not $reply.choices) { Fail 'the engine answered no completion' }
        $engineVersion = (Invoke-RestMethod ($base + '/v1/status') -TimeoutSec 20).engine
        Write-Log ('  the engine answered; it reports version ' + $engineVersion)
    } catch {
        $msg = $_.Exception.Message
        if ($msg -match '40[13]') {
            Write-Log '  the server asks for an api key: skipping the completion check'
        } else {
            Fail ('the engine did not answer a request: ' + $msg)
        }
    }
    $expected = $Target -replace '^v', ''
    if ($engineVersion -and -not $engineVersion.StartsWith($expected)) {
        Fail ('the new engine reports ' + $engineVersion + ', but the release is ' + $expected)
    }

    # --- 8. tag the result -----------------------------------------------
    Write-Log '[8/8] tagging'
    $localTag = 'local-' + $expected
    if ((Try-Git @('rev-parse', '-q', '--verify', ('refs/tags/' + $localTag))).Code -ne 0) {
        Invoke-Git @('tag', '-a', $localTag, '-m', ('local build on Basalt ' + $Target)) | Out-Null
        Write-Log ('  tagged ' + $localTag)
    } else {
        Write-Log ('  tag ' + $localTag + ' already exists')
    }
    Write-Log 'done. The new commits are local: push them yourself when you are happy with the build.'
    Complete $true 'updated' @{ changed = $true; engine = $engineVersion; tag = $localTag }
    exit 0
}
catch {
    $reason = $_.Exception.Message
    Write-Log ('FAILED: ' + $reason)
    if ($Merged -or $Stopped) {
        Write-Log ('rolling back to ' + $OldCommit)
        try {
            & git -C $Root merge --abort 2>&1 | Out-Null
            if ($OldCommit) { & git -C $Root reset --hard $OldCommit 2>&1 | ForEach-Object { Write-Log ('  ' + $_) } }
            if ($Stopped -and $BackedUp) {
                & powershell -NoProfile -ExecutionPolicy Bypass -File $Ctl stop 2>&1 | ForEach-Object { Write-Log ('  ' + $_) }
                Get-ChildItem $Backup -Filter *.exe | ForEach-Object { Copy-Item $_.FullName $Bin -Force }
                Write-Log '  previous engines restored'
            }
            & powershell -NoProfile -ExecutionPolicy Bypass -File $Ctl start -WaitSeconds 300 2>&1 | ForEach-Object { Write-Log ('  ' + $_) }
            Write-Log 'rolled back: the previous version is running again'
            Complete $false $reason @{ rolled_back = $true; rollback_to = $OldCommit }
        } catch {
            Write-Log ('ROLLBACK FAILED: ' + $_.Exception.Message)
            Complete $false $reason @{ rolled_back = $false; rollback_error = $_.Exception.Message }
        }
    } else {
        Write-Log 'nothing was changed: the tree and bin/ are as they were'
        Complete $false $reason @{ rolled_back = $false }
    }
    exit 1
}
