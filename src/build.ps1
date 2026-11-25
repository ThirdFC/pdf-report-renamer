$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $taskPython)) {
    py -3.12 -m venv .venv
}
& $taskPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
& $taskPython -m PyInstaller --noconfirm --windowed --onedir --name 'PDF框选重命名' --icon app.ico --add-data 'app.ico;.' --collect-data rapidocr_onnxruntime --collect-data rapidocr --collect-binaries onnxruntime --exclude-module tkinter --exclude-module pymupdf4llm --exclude-module pymupdf_layout app.py
if ($LASTEXITCODE -ne 0) { throw '构建失败' }
Write-Host '构建完成：dist\PDF框选重命名'
