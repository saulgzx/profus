# =============================================================
# push_to_github.ps1
# Sube el proyecto dofus-autofarm-v2 a GitHub como repo PRIVADO.
# Requisitos:
#   - git instalado y en PATH
#   - GitHub CLI (gh) instalado y autenticado (gh auth login)
# Uso:
#   1) Abrir PowerShell en C:\Users\Alexis\dofus-autofarm-v2
#   2) Ejecutar:  powershell -ExecutionPolicy Bypass -File .\push_to_github.ps1
# =============================================================

$ErrorActionPreference = "Stop"

# 0. Ir a la carpeta del proyecto (por si se ejecuta desde otro lado)
Set-Location -LiteralPath "C:\Users\Alexis\dofus-autofarm-v2"
Write-Host ""
Write-Host "===> Carpeta de trabajo: $(Get-Location)" -ForegroundColor Cyan

# 1. Verificar herramientas
Write-Host ""
Write-Host "===> Verificando git..." -ForegroundColor Cyan
git --version
if ($LASTEXITCODE -ne 0) { throw "git no está instalado o no está en PATH" }

Write-Host ""
Write-Host "===> Verificando GitHub CLI (gh)..." -ForegroundColor Cyan
gh --version
if ($LASTEXITCODE -ne 0) { throw "gh no está instalado. Instalar desde https://cli.github.com/" }

Write-Host ""
Write-Host "===> Estado de autenticación de gh..." -ForegroundColor Cyan
gh auth status
if ($LASTEXITCODE -ne 0) {
    Write-Host "No estás autenticado en gh. Ejecutá:  gh auth login  y volvé a correr este script." -ForegroundColor Yellow
    exit 1
}

# 2. Estado del repo local
Write-Host ""
Write-Host "===> git status (antes)" -ForegroundColor Cyan
git status --short

# 3. Configurar identidad si falta (no rompe si ya está)
$userName  = (git config user.name)  2>$null
$userEmail = (git config user.email) 2>$null
if (-not $userName)  { git config user.name  "Alexis" }
if (-not $userEmail) { git config user.email "algonzx1z2@gmail.com" }

# 4. Asegurar branch main
$currentBranch = (git rev-parse --abbrev-ref HEAD) 2>$null
if (-not $currentBranch -or $currentBranch -eq "HEAD") {
    git checkout -b main
} elseif ($currentBranch -ne "main") {
    Write-Host "Branch actual: $currentBranch (se renombra a main)" -ForegroundColor Yellow
    git branch -M main
}

# 5. Add + commit (solo si hay cambios)
git add -A
$pending = (git status --porcelain)
if ($pending) {
    Write-Host ""
    Write-Host "===> Creando commit..." -ForegroundColor Cyan
    git commit -m "v2: snapshot inicial subido a GitHub"
} else {
    Write-Host "===> Nada nuevo para commitear." -ForegroundColor Yellow
}

# 6. ¿Ya tiene remoto origin?
$origin = (git remote get-url origin) 2>$null
if ($origin) {
    Write-Host ""
    Write-Host "===> Remoto origin ya existe: $origin" -ForegroundColor Yellow
    Write-Host "===> Haciendo push..." -ForegroundColor Cyan
    git push -u origin main
} else {
    # 7. Crear repo privado en GitHub y pushear
    $repoName = "dofus-autofarm-v2"
    Write-Host ""
    Write-Host "===> Creando repo PRIVADO '$repoName' en GitHub..." -ForegroundColor Cyan
    gh repo create $repoName --private --source=. --remote=origin --push --description "Dofus Retro autofarm bot - v2 (refactor en progreso)"
}

# 8. Mostrar URL final
Write-Host ""
Write-Host "===> Listo. Repo:" -ForegroundColor Green
gh repo view --json url,visibility,nameWithOwner | Out-Host

Write-Host ""
Write-Host "Para abrirlo en el navegador: gh repo view --web" -ForegroundColor DarkGray
