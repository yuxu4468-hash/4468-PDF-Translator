# tools/ — 诊断与实测脚本

这个目录放的是**开发期用来拿数据的脚本**，不是程序运行的一部分。
留下它们的原因：README 与 `docs/` 里那些实测数字（失配率、符号落点、
行边界收益……）都出自这些脚本，留着才能被复核。

跑它们**不需要 API Key**，也不需要网络（除了 OCR 模型下载）。

| 脚本 | 作用 | 需要语料 |
| --- | --- | --- |
| `gen_config_example.py` | 按 `constants.DEFAULT_CONFIG` 重新生成 `config/config.example.json` | 否 |
| `probe_symbol_fonts.py` | 查本机字体对 63 个公式符号的覆盖与"写-读回"是否一致 | 否 |
| `probe_symbol_coverage.py` | 算每个字体覆盖多少符号，决定符号落点的候选顺序 | 否 |
| `probe_table_rows.py` | 量化"按横线切行"能收紧多少段落（决定要不要做这个功能） | **是** |

## 用法

```bat
python tools\gen_config_example.py
python tools\probe_symbol_fonts.py
python tools\probe_symbol_coverage.py

:: 语料目录会被递归扫描，只读不写；建议用一份 PDF 的副本目录，
:: 因为脚本会把"文件名里带 译文/中文 的 PDF"当成自己的产物跳过。
python tools\probe_table_rows.py --base "D:\我的PDF" --limit 50
```

## 约定

- 这些脚本**只读**输入 PDF，产物不落盘（只打印统计）。
- 输出里的中文数值是给人看的，没有机器可读格式 —— 需要的话再让它吐 JSON。
- 改了 `src/` 的核心算法后，这些脚本的数字可能变化；**数字变了要同步更新
  README 与 `docs/` 里的对应段落**，别让文档和实际行为对不上。
