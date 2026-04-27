param(
    [string]$Vault = ""   # Opcional: override do destino do vault
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

# ── Verifica versão do Python ──────────────────────────────────────────────────
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) {
    $pyCmd = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $pyCmd) {
    Write-Host "ERRO: Python nao encontrado no PATH." -ForegroundColor Red
    Write-Host "Instale Python 3.9+ em https://python.org e marque 'Add to PATH'." -ForegroundColor Yellow
    Read-Host "Pressione Enter para fechar"
    exit 1
}

$pyExe = $pyCmd.Source
$pyMinor = & $pyExe -c "import sys; print(sys.version_info.minor)" 2>$null
$pyMajor = & $pyExe -c "import sys; print(sys.version_info.major)" 2>$null
if ([int]$pyMajor -lt 3 -or ([int]$pyMajor -eq 3 -and [int]$pyMinor -lt 9)) {
    Write-Host "ERRO: Python 3.9+ e necessario. Versao encontrada: $pyMajor.$pyMinor" -ForegroundColor Red
    Read-Host "Pressione Enter para fechar"
    exit 1
}

# ── Resolve o desktop real (OneDrive ou local) ─────────────────────────────────
$desktopReal = (Get-Item (Join-Path $env:USERPROFILE 'OneDrive\*de Trabalho') -ErrorAction SilentlyContinue).FullName
if (-not $desktopReal) {
    $desktopReal = (Get-Item (Join-Path $env:USERPROFILE '*de Trabalho') -ErrorAction SilentlyContinue).FullName
}
if (-not $desktopReal) {
    $desktopReal = Join-Path $env:USERPROFILE 'Desktop'
}

Write-Host "Desktop encontrado: $desktopReal" -ForegroundColor Cyan

# ── Localiza 'Claude Data' recursivamente (ate 5 niveis abaixo do desktop) ─────
$claudeDataDir = Get-ChildItem $desktopReal -Recurse -Depth 5 -Directory -ErrorAction SilentlyContinue |
                 Where-Object { $_.Name -eq 'Claude Data' } |
                 Select-Object -First 1

if ($claudeDataDir) {
    $base        = $claudeDataDir.FullName
    $contextRoot = $claudeDataDir.Parent.FullName
    Write-Host "Claude Data encontrado em: $base" -ForegroundColor Green
} else {
    $base        = Join-Path $desktopReal 'Claude Data'
    $contextRoot = $desktopReal
}

$script = Join-Path $contextRoot 'claude_to_obsidian\claude_scraper.py'

# Vault: usa o parâmetro -Vault se fornecido, senão o padrão
if ($Vault -ne "") {
    $vaultPath = $Vault
} else {
    $vaultPath = Join-Path $contextRoot 'MeuVaultClaude'
}

$logFile = Join-Path $contextRoot 'claude_scraper_log.txt'

"" | Set-Content $logFile -Encoding UTF8

function Log($msg) {
    Write-Host $msg
    $msg | Add-Content $logFile -Encoding UTF8
}

Log "============================================"
Log ("  Claude Artifacts -> Obsidian  |  " + (Get-Date -Format 'dd/MM/yyyy HH:mm'))
Log "============================================"
Log ""
Log "Base  : $base"
Log "Script: $script"
Log "Vault : $vaultPath"
Log ""

if (-not (Test-Path $base)) {
    Log "ERRO: Pasta 'Claude Data' nao encontrada em: $desktopReal"
    Log "Verifique se a pasta existe com esse nome exato."
    Read-Host "Pressione Enter para fechar"
    exit 1
}

if (-not (Test-Path $script)) {
    Log "ERRO: claude_scraper.py nao encontrado em: $script"
    Log "Verifique se a pasta claude_to_obsidian esta na Area de Trabalho."
    Read-Host "Pressione Enter para fechar"
    exit 1
}

$pastas = Get-ChildItem -Path $base -Directory
$processados = 0

foreach ($pasta in $pastas) {
    # Caso 1: Claude / DeepSeek — tem conversations.json singular
    $json = Join-Path $pasta.FullName 'conversations.json'
    if (Test-Path $json) {
        Log (">> " + $pasta.Name)
        # Output em tempo real — sem buffer em memória
        & $pyExe $script $json --vault $vaultPath 2>&1 | ForEach-Object {
            Log $_
        }
        $processados++
        Log ""
        continue
    }

    # Caso 2: ChatGPT — tem conversations-000.json paginado (sem conversations.json)
    $paginado = Get-ChildItem $pasta.FullName -Filter 'conversations-*.json' -ErrorAction SilentlyContinue |
                Select-Object -First 1
    if ($paginado) {
        Log (">> " + $pasta.Name + " [ChatGPT]")
        & $pyExe $script $pasta.FullName --vault $vaultPath 2>&1 | ForEach-Object {
            Log $_
        }
        $processados++
        Log ""
        continue
    }
}

if ($processados -eq 0) {
    Log "Nenhuma pasta com conversations.json encontrada dentro de 'Claude Data'."
} else {
    Log "============================================"
    Log "  Concluido! $processados export(s) processados."
    Log "  Vault: $vaultPath"
    Log "============================================"
}

Log ""
Log "Log salvo em: $logFile"
Read-Host "Pressione Enter para fechar"
