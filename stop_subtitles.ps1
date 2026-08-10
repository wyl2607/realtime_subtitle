# 兼容转发 —— 实际脚本在 scripts\windows\stop_subtitles.ps1
#
# ☠️ 别删。2026-08-10 的目录重构把这些 .ps1 挪进了 scripts\windows\，但**已经
# 装好的用户**桌面上那几个 .bat 里内嵌的是仓库**根目录**的绝对路径
# （老 install.ps1 生成时就是这么写的）。他们双击「更新字幕.bat」拉到这版之后，
# 5 个快捷方式会全部报"找不到文件"——包括更新脚本自己，只能手工重跑
# install.ps1 才能修好。留着这层转发，老快捷方式继续能用，用户什么都不用做。
#
# 新装的用户不会走到这里：新 install.ps1 生成的 .bat 直接指向 scripts\windows\。
$ErrorActionPreference = "Stop"
$target = Join-Path $PSScriptRoot "scripts\windows\stop_subtitles.ps1"
& powershell -NoProfile -ExecutionPolicy Bypass -File $target
exit $LASTEXITCODE
