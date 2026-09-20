# 兼容转发 —— 实际脚本在 scripts\windows\download_subtitle.ps1
#
# ☠️ 别删。2026-08-10 的目录重构把这些 .ps1 挪进了 scripts\windows\，但**已经
# 装好的用户**桌面上那几个 .bat 里内嵌的是仓库**根目录**的绝对路径。删掉这层
# 转发，他们每一个快捷方式都会报"找不到文件"——包括更新脚本自己，于是连
# "更新一下就好了"这条路都没有，只能手工重跑 install.ps1 才能修好。
#
# ☠️ 参数一律用 $args 原样透传，**别再把参数名一个个列出来**。
# 原来这里声明了 param(Url/OutputDir/NoSummary) 三个，而真脚本还有
# SourceLanguage / TargetLanguage / SubtitleMode，以及 Input/Path/File 三个
# 别名——从根目录调用时那几个参数直接报"找不到参数"。列举式转发每次给真脚本
# 加参数都得同步改这里，而漏改**只在用到那个参数时才暴露**。
$ErrorActionPreference = "Stop"
$Target = Join-Path $PSScriptRoot "scripts\windows\download_subtitle.ps1"
& powershell -NoProfile -ExecutionPolicy Bypass -File $Target @args
exit $LASTEXITCODE
