# 长篇小说导入

上传和导入接口按项目的 `knowledge_pipeline` 校验原文。字符数不包含空白字符。

| 项目流水线 | 上传文件上限 | 开始导入时的文件上限 | 正文字符上限 |
| --- | --- | --- | --- |
| `structured_v1` | 8 MiB | 8 MiB | 1,000,000 |
| 其他值或未设置 | 512 KiB | 1 MiB | 100,000 |

DOCX 在调用 python-docx 解析前检查 ZIP 中所有条目的解压后大小之和，超过 50 MiB 时拒绝。这一限制适用于所有流水线，与压缩文件大小和正文字数上限独立。超限上传不会替换已有文件。

结构化流水线保留完整原文，并使用现有章节与文本块划分处理长篇内容。上述上限控制文件资源占用，不代表模型能够一次处理整本小说，也不保证后续 AI 步骤自动完成。

## 外观证据修复

角色外观提取读取合并后已验证的 `evidence_text`，并兼容旧的 `quote` 字段。外观缓存版本升至 4，旧版本缓存不会再用于生成缺少原文证据的外观结果。已有非空外观字段仍按现有规则保留，不会自动覆盖用户编辑内容。

## 后续 AI 分析的限制

文本请求预算守卫**默认开启**（保守默认窗口 131072），覆盖 PydanticAI 与 LiteLLM 两条文本通道：超限时在发包前抛出 `MODEL_CONTEXT_BUDGET_EXCEEDED`，错误信息含可操作建议，不再静默地在网关侧失败。

- 按实际网关模型窗口覆盖 `NEWAPI_TEXT_CONTEXT_TOKENS`；或用 `MODEL_CONTEXT_WINDOWS_JSON` 按网关别名分别设置，如 `{"DC-hermes-LLM":200000}`。
- 设为 `NEWAPI_TEXT_CONTEXT_TOKENS=0` 可关闭校验（旧行为）。
- `NEWAPI_TEXT_OUTPUT_TOKENS` 是默认输出 token 预算；窗口必须大于输出预算 + 1024。
- 守卫只估算文本（图像/Base64 传输数据不计入），且不会截断原文。

长篇导入后，某些汇总步骤仍可能超过实际模型窗口。**自动分块（map-reduce）** 会逐步接入这些调用点，把"整段失败"变为"可完成"。在自动分块覆盖到某一步之前，该步超限会明确报错，部署者据此分批或调整预算。此前完成的分块提取仍可能已经产生调用费用。

## 回归验证

```bash
uv run pytest tests/test_api_ingest_chapter_preview.py tests/test_structured_ingest.py tests/test_structured_builders.py tests/test_upload_sanitization.py tests/test_api_asset_upload_offload.py
```

覆盖长文本完整保存、上传和开始导入的边界、旧流水线限制、DOCX 总解压大小限制、原文件保护，以及角色外观提示词与证据缓存。
