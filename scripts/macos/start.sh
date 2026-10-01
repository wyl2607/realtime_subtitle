#!/usr/bin/env bash
# 转发壳：start.sh → start_and_update_subtitles.sh（短名是文档和用户习惯里的叫法，实现跟
# Windows 那边的 .ps1 同名文件放在一起，便于两边对照）。
#
# ☠️ 参数一律 "$@" 原样透传，**别列举参数名**：真脚本以后加了新参数，
# 列举式转发会把它变成"从短名调用时报找不到参数"，而且**只在真用到那个参数时**
# 才暴露——Windows 根目录那 7 个转发壳就是这么坑过人的（CLAUDE.md 第 4 节第 46 条）。
set -uo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/start_and_update_subtitles.sh" "$@"
