---
name: 问题反馈 / Bug report
about: 装不上、跑不起来、字幕效果异常都用这个
title: "[bug] 一句话描述问题"
labels: bug
---

<!-- 提示：把这个模板直接丢给你电脑上的 AI 助手（Claude Code 等），
     让它替你跑命令、填内容，是最省事的方式。 -->

## 现象

（发生了什么？期望是什么？什么时候开始的——刚装完就这样，还是某次更新后？）

## 复现步骤

1.
2.

## 环境信息（在仓库目录跑，粘贴输出）

```powershell
Select-String -Path version.py -Pattern '__version__'
git rev-parse --short HEAD
(Get-CimInstance Win32_OperatingSystem).Caption
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
Get-Content config_local.py -ErrorAction SilentlyContinue
```

粘贴处：

```
（输出贴这里）
```

## 日志（必带，没有日志基本没法排查）

> ⚠️ 贴之前扫一眼：如果你为了排障打开过 `SHOW_PERFORMANCE = True`，
> subtitle.log 里会有**识别出的字幕正文**（本程序抓的是系统全部声音，
> 可能包含语音通话内容）。不想公开的段落直接删掉再贴，不影响排查。

```powershell
Get-Content subtitle.err.log -Tail 50
Get-Content subtitle.log -Tail 80
```

粘贴处：

```
（输出贴这里）
```
