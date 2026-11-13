# Screen recorder: video + audio, HEVC (hardware if available), AAC. Press q in the console to stop.
param(
    [ValidateSet('1080','1440')][string]$Res = '1080',
    [int]$Fps = 30,
    [string]$Audio = 'Microphone (G433 Gaming Headset)',   # dshow device name; 'none' disables
    [string]$Out = (Join-Path ([Environment]::GetFolderPath('MyVideos')) ("rec_{0:yyyyMMdd_HHmmss}.mp4" -f (Get-Date))),
    [int]$Seconds = 0,   # 0 = until q is pressed
    [ValidateSet('auto','qsv','nvenc','amf','x265','x264')][string]$Encoder = 'auto'
)
$h = [int]$Res; $w = [int]($h * 16 / 9)
$enc = ffmpeg -hide_banner -encoders 2>&1 | Out-String
if ($Encoder -eq 'auto') {
    $Encoder = foreach ($e in 'nvenc','qsv','amf') {
        if ($enc -match "hevc_$e") {
            ffmpeg -hide_banner -loglevel quiet -f lavfi -i "color=s=1280x720:d=0.2" -pix_fmt yuv420p -c:v "hevc_$e" -f null - 2>$null
            if ($LASTEXITCODE -eq 0) { $e; break }
        }
    }
    if (-not $Encoder) { $Encoder = 'x265' }
}
$v = switch ($Encoder) {
    'qsv'   { '-c:v','hevc_qsv','-global_quality','26','-preset','medium','-look_ahead','0','-bf','0' }
    'nvenc' { '-c:v','hevc_nvenc','-preset','p5','-tune','hq','-rc','vbr','-cq','28','-b:v','0' }
    'amf'   { '-c:v','hevc_amf','-quality','balanced','-rc','cqp','-qp_i','26','-qp_p','28' }
    'x265'  { '-c:v','libx265','-preset','veryfast','-crf','28' }
    'x264'  { '-c:v','libx264','-preset','veryfast','-crf','23' }
}
$in = @('-f','gdigrab','-framerate',$Fps,'-draw_mouse','1','-i','desktop')
if ($Audio -ne 'none') { $in += '-f','dshow','-audio_buffer_size','50','-i',"audio=$Audio" }
$args2 = @('-hide_banner','-y','-thread_queue_size','1024') + $in +
    @('-vf',"fps=${Fps},scale=${w}:${h}:flags=bicubic,format=yuv420p") + $v +
    @('-tag:v','hvc1','-c:a','aac','-b:a','128k','-af','aresample=async=1','-movflags','+faststart',$Out)
if ($Encoder -eq 'x264') { $args2 = $args2 | ForEach-Object { $_ -replace '^hvc1$','avc1' } }
if ($Seconds -gt 0) { $args2 = $args2[0..($args2.Count-2)] + @('-t',$Seconds,$Out) }
Write-Host "Encoder: $Encoder | ${w}x$h@$Fps | $Out`nPress q to stop." -ForegroundColor Cyan
ffmpeg @args2
Write-Host "Saved: $Out" -ForegroundColor Green
