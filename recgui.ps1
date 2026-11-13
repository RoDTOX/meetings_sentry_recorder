# Screen recorder GUI: pick monitor / all monitors / custom region, audio, quality. Stop with the Stop button.
param([switch]$Tray)
$mutex = New-Object Threading.Mutex($false, 'Local\AmberScreenRecorderGui')
$showEvt = New-Object Threading.EventWaitHandle($false, 'AutoReset', 'Local\AmberScreenRecorderShow')
if (-not $mutex.WaitOne(0)) { [void]$showEvt.Set(); exit }   # already running in the tray: ask it to show its window
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -Namespace W -Name U -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool SetWindowDisplayAffinity(IntPtr h, uint a);
'@
[void][W.U]::SetProcessDPIAware()
[Windows.Forms.Application]::EnableVisualStyles()

$screens = [Windows.Forms.Screen]::AllScreens
$vs = [Windows.Forms.SystemInformation]::VirtualScreen
$videos = [Environment]::GetFolderPath('MyVideos')

function ConvertTo-CliTarget($key, $rect) {
    # The GUI is system-DPI-aware (virtualized coords); the CLI uses physical pixels. Map via the monitor list.
    if ($key -eq 'all') { return 'all' }
    $mons = @((Invoke-Cli @('devices')).monitors)
    $order = @($screens | Sort-Object @{ e = { -[int]$_.Primary } }, @{ e = { $_.Bounds.X } })
    $cx = $rect.X + $rect.Width / 2; $cy = $rect.Y + $rect.Height / 2
    $si = 0
    for ($i = 0; $i -lt $order.Count; $i++) { if ($order[$i].Bounds.Contains([int]$cx, [int]$cy)) { $si = $i; break } }
    if ($key -ne 'region') { return "monitor:$($si + 1)" }
    $b = $order[$si].Bounds; $m = $mons[$si]
    $sx = $m.w / $b.Width; $sy = $m.h / $b.Height
    $x = [int]($m.x + ($rect.X - $b.X) * $sx); $y = [int]($m.y + ($rect.Y - $b.Y) * $sy)
    $w = [int][Math]::Min($rect.Width * $sx, $m.x + $m.w - $x); $h = [int][Math]::Min($rect.Height * $sy, $m.y + $m.h - $y)
    "region:$x,$y,$w,$h"
}

function Select-Region {
    $f = New-Object Windows.Forms.Form
    $f.FormBorderStyle = 'None'; $f.StartPosition = 'Manual'; $f.Bounds = $vs
    $f.TopMost = $true; $f.Opacity = 0.35; $f.BackColor = 'Black'; $f.Cursor = 'Cross'; $f.KeyPreview = $true
    $st = @{ p0 = $null; r = [Drawing.Rectangle]::Empty }
    $f.Add_KeyDown({ if ($_.KeyCode -eq 'Escape') { $st.r = [Drawing.Rectangle]::Empty; $f.Close() } })
    $f.Add_MouseDown({ $st.p0 = $_.Location })
    $f.Add_MouseMove({
        if ($st.p0) {
            $x = [Math]::Min($st.p0.X, $_.X); $y = [Math]::Min($st.p0.Y, $_.Y)
            $st.r = New-Object Drawing.Rectangle $x, $y, ([Math]::Abs($_.X - $st.p0.X)), ([Math]::Abs($_.Y - $st.p0.Y))
            $f.Invalidate()
        }
    })
    $f.Add_Paint({
        if ($st.r.Width -gt 0) {
            $pen = New-Object Drawing.Pen ([Drawing.Color]::Red), 3
            $_.Graphics.FillRectangle([Drawing.Brushes]::White, $st.r)
            $_.Graphics.DrawRectangle($pen, $st.r)
        }
    })
    $f.Add_MouseUp({ if ($st.p0) { $f.Close() } })
    [void]$f.ShowDialog()
    $r = $st.r
    if ($r.Width -lt 32 -or $r.Height -lt 32) { return $null }
    New-Object Drawing.Rectangle ($r.X + $vs.X), ($r.Y + $vs.Y), $r.Width, $r.Height
}

# ---------- Main form ----------
$form = New-Object Windows.Forms.Form
$form.Text = 'Screen Recorder'; $form.Size = '420,520'; $form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'; $form.MaximizeBox = $false

function Add-Label($t, $y) { $l = New-Object Windows.Forms.Label; $l.Text = $t; $l.Location = "15,$y"; $l.AutoSize = $true; $l.Font = New-Object Drawing.Font('Segoe UI', 9, [Drawing.FontStyle]::Bold); $form.Controls.Add($l) }

Add-Label 'What to record' 12
$radios = @()
$y = 36
$opts = @()
$opts += [pscustomobject]@{ T = "All monitors ($($vs.Width)x$($vs.Height))"; K = 'all' }
for ($i = 0; $i -lt $screens.Count; $i++) {
    $b = $screens[$i].Bounds
    $opts += [pscustomobject]@{ T = "Monitor $($i+1)$(if($screens[$i].Primary){' (primary)'}) - $($b.Width)x$($b.Height)"; K = "m$i" }
}
$opts += [pscustomobject]@{ T = 'Custom region (drag a box)'; K = 'region' }
foreach ($o in $opts) {
    $r = New-Object Windows.Forms.RadioButton; $r.Text = $o.T; $r.Tag = $o.K; $r.Location = "30,$y"; $r.AutoSize = $true
    $form.Controls.Add($r); $radios += $r; $y += 24
}
$radios[1].Checked = $true
$y += 8

function Add-Combo($label, $items, $def) {
    Add-Label $label $script:y
    $c = New-Object Windows.Forms.ComboBox; $c.DropDownStyle = 'DropDownList'; $c.Location = "150,$($script:y - 3)"; $c.Width = 240
    $items | ForEach-Object { [void]$c.Items.Add($_) }
    $c.SelectedItem = $def; if ($null -eq $c.SelectedItem) { $c.SelectedIndex = 0 }
    $form.Controls.Add($c); $script:y += 34; $c
}
$cRes = Add-Combo 'Max height' @('Original', '1080', '1440') '1080'
$cFps = Add-Combo 'FPS' @('24', '30', '60') '30'
$audMap = [ordered]@{
    'Auto: system + mic (follows default devices)' = 'auto'
    'System sound only (follows default output)'   = 'system'
    'Microphone only (follows default input)'      = 'mic'
    'No audio'                                      = 'none'
}
$cAud = Add-Combo 'Audio' @($audMap.Keys) 'Auto: system + mic (follows default devices)'
$cDelay = Add-Combo 'Start delay (s)' @('0', '3', '5', '10') '3'
$cFmt = Add-Combo 'Final file' @('mp4 (remux after stop)', 'mkv') 'mp4 (remux after stop)'

$btn = New-Object Windows.Forms.Button; $btn.Text = 'Start recording'; $btn.Location = "15,$y"; $btn.Size = '375,40'
$btn.BackColor = 'Firebrick'; $btn.ForeColor = 'White'; $btn.Font = New-Object Drawing.Font('Segoe UI', 10, [Drawing.FontStyle]::Bold)
$form.Controls.Add($btn)

# ---------- Sentry (rolling buffer) ----------
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
$cli = Join-Path $PSScriptRoot 'screenrec.py'
$sentryState = Join-Path $env:TEMP 'screenrec_sentry.json'
$sentryDir = if ($env:SCREENREC_SENTRY_DIR) { $env:SCREENREC_SENTRY_DIR } else { Join-Path $env:TEMP 'screenrec_sentry' }

function Invoke-Cli {
    param([string[]]$CliArgs)
    $o = & $py $cli @CliArgs 2>&1 | Out-String
    try { $o | ConvertFrom-Json } catch { [pscustomobject]@{ ok = $false; error = $o } }
}

$y += 55
$sep = New-Object Windows.Forms.Label; $sep.BorderStyle = 'Fixed3D'; $sep.Location = "15,$($y - 8)"; $sep.Size = '375,2'; $form.Controls.Add($sep)
Add-Label 'Sentry (rolling buffer)' $y
$sLbl = New-Object Windows.Forms.Label; $sLbl.Location = "15,$($y + 24)"; $sLbl.Size = '375,32'; $sLbl.Text = 'Sentry: OFF'
$form.Controls.Add($sLbl)
$y += 62
$sBuf = Add-Combo 'Buffer (min)' @('10', '30', '60') '30'
$sFps = Add-Combo 'Sentry FPS' @('2', '5', '10') '5'
$sBtn = New-Object Windows.Forms.Button; $sBtn.Text = 'Start sentry'; $sBtn.Location = "15,$y"; $sBtn.Size = '180,34'; $form.Controls.Add($sBtn)
$sSave = New-Object Windows.Forms.Button; $sSave.Text = 'Save last N min'; $sSave.Location = "210,$y"; $sSave.Size = '110,34'; $form.Controls.Add($sSave)
$sMin = New-Object Windows.Forms.ComboBox; $sMin.DropDownStyle = 'DropDownList'; $sMin.Location = "328,$($y + 6)"; $sMin.Width = '62'
'1', '5', '10', '15', '30' | ForEach-Object { [void]$sMin.Items.Add($_) }; $sMin.SelectedItem = '10'; $form.Controls.Add($sMin)
$sy = $y + 48
$sep2 = New-Object Windows.Forms.Label; $sep2.BorderStyle = 'Fixed3D'; $sep2.Location = "15,$($sy - 8)"; $sep2.Size = '375,2'; $form.Controls.Add($sep2)
Add-Label 'Auto-record calls (voice trigger)' $sy
$vLbl = New-Object Windows.Forms.Label; $vLbl.Location = "15,$($sy + 24)"; $vLbl.Size = '375,32'; $vLbl.Text = 'Voice watch: OFF'
$form.Controls.Add($vLbl)
$y = $sy + 62
$vModeMap = [ordered]@{ 'Conversation (you + others)' = 'conversation'; 'Any voice (even if I am muted)' = 'any' }
$vMode = Add-Combo 'Trigger' @($vModeMap.Keys) 'Conversation (you + others)'
$vCntMap = [ordered]@{ 'Audio only (for transcripts)' = 'audio'; 'Audio + screen images (1 fps)' = 'audio+screen' }
$vCnt = Add-Combo 'Record' @($vCntMap.Keys) 'Audio only (for transcripts)'
$vTrMap = [ordered]@{ 'Transcribe after call (auto language)' = 'auto'; 'Transcribe - Romanian' = 'ro'; 'Transcribe - English' = 'en'; 'No transcript' = 'off' }
$vTr = Add-Combo 'Transcript' @($vTrMap.Keys) 'Transcribe after call (auto language)'
$vHang = Add-Combo 'Stop after silence' @('60 s', '90 s', '180 s', '300 s') '90 s'
$vBtn = New-Object Windows.Forms.Button; $vBtn.Text = 'Start voice watch'; $vBtn.Location = "15,$y"; $vBtn.Size = '255,34'; $form.Controls.Add($vBtn)
$vOpen = New-Object Windows.Forms.Button; $vOpen.Text = 'Open folder'; $vOpen.Location = "280,$y"; $vOpen.Size = '110,34'; $form.Controls.Add($vOpen)
$vOpen.Add_Click({
    $d = if ($env:SCREENREC_DIR) { $env:SCREENREC_DIR } else { Join-Path $env:USERPROFILE 'Videos' }
    $day = Join-Path $d (Get-Date -Format 'yyyy-MM-dd')
    Start-Process explorer.exe $(if (Test-Path $day) { $day } else { $d })
})
$form.ClientSize = "405,$($y + 55)"

function Get-SentryInfo {
    $run = $false; $st = $null
    if (Test-Path $sentryState) {
        try { $st = Get-Content $sentryState -Raw | ConvertFrom-Json; $run = [bool](Get-Process -Id $st.worker_pid -ErrorAction SilentlyContinue) } catch {}
    }
    $segs = @(Get-ChildItem $sentryDir -Filter 'sentry_*.ts' -ErrorAction SilentlyContinue | Sort-Object Name)
    $mb = ($segs | Measure-Object Length -Sum).Sum / 1MB
    $mins = 0
    if ($segs.Count) {
        $t0 = [datetime]::ParseExact($segs[0].BaseName.Substring(7), 'yyyyMMdd_HHmmss', $null)
        $mins = [math]::Round(($segs[-1].LastWriteTime - $t0).TotalMinutes, 1)
    }
    [pscustomobject]@{ Running = $run; Mins = $mins; MB = [math]::Round($mb, 1); Max = if ($st) { $st.buffer_minutes } else { 0 } }
}

function Update-Sentry {
    $i = Get-SentryInfo
    if ($i.Running) {
        $sLbl.Text = "Sentry: ON - buffered $($i.Mins) / $($i.Max) min ($($i.MB) MB)"; $sLbl.ForeColor = 'DarkGreen'; $sBtn.Text = 'Stop sentry'
    } else {
        $sLbl.Text = if ($i.Mins -gt 0) { "Sentry: OFF (old buffer: $($i.Mins) min)" } else { 'Sentry: OFF' }
        $sLbl.ForeColor = 'Black'; $sBtn.Text = 'Start sentry'
    }
    $sSave.Enabled = $i.Mins -gt 0
}

$sBtn.Add_Click({
    $form.Cursor = 'WaitCursor'; $sBtn.Enabled = $false
    if ((Get-SentryInfo).Running) { [void](Invoke-Cli @('sentry-stop')) }
    else {
        $r = Invoke-Cli @('sentry-start', '--buffer-minutes', $sBuf.SelectedItem, '--fps', $sFps.SelectedItem)
        if (-not $r.ok) { [void][Windows.Forms.MessageBox]::Show("$($r.error)`n$($r.log_tail)", 'Sentry') }
    }
    $sBtn.Enabled = $true; $form.Cursor = 'Default'; Update-Sentry
})
$sSave.Add_Click({
    $form.Cursor = 'WaitCursor'
    $r = Invoke-Cli @('sentry-save', '--minutes', $sMin.SelectedItem)
    $form.Cursor = 'Default'
    if ($r.ok) { Start-Process explorer.exe "/select,`"$($r.path)`"" }
    else { [void][Windows.Forms.MessageBox]::Show("$($r.error)", 'Save failed') }
})
function Get-VoiceInfo {
    $f = Join-Path $env:TEMP 'screenrec_voice.json'
    $on = $false
    if (Test-Path $f) { try { $on = [bool](Get-Process -Id (Get-Content $f -Raw | ConvertFrom-Json).pid -ErrorAction SilentlyContinue) } catch {} }
    $st = $null
    if ($on) { try { $st = Get-Content (Join-Path $env:TEMP 'screenrec_voice.status.json') -Raw | ConvertFrom-Json } catch {} }
    [pscustomobject]@{ On = $on; Rec = [bool]($st -and $st.recording); Sec = if ($st) { $st.recording_s } else { 0 }; N = if ($st) { @($st.meetings).Count } else { 0 } }
}
function Update-Voice {
    $v = Get-VoiceInfo
    if ($v.On) {
        $vLbl.Text = if ($v.Rec) { "Voice watch: RECORDING a call ($($v.Sec) s)" } else { "Voice watch: listening ($($v.N) calls saved this session)" }
        $vLbl.ForeColor = if ($v.Rec) { 'Firebrick' } else { 'DarkGreen' }; $vBtn.Text = 'Stop voice watch'
    } else { $vLbl.Text = 'Voice watch: OFF'; $vLbl.ForeColor = 'Black'; $vBtn.Text = 'Start voice watch' }
}
$vBtn.Add_Click({
    $form.Cursor = 'WaitCursor'; $vBtn.Enabled = $false
    if ((Get-VoiceInfo).On) { [void](Invoke-Cli @('voice-stop')) }
    else {
        $vArgs = @('voice-start', '--mode', $vModeMap[[string]$vMode.SelectedItem], '--hangover', (([string]$vHang.SelectedItem) -replace ' s', ''), '--content', $vCntMap[[string]$vCnt.SelectedItem])
        $tr = $vTrMap[[string]$vTr.SelectedItem]
        if ($tr -eq 'off') { $vArgs += '--no-transcribe' } else { $vArgs += @('--language', $tr) }
        $r = Invoke-Cli $vArgs
        if (-not $r.ok) { [void][Windows.Forms.MessageBox]::Show("$($r.error)", 'Voice watch') }
    }
    $vBtn.Enabled = $true; $form.Cursor = 'Default'; Update-Voice
})
$timer = New-Object Windows.Forms.Timer; $timer.Interval = 5000; $timer.Add_Tick({ Update-Sentry; Update-Voice }); $timer.Start()
Update-Sentry; Update-Voice

function Start-Recording {
    $key = ($radios | Where-Object Checked).Tag
    $form.Hide()
    $rect = switch -Wildcard ($key) {
        'all'    { $vs }
        'm*'     { $screens[[int]$key.Substring(1)].Bounds }
        'region' { Select-Region }
    }
    if (-not $rect) { $form.Show(); return }
    $target = ConvertTo-CliTarget $key $rect
    $ext = if ($cFmt.SelectedItem -like 'mp4*') { 'mp4' } else { 'mkv' }
    $out = Join-Path $videos ("rec_{0:yyyyMMdd_HHmmss}.{1}" -f (Get-Date), $ext)
    $maxH = if ($cRes.SelectedItem -eq 'Original') { '0' } else { $cRes.SelectedItem }

    # countdown / control window (excluded from the capture)
    $ctl = New-Object Windows.Forms.Form
    $ctl.FormBorderStyle = 'FixedToolWindow'; $ctl.Text = 'REC'; $ctl.Size = '230,120'; $ctl.TopMost = $true
    $ctl.StartPosition = 'Manual'; $ctl.Location = "$($screens[0].Bounds.Right - 250),$($screens[0].Bounds.Top + 20)"
    $lbl = New-Object Windows.Forms.Label; $lbl.Location = '10,10'; $lbl.Size = '200,30'; $lbl.Font = New-Object Drawing.Font('Segoe UI', 14, [Drawing.FontStyle]::Bold)
    $stop = New-Object Windows.Forms.Button; $stop.Text = 'Stop'; $stop.Location = '10,45'; $stop.Size = '195,30'
    $ctl.Controls.AddRange(@($lbl, $stop)); $ctl.Show()
    [void][W.U]::SetWindowDisplayAffinity($ctl.Handle, 0x11)

    $delay = [int]$cDelay.SelectedItem
    for ($i = $delay; $i -gt 0; $i--) { $lbl.Text = "Starting in $i..."; $ctl.Refresh(); Start-Sleep 1 }

    $r = Invoke-Cli @('start', '--target', $target, '--audio', $audMap[[string]$cAud.SelectedItem], '--fps', $cFps.SelectedItem,
        '--max-height', $maxH, '--format', $ext, '--out', $out)
    if (-not $r.ok) {
        $ctl.Dispose()
        [void][Windows.Forms.MessageBox]::Show("$($r.error)`n$($r.log_tail)", 'Start failed')
        $form.Show(); return
    }
    $t0 = Get-Date
    $stopped = @{ v = $false }
    $stop.Add_Click({ $stopped.v = $true })
    $ctl.Add_FormClosing({ $stopped.v = $true })
    $lastChk = Get-Date
    while (-not $stopped.v) {
        $d = (Get-Date) - $t0
        $lbl.Text = "REC {0:hh\:mm\:ss}" -f $d; $lbl.ForeColor = 'Red'
        [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 200
        if (((Get-Date) - $lastChk).TotalSeconds -ge 5) {
            $lastChk = Get-Date
            if (-not (Invoke-Cli @('status')).recording) { break }
        }
    }
    $lbl.Text = 'Finalizing...'; $ctl.Refresh()
    [void](Invoke-Cli @('stop'))
    $ctl.Dispose()

    $final = $out
    $size = if (Test-Path $final) { '{0:N1} MB' -f ((Get-Item $final).Length / 1MB) } else { 'no file' }
    [void][Windows.Forms.MessageBox]::Show("Saved:`n$final`n($size)", 'Recording done')
    Start-Process explorer.exe "/select,`"$final`""
    $form.Show()
}

$btn.Add_Click({ Start-Recording })

# ---------- Tray icon ----------
function New-DotIcon($color, $color2 = $null, $glyph = '') {
    $bmp = New-Object Drawing.Bitmap 32, 32
    $g = [Drawing.Graphics]::FromImage($bmp); $g.SmoothingMode = 'AntiAlias'
    $g.FillEllipse((New-Object Drawing.SolidBrush $color), 3, 3, 26, 26)
    if ($color2) { $g.FillPie((New-Object Drawing.SolidBrush $color2), 3, 3, 26, 26, 270, 180) }   # right half
    $g.DrawEllipse((New-Object Drawing.Pen ([Drawing.Color]::White), 2), 3, 3, 26, 26)
    $white = New-Object Drawing.SolidBrush ([Drawing.Color]::White)
    if ($glyph -eq 'eye') { $g.FillEllipse($white, 11, 11, 10, 10) }          # sentry: camera lens
    elseif ($glyph -eq 'mic') { $g.FillRectangle($white, 13, 8, 6, 12); $g.FillRectangle($white, 15, 21, 2, 4) }   # voice watch: microphone
    $g.Dispose()
    [Drawing.Icon]::FromHandle($bmp.GetHicon())
}
$icoOff = New-DotIcon ([Drawing.Color]::Gray)
$icoRec = New-DotIcon ([Drawing.Color]::Red)
$icoSentry = New-DotIcon ([Drawing.Color]::RoyalBlue) $null 'eye'
$icoVoice = New-DotIcon ([Drawing.Color]::SeaGreen) $null 'mic'
$icoBoth = New-DotIcon ([Drawing.Color]::RoyalBlue) ([Drawing.Color]::SeaGreen) ''
$form.Icon = $icoOff

$ni = New-Object Windows.Forms.NotifyIcon   # not $tray: PowerShell variables are case-insensitive and -Tray is a parameter
$ni.Icon = $icoOff; $ni.Text = 'Screen Recorder'; $ni.Visible = $true
$menu = New-Object Windows.Forms.ContextMenuStrip
$miOpen = $menu.Items.Add('Open')
$miSentry = $menu.Items.Add('Start sentry')
$miVoice = $menu.Items.Add('Start voice watch')
$miSave = $menu.Items.Add('Save last 10 min')
[void]$menu.Items.Add('-')
$miExit = $menu.Items.Add('Exit')
$ni.ContextMenuStrip = $menu

$state = @{ exit = $false }
function Show-Main { $form.Show(); $form.WindowState = 'Normal'; $form.Activate() }
$ni.Add_DoubleClick({ Show-Main })
$miOpen.Add_Click({ Show-Main })
$miSentry.Add_Click({ $sBtn.PerformClick() })
$miVoice.Add_Click({ $vBtn.PerformClick() })
$miSave.Add_Click({
    $r = Invoke-Cli @('sentry-save', '--minutes', '10')
    if ($r.ok) { $ni.ShowBalloonTip(3000, 'Sentry', "Saved: $($r.path)", 'Info'); Start-Process explorer.exe "/select,`"$($r.path)`"" }
    else { $ni.ShowBalloonTip(4000, 'Sentry save failed', "$($r.error)", 'Error') }
})
$miExit.Add_Click({ $state.exit = $true; $form.Close() })

# close (X) and minimize hide to tray; sentry keeps running in the background
function Hide-ToTray { $form.Hide(); $ni.ShowBalloonTip(1500, 'Screen Recorder', 'Still running in the tray (double-click to open)', 'Info') }
$form.Add_FormClosing({
    if (-not $state.exit -and $_.CloseReason -eq 'UserClosing') { $_.Cancel = $true; Hide-ToTray }
})
$form.Add_Resize({ if ($form.WindowState -eq 'Minimized') { Hide-ToTray } })

# keep tray icon/menu in sync with sentry status
$timer.Add_Tick({
    $on = (Get-SentryInfo).Running
    $vi = Get-VoiceInfo
    $ni.Icon = if ($vi.Rec) { $icoRec } elseif ($on -and $vi.On) { $icoBoth } elseif ($on) { $icoSentry } elseif ($vi.On) { $icoVoice } else { $icoOff }
    $ni.Text = if ($vi.Rec) { 'Screen Recorder - recording a call' } elseif ($on -and $vi.On) { 'Screen Recorder - sentry + voice watch' } elseif ($on) { 'Screen Recorder - sentry ON' } elseif ($vi.On) { 'Screen Recorder - voice watch (listening)' } else { 'Screen Recorder' }
    $miVoice.Text = if ($vi.On) { 'Stop voice watch' } else { 'Start voice watch' }
    $miSentry.Text = if ($on) { 'Stop sentry' } else { 'Start sentry' }
    $miSave.Enabled = (Get-SentryInfo).Mins -gt 0
})

$showTimer = New-Object Windows.Forms.Timer; $showTimer.Interval = 500
$showTimer.Add_Tick({ if ($showEvt.WaitOne(0)) { Show-Main } }); $showTimer.Start()

# Windows 11 hides new tray icons in the "^" overflow: mark ours as always visible
$promote = New-Object Windows.Forms.Timer; $promote.Interval = 3000
$promote.Add_Tick({
    $promote.Stop()
    try {
        Get-ChildItem 'HKCU:\Control Panel\NotifyIconSettings' -ErrorAction Stop | ForEach-Object {
            $p = Get-ItemProperty $_.PSPath
            if ($p.ExecutablePath -like '*\powershell.exe' -and $p.IsPromoted -ne 1) { Set-ItemProperty $_.PSPath -Name IsPromoted -Value 1 -Type DWord }
        }
    } catch {}
})
$promote.Start()

if ($Tray) { $form.Add_Shown({ $form.Hide() }) }
[Windows.Forms.Application]::Run($form)   # not ShowDialog: hiding a modal dialog would end it
$ni.Visible = $false; $ni.Dispose()
