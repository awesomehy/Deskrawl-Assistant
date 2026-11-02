# Render our vector mark using Windows GDI+; no extra runtime dependencies.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$iconDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) 'deskrawl_assistant/web'
$iconSizes = @(16,20,24,32,40,48,64,128,256)
$iconPayloads = @()
function Icon-Brush([string]$color) { return New-Object System.Drawing.SolidBrush([System.Drawing.ColorTranslator]::FromHtml($color)) }
foreach ($iconSize in $iconSizes) {
    $iconBitmap = New-Object System.Drawing.Bitmap($iconSize,$iconSize)
    $iconGraphics = [System.Drawing.Graphics]::FromImage($iconBitmap)
    $iconGraphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $iconGraphics.ScaleTransform(($iconSize / 128.0),($iconSize / 128.0))
    $iconBack = Icon-Brush '#191e28'; $iconShield = Icon-Brush '#302b26'
    $iconGold = Icon-Brush '#f0cf96'; $iconAccent = Icon-Brush '#ba935a'; $iconSpark = Icon-Brush '#f8e4bc'
    $iconPen = New-Object System.Drawing.Pen([System.Drawing.ColorTranslator]::FromHtml('#ba935a'),3)
    $iconPen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $iconShape = New-Object System.Drawing.Drawing2D.GraphicsPath
    $iconShape.AddArc(4,4,58,58,180,90); $iconShape.AddArc(66,4,58,58,270,90)
    $iconShape.AddArc(66,66,58,58,0,90); $iconShape.AddArc(4,66,58,58,90,90)
    $iconShape.CloseFigure(); $iconGraphics.FillPath($iconBack,$iconShape)
    $iconShape.Reset()
    $iconShape.AddLine(64,19,102,33); $iconShape.AddLine(102,33,102,62)
    $iconShape.AddBezier(102,62,102,86,85,104,64,114)
    $iconShape.AddBezier(64,114,43,104,26,86,26,62)
    $iconShape.AddLine(26,62,26,33); $iconShape.CloseFigure()
    $iconGraphics.FillPath($iconShield,$iconShape); $iconGraphics.DrawPath($iconPen,$iconShape)
    $iconShape.Reset(); $iconShape.FillMode = [System.Drawing.Drawing2D.FillMode]::Alternate
    $iconShape.AddLine(44,39,61,39); $iconShape.AddBezier(61,39,81,39,90,49,90,65)
    $iconShape.AddBezier(90,65,90,81,81,91,61,91); $iconShape.AddLine(61,91,44,91); $iconShape.CloseFigure()
    $iconShape.AddLine(56,50,56,80); $iconShape.AddLine(56,80,61,80)
    $iconShape.AddBezier(61,80,72,80,77,75,77,65); $iconShape.AddBezier(77,65,77,55,72,50,61,50)
    $iconShape.CloseFigure(); $iconGraphics.FillPath($iconGold,$iconShape)
    $iconGraphics.FillRectangle($iconAccent,32,55,7,21)
    $iconShape.Reset()
    $iconShape.AddLine(99,17,102,24); $iconShape.AddLine(102,24,109,27)
    $iconShape.AddLine(109,27,102,30); $iconShape.AddLine(102,30,99,37)
    $iconShape.AddLine(99,37,96,30); $iconShape.AddLine(96,30,89,27)
    $iconShape.AddLine(89,27,96,24); $iconShape.CloseFigure()
    $iconGraphics.FillPath($iconSpark,$iconShape)
    $iconStream = New-Object System.IO.MemoryStream
    $iconBitmap.Save($iconStream,[System.Drawing.Imaging.ImageFormat]::Png)
    $iconPayloads += ,$iconStream.ToArray()
    if ($iconSize -eq 256) { $iconBitmap.Save((Join-Path $iconDirectory 'assistant-icon.png'),[System.Drawing.Imaging.ImageFormat]::Png) }
    foreach ($iconDisposable in @($iconStream,$iconShape,$iconPen,$iconSpark,$iconAccent,$iconGold,$iconShield,$iconBack,$iconGraphics,$iconBitmap)) { $iconDisposable.Dispose() }
}
$iconFile = [System.IO.File]::Create((Join-Path $iconDirectory 'assistant.ico'))
$iconWriter = New-Object System.IO.BinaryWriter($iconFile)
try {
    $iconWriter.Write([uint16]0); $iconWriter.Write([uint16]1); $iconWriter.Write([uint16]$iconSizes.Count)
    $iconOffset = 6 + 16 * $iconSizes.Count
    for ($iconIndex=0; $iconIndex -lt $iconSizes.Count; $iconIndex++) {
        $iconDimension = if ($iconSizes[$iconIndex] -eq 256) { 0 } else { $iconSizes[$iconIndex] }
        $iconWriter.Write([byte]$iconDimension); $iconWriter.Write([byte]$iconDimension)
        $iconWriter.Write([uint16]0); $iconWriter.Write([uint16]1); $iconWriter.Write([uint16]32)
        $iconWriter.Write([uint32]$iconPayloads[$iconIndex].Length); $iconWriter.Write([uint32]$iconOffset)
        $iconOffset += $iconPayloads[$iconIndex].Length
    }
    foreach ($iconPayload in $iconPayloads) { $iconWriter.Write([byte[]]$iconPayload) }
} finally { $iconWriter.Dispose(); $iconFile.Dispose() }
Write-Output 'Generated multi-resolution assistant.ico and assistant-icon.png.'
