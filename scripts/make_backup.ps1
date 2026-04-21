param(
    [string]$Timestamp = (Get-Date -Format 'yyyyMMdd-HHmmss')
)

$ErrorActionPreference = 'Stop'
$root = 'C:\Users\Alexis\dofus-autofarm'
$out = $root + '\backups\dofus-autofarm-backup-' + $Timestamp + '.zip'
$staging = $env:TEMP + '\dofus-backup-' + $Timestamp

if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
New-Item -ItemType Directory -Force -Path $staging | Out-Null

Copy-Item -Path (Join-Path $root 'config.yaml') -Destination $staging -Force
Copy-Item -Path (Join-Path $root 'launch_gui.pyw') -Destination $staging -Force
Copy-Item -Path (Join-Path $root 'requirements.txt') -Destination $staging -Force

Copy-Item -Path (Join-Path $root 'src') -Destination (Join-Path $staging 'src') -Recurse -Force
Copy-Item -Path (Join-Path $root 'scripts') -Destination (Join-Path $staging 'scripts') -Recurse -Force
Copy-Item -Path (Join-Path $root 'assets') -Destination (Join-Path $staging 'assets') -Recurse -Force
Copy-Item -Path (Join-Path $root 'mapas') -Destination (Join-Path $staging 'mapas') -Recurse -Force

# Purgar pycache
Get-ChildItem -Path $staging -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
Get-ChildItem -Path $staging -Recurse -File -Filter '*.pyc' | Remove-Item -Force

Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $out -Force
Remove-Item -Recurse -Force $staging

$f = Get-Item $out
Write-Host ('[OK] Backup: ' + $f.FullName)
Write-Host ('Size: ' + [math]::Round($f.Length/1MB, 2) + ' MB')
