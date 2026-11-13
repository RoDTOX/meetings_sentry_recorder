# Builds ScreenRecorder.exe (windowless launcher with a red-dot icon) next to recgui.ps1. Uses the .NET Framework csc.exe already on Windows.
$root = Split-Path $PSScriptRoot -Parent
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
Add-Type -AssemblyName System.Drawing
$bmp = New-Object Drawing.Bitmap 64, 64
$g = [Drawing.Graphics]::FromImage($bmp); $g.SmoothingMode = 'AntiAlias'
$g.FillEllipse([Drawing.Brushes]::Firebrick, 4, 4, 56, 56)
$g.DrawEllipse((New-Object Drawing.Pen ([Drawing.Color]::White), 4), 4, 4, 56, 56); $g.Dispose()
$png = New-Object IO.MemoryStream; $bmp.Save($png, [Drawing.Imaging.ImageFormat]::Png)
$ico = Join-Path $PSScriptRoot 'icon.ico'
$fs = [IO.File]::Create($ico); $bw = New-Object IO.BinaryWriter $fs
$bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]1)             # ICONDIR
$bw.Write([byte]64); $bw.Write([byte]64); $bw.Write([byte]0); $bw.Write([byte]0)
$bw.Write([uint16]1); $bw.Write([uint16]32); $bw.Write([uint32]$png.Length); $bw.Write([uint32]22)
$bw.Write($png.ToArray()); $bw.Close()
& $csc /nologo /target:winexe /optimize "/win32icon:$ico" "/out:$root\ScreenRecorder.exe" "$PSScriptRoot\Launcher.cs"
if ($LASTEXITCODE -eq 0) { Write-Host "Built $root\ScreenRecorder.exe" -ForegroundColor Green }
