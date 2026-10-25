# 002/005 draft 评审（/sdd-review，2026-10-11）

评审对象：`draft.md`（基线 084c615）。两路独立评审，均为只读、≤600 字，由非 Claude lane 执行，Coordinator（Claude）核对证据后在 draft 末尾「Review Decision」分诊。

- 架构：codex gpt-5.5（reasoning high）
- 安全：codex gpt-5.5（reasoning high）。先派 agy（headless 下 command 权限被自动拒绝，零产出）、再派 grok（读完代码但没写出文件），都没交付，改派 codex。

## A. 架构（lens=architecture）

id:ARCH-01 / severity:major / category:correctness / location:draft.md:24-25,45-50 / evidence:单段失败降级与连续3次才不可用冲突，5xx/超时后的迁移/恢复不可判定。/ suggested_fix:补状态表+route_cases。

id:ARCH-02 / severity:major / category:maintainability / location:NodeClient.swift:4-15;NodeRouter.swift:63-77 / evidence:WS契约需node_id/token_file/busy/on_ac/in_use；云端不进nodes.json，不能复用NodeConfig/评分器。/ suggested_fix:定义Provider/Tier，Groq只共享状态+P5输出。

id:ARCH-03 / severity:major / category:correctness / location:draft.md:66-67,115,130-132 / evidence:样本时钟未解却让TK5/6开工；process_iter只返文本，替换时间会丢。/ suggested_fix:时钟测试作TK4出口。

verdict: needs_fix

## B. 安全（lens=security）

id:S1/severity:major/category:security/location:draft.md:62/evidence:关开关“立即生效”却允许在飞请求完成；requirement.md:46 要求关后不连 Groq，当前段仍会上传。/suggested_fix:关开关即取消HTTP并丢响应。
id:S2/severity:major/category:security/location:draft.md:35/evidence:云端HTTPS走系统/环境代理，未写证书校验、禁verify=false或Surge解密边界，key/音频经过代理。/suggested_fix:保留TLS校验，api.groq.com禁MITM/正文日志。
id:S3/severity:major/category:security/location:realtime_subtitle/config.py:592/evidence:SHOW_PERFORMANCE会把识别原文/译文写进subtitle.log；draft.md:36只禁云端客户端日志正文。/suggested_fix:云端模式正文日志/崩溃报告强制脱敏或关闭。
verdict: needs_fix
