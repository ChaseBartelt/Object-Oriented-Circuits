param(
    [string]$Python,
    [string]$QuartusBin,
    [string]$Source,
    [switch]$ExportOnly
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if (-not $Python) {
    $Python = Join-Path $repo '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $Python)) { $Python = 'python' }
}
if ($Source) {
    $circuitSource = (Resolve-Path -LiteralPath $Source).Path
} else {
    $circuitSource = Join-Path $repo 'examples/digital/HYBRID01.ckt'
}
$generated = Join-Path $PSScriptRoot 'build/HYBRID01.v'
New-Item -ItemType Directory -Force (Split-Path $generated) | Out-Null
Push-Location $repo
try {
    @'
import sys
from pathlib import Path
from circuitlab.tooling.fpga import compile_de10_lite
source, output = map(Path, sys.argv[1:])
hdl, qsf, sdc = compile_de10_lite(source.read_text(encoding="utf-8-sig"))
output.write_text(hdl, encoding="utf-8")
project = output.parent.parent
(project / "HYBRID01.qsf").write_text(qsf, encoding="utf-8")
(project / "HYBRID01.sdc").write_text(sdc, encoding="utf-8")
'@ | & $Python - $circuitSource $generated
    if ($LASTEXITCODE -ne 0) { throw 'OOC export failed; Quartus was not run.' }
} finally { Pop-Location }
Write-Output "Exported $generated"
Write-Output 'Regenerated HYBRID01.qsf and HYBRID01.sdc for this source and its switch synchronization.'
if ($ExportOnly) { return }

if ($QuartusBin) {
    $quartus = Join-Path $QuartusBin 'quartus_sh.exe'
} else {
    $command = Get-Command quartus_sh.exe -ErrorAction SilentlyContinue
    if (-not $command) { throw 'Pass -QuartusBin pointing to your Quartus bin64 folder.' }
    $quartus = $command.Source
}
Push-Location $PSScriptRoot
try {
    & $quartus --flow compile HYBRID01
    if ($LASTEXITCODE -ne 0) { throw 'Quartus compilation failed. Read the reports in build/output_files.' }
} finally { Pop-Location }
$summary = Get-Content -Raw (Join-Path $PSScriptRoot 'build/output_files/HYBRID01.sta.summary')
if ($summary -notmatch '(?m)^Slack\s*:' -or $summary -match '(?m)^Slack\s*:\s*-') {
    throw 'Timing analysis did not report passing slack. Do not program this build; inspect the Timing Analyzer reports.'
}
Write-Output 'Timing checks passed: all reported setup, hold and pulse-width slacks are nonnegative.'
Write-Output "Programming file: $(Join-Path $PSScriptRoot 'build/output_files/HYBRID01.sof')"
