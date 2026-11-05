# Adds the Corral panel to Herdr Desktop on this Windows computer (the one
# whose window you use). After this, the panel updates itself from Corral.
#   irm https://raw.githubusercontent.com/bfreed/herdr-corral/main/desktop/install.ps1 | iex
# Works in Windows PowerShell 5.1 and PowerShell 7. On macOS and Linux, use install.sh.
& {
    $ErrorActionPreference = "Stop"
    $ref = if ($env:CORRAL_REF) { $env:CORRAL_REF } else { "main" }
    $base = "https://raw.githubusercontent.com/bfreed/herdr-corral/$ref/desktop/corral/mods/corral"
    $appHome = if ($env:HERDR_APP_HOME) { $env:HERDR_APP_HOME }
               elseif ($env:XDG_CONFIG_HOME) { Join-Path $env:XDG_CONFIG_HOME "herdr-app" }
               else { Join-Path $env:USERPROFILE ".config\herdr-app" }
    $dir = Join-Path $appHome "mods\corral"
    $main = Join-Path $dir "main.js"
    if ((Test-Path -LiteralPath $main) -and -not ((Get-Content -LiteralPath $main -TotalCount 1) -like "// Corral panel*")) {
        Write-Host "$dir holds a different mod named corral; leaving it alone." -ForegroundColor Red
        return
    }

    # Herdr docks a new panel the first time it sees it, but never into its own
    # layouts (main, terminals, win98): those are read-only, and while one shows
    # the panel waits. So on one of those, show a copy of it first; Herdr then
    # docks the panel there. The copy keeps using Herdr's panels, which keep
    # updating; only its arrangement stops following Herdr's.
    $shown = "main"
    try {
        $layout = (Get-Content -LiteralPath (Join-Path $appHome "shared.json") -Raw | ConvertFrom-Json).layout
        if ($layout -and (Test-Path -LiteralPath (Join-Path $appHome "layouts\$layout.json"))) { $shown = $layout }
    } catch { }
    $seen = $false
    try { $seen = (Get-Content -LiteralPath (Join-Path $appHome "seen.json") -Raw) -match '"corral"' } catch { }
    $copy = $null
    $manual = $false
    if (-not $seen -and @("main", "terminals", "win98") -contains $shown) {
        $copy = "$shown-corral"
        $app = (Get-Command herdr-app -ErrorAction SilentlyContinue).Source
        if (-not $app) {
            $installed = Join-Path $env:LOCALAPPDATA "Programs\herdr-app\herdr-app.exe"
            if (Test-Path -LiteralPath $installed) { $app = $installed }
        }
    }

    # Download first, so a failed download changes nothing.
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    foreach ($f in "mod.json", "main.js") {
        Invoke-WebRequest -UseBasicParsing -Uri "$base/$f" -OutFile (Join-Path $dir ".$f.part")
    }

    if ($copy) {
        # --name/--as rather than JSON: Windows PowerShell drops the quotes in JSON arguments.
        # In Windows PowerShell, a native program's stderr would stop this script, so
        # it's caught here; a closed window is reported by the exit code.
        $ok = $false
        if ($app) {
            $was = $ErrorActionPreference
            try {
                $ErrorActionPreference = "Continue"
                if (Test-Path -LiteralPath (Join-Path $appHome "layouts\$copy.json")) {
                    & $app do layout.switch --name $copy 2>&1 | Out-Null
                } else {
                    & $app do layout.clone --name $shown --as $copy 2>&1 | Out-Null
                }
                $ok = $LASTEXITCODE -eq 0
            } catch { $ok = $false } finally { $ErrorActionPreference = $was }
        }
        if ($ok) {
            Write-Host "Herdr now shows $copy, your copy of its $shown layout; the panel docks there (ctrl-shift-l switches layouts)."
        } else {
            $manual = $true
        }
    }

    # main.js last: Herdr reloads the panel when it changes.
    foreach ($f in "mod.json", "main.js") {
        Move-Item -Force -LiteralPath (Join-Path $dir ".$f.part") -Destination (Join-Path $dir $f)
    }
    Write-Host "Corral panel installed in $dir."
    if ($manual) {
        Write-Host "Herdr shows new panels only in a layout of your own, and it's showing its own"
        Write-Host "`"$shown`". With Herdr open, make a copy of it, and Herdr docks the panel there:"
        Write-Host "  herdr-app do layout.clone --name $shown --as $copy"
        Write-Host "(Or in Herdr's command palette: Duplicate layout.)"
    } elseif (-not $copy) {
        if ($seen) { Write-Host "It shows wherever your layout has it." } else { Write-Host "Herdr docks it on the left." }
    }
}
