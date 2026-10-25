# Groq 数据保留（README 隐私段的依据）

来源：Groq 官方文档「Your Data in GroqCloud」 https://console.groq.com/docs/your-data （协调者 2026-10-11 查阅）。

- **默认不保留推理请求的数据**（含 audio transcription 端点）。
- 例外：排查影响可靠性的错误、或调查疑似滥用（如绕过限流）时，可能临时记录输入输出，**最长 30 天**（法律要求更长除外）。
- 所有账户都可在控制台 **Data Controls 开启 Zero Data Retention（ZDR）**；开启后连上述可靠性/滥用日志也不保留；推理端点支持 ZDR。
- 保留的数据存放在美国 GCP。
- 官方该页**没有写**是否用于训练；第三方转述称其 DPA 承诺不用 API 数据训练——README 里只写官方页能证实的内容，不写「不训练」。

README 建议写法：「Groq 默认不保留识别请求；排查故障或滥用时可能临时保存最多 30 天。介意的话在 Groq 控制台 Data Controls 打开 Zero Data Retention。」并附官方链接。
