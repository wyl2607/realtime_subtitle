# 下载网络视频、或导入本地视频/音频，并制作字幕（可选单语/双语、英语/德语）。
param(
    # 网络地址或本地文件路径都可以（拖进来的文件路径同样有效）。
    [Alias("Input", "Path", "File")]
    [string]$Url,
    [string]$OutputDir,
    [string]$SourceLanguage,
    [string]$TargetLanguage,
    [string]$SubtitleMode,
    [switch]$NoSummary
)
$ErrorActionPreference = "Stop"
$RepoRoot = (Get-Item $PSScriptRoot).Parent.Parent.FullName
Set-Location $RepoRoot
$Python = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "❌ 还没有安装运行环境（找不到 venv）。请先运行 install.ps1。"
    exit 1
}

# 分享文案里常常不止一个链接（短链 + 活动页 + 下载页）。以前直接取第一个匹配，
# 等于替用户随机决定处理哪个视频；现在超过一个就让他选。
function Get-UrlChoice([string[]]$Candidates) {
    if ($Candidates.Count -eq 1) { return $Candidates[0] }
    Write-Host "这段文字里有多个链接，请选择要处理的那一个："
    for ($i = 0; $i -lt $Candidates.Count; $i++) {
        Write-Host ("  {0}. {1}" -f ($i + 1), $Candidates[$i])
    }
    $Pick = Read-Host "输入序号（直接回车放弃）"
    if ([string]::IsNullOrWhiteSpace($Pick)) { return "" }
    $Index = 0
    if (-not [int]::TryParse($Pick, [ref]$Index)) { return "" }
    if ($Index -lt 1 -or $Index -gt $Candidates.Count) { return "" }
    return $Candidates[$Index - 1]
}

# ☠️ 剪贴板和手动输入走**同一个**解析入口。以前只有剪贴板分支会从分享文案里
# 抽链接，手动粘贴同一段文案则被整段当成地址交给下载器，直接失败。
function Resolve-InputText([string]$Text) {
    if ([string]::IsNullOrWhiteSpace($Text)) { return "" }
    $Text = $Text.Trim().Trim('"')
    if (Test-Path -LiteralPath $Text -PathType Leaf) { return $Text }
    # ☠️ 抠链接的规则只有 Python 那一份（offline.extract_share_urls），这里调过去。
    # 别在这儿再写一套正则：`https?://\S+` 对中文分享文案是错的——中文标点不是
    # 空白字符，`http://xhslink.com/a/xxx，快去看` 会把后半句一起当成 URL。
    $Found = @(& $Python (Join-Path $RepoRoot "download_subtitle.py") $Text --list-urls |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($Found.Count -eq 0) { return $Text }
    return Get-UrlChoice $Found
}

if ([string]::IsNullOrWhiteSpace($Url)) {
    try {
        $ClipboardText = (Get-Clipboard -Raw -ErrorAction Stop).Trim()
        $Url = Resolve-InputText $ClipboardText
        if (-not [string]::IsNullOrWhiteSpace($Url)) {
            Write-Host "已从剪贴板读取视频地址。"
        }
    } catch {
        # 读剪贴板失败就走下面的输入提示。
    }
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    $Url = Resolve-InputText (Read-Host "请粘贴视频地址或分享文案，或把本地视频文件拖到这里后回车")
} else {
    # -Url 也走同一个解析入口：快捷方式里粘一整段分享文案同样要能用
    $Url = Resolve-InputText $Url
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    Write-Host "❌ 需要一个视频地址或本地文件才能继续。"
    exit 1
}
$IsLocalFile = (-not ($Url -match '^[a-zA-Z][a-zA-Z0-9+.-]*://')) -and (Test-Path -LiteralPath $Url -PathType Leaf)
if ($IsLocalFile) {
    $Url = (Resolve-Path -LiteralPath $Url).Path
    Write-Host "导入本地文件：$Url"
}

# ☠️ 选项编号只是菜单序号，不能直接当语言码用（"1" 不是 en）。
if ([string]::IsNullOrWhiteSpace($SubtitleMode)) {
    Write-Host ""
    Write-Host "字幕形式："
    Write-Host "  1 双语（原文 + 译文，默认）"
    Write-Host "  2 单语（只要译文）"
    $ModePick = (Read-Host "输入序号（直接回车用默认）").Trim()
    switch ($ModePick) {
        "2" { $SubtitleMode = "target" }
        "1" { $SubtitleMode = "bilingual" }
        "" { $SubtitleMode = "bilingual" }
        default {
            # 别默默把看不懂的输入当默认值咽下去，说一声再用默认
            Write-Host "无法识别的选项「$ModePick」，按默认（双语）处理。"
            $SubtitleMode = "bilingual"
        }
    }
}
if ([string]::IsNullOrWhiteSpace($TargetLanguage)) {
    Write-Host ""
    Write-Host "译文语言："
    Write-Host "  1 英语（中文视频默认，直接回车即可）"
    Write-Host "  2 德语"
    $LangPick = (Read-Host "输入序号（直接回车用默认）").Trim()
    switch ($LangPick) {
        "2" { $TargetLanguage = "de" }
        "1" { $TargetLanguage = "en" }
        "" { $TargetLanguage = "" }  # 交给 Python 按源语言定默认
        default {
            Write-Host "无法识别的选项「$LangPick」，按默认处理。"
            $TargetLanguage = ""
        }
    }
}

$ModeLabel = if ($SubtitleMode -eq "target") { "单语（只要译文）" }
             elseif ($SubtitleMode -eq "source") { "只要原文" }
             else { "双语（原文 + 译文）" }
$LangLabel = if ([string]::IsNullOrWhiteSpace($TargetLanguage)) { "按源语言自动决定（中文视频→英语）" }
             elseif ($TargetLanguage -eq "de") { "德语" }
             elseif ($TargetLanguage -eq "en") { "英语" }
             else { $TargetLanguage }
$SourceLabel = if ([string]::IsNullOrWhiteSpace($SourceLanguage)) { "自动识别" } else { $SourceLanguage }
$InputLabel = if ($IsLocalFile) { "本地文件" } else { "网络地址" }
$OutputLabel = if ([string]::IsNullOrWhiteSpace($OutputDir)) { Join-Path $RepoRoot "downloads" } else { $OutputDir }
Write-Host ""
Write-Host "本次任务：$InputLabel / 源语言 $SourceLabel / 译文 $LangLabel / $ModeLabel"
Write-Host "输出目录：$OutputLabel"
Write-Host ""

# ☠️ 一律用参数数组传给 Python，不拼命令行字符串、不用 Invoke-Expression：
# 视频地址来自剪贴板，拼字符串等于把它交给 shell 解析。
$CliArgs = @($Url)
if (-not [string]::IsNullOrWhiteSpace($OutputDir)) {
    $CliArgs += @("--output-dir", $OutputDir)
}
if (-not [string]::IsNullOrWhiteSpace($SourceLanguage)) {
    $CliArgs += @("--source-language", $SourceLanguage)
}
if (-not [string]::IsNullOrWhiteSpace($TargetLanguage)) {
    $CliArgs += @("--target-language", $TargetLanguage)
}
if (-not [string]::IsNullOrWhiteSpace($SubtitleMode)) {
    $CliArgs += @("--subtitle-mode", $SubtitleMode)
}
if ($NoSummary) {
    $CliArgs += "--no-summary"
}
& $Python (Join-Path $RepoRoot "download_subtitle.py") @CliArgs
$ExitCode = $LASTEXITCODE
if ($ExitCode -eq 0) {
    Read-Host "完成。按回车关闭"
} else {
    Read-Host "失败。按回车关闭"
}
exit $ExitCode
