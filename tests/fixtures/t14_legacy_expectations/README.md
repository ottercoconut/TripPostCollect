# T14 固化的旧实现预期

T14 删除 fork 子模块 `tools/MediaCrawler`、私有桥 E（`scripts/mediacrawler_export_entrypoint.py`）与
`platforms/_fork_bridge.py` 之前，把各卡“旧实现与根实现逐项对照”中旧侧的真实输出固化到这里。
删除之后，根侧用例只运行根实现并与这些文件比较，不再“新实现与自身比较”。

## 来源

`manifest.json` 记录：生成日期、生成命令、fork 提交（`fork_commit`）、E 文件 sha256、fork 内全部
`.py` 源的合并 sha256，以及每个文件的 sha256 和产生它的原双轨测试（`source_tests`，可多条旧路径共用
一份预期，例如冻结 T 卡 fixture 与 fork 工厂两条旧路径结果逐字节相同）。

生成方式（fork/E 仍在时，经本机测试沙箱，DIR 需可写）：

```bash
pytest -p pytest_asyncio.plugin \
  -m t14_legacy_guard \
  --t14-write-legacy-expectations=DIR \
  tests/test_adapter_t03.py tests/test_adapter_t05.py tests/test_adapter_t05_bridge.py \
  tests/test_adapter_t06.py tests/test_adapter_t06_bridge.py tests/test_adapter_t07.py \
  tests/test_adapter_t09.py tests/test_shared_staging.py
```

守卫用例（标记 `t14_legacy_guard`）不带该选项时把旧侧当场结果编码后与本目录逐字节比较；带选项时改为
写到 DIR 并写 `DIR/manifest.json`。人工审阅差异后整体替换本目录。编码与归一规则见
`tests/support/legacy_expectations.py`。

## 比较粒度

- 与原对照相同：原对照的归一（小红书 `normalized()`）、有意偏离的替换（#49 作者原值、#52 作者页取数、
  #55 主页关闭异常类名）都在生成旧侧时照原样生效；根侧沿用同一 drive 与 `==` 语义。
- 新增的只有四项，两侧同样处理：dict 按键排序书写（`==` 本与顺序无关，避免目录遍历顺序随 OS 不同）；检出根与临时目录换成占位符；超过 16 KiB 的 bytes（stealth 脚本原文）
  只存 sha256 与长度；键名含 `avatar` 的值只存摘要，文件不含头像 URL。
- `shared_staging`：根内容出口已无评论/创作者写出（T12 退出切片），根侧比较排除基线中的
  `_comments_`/`_creators_` 文件；根配置在构造时冻结，换目录即构造新 sink。

## T14-C

删除 fork/E 时同批删除守卫用例与只测旧桥的用例（标记 `t14_legacy_guard` / `t14_legacy_only`），
本目录与根侧比较用例保留。之后若根实现有意改变行为，须登记偏离并另行更新预期，不得用根实现重新生成。
