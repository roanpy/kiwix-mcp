# kiwix-mcp

[English](README.md) | 简体中文

一个 MCP 服务器，让 AI Agent 离线访问 Kiwix ZIM 存档：维基百科、维基词典、维基教科书、
Stack Exchange 数据转储，以及任何其他 ZIM 集合。全部针对本地磁盘文件运行，查询时不联网、
不需要账号、不需要 API Key。

核心价值在于**可溯源**。Agent 不依赖模型记忆，而是检索一份带日期的离线快照、读取真实条目
正文，并通过 `kiwix://` URI 指回它来自哪个存档。服务器是只读的：不会写入任何 ZIM 文件。

需要 Python 3.12 或更高版本（已在 3.12、3.13、3.14 上测试），以及 macOS 或 Linux——图片
缓存依赖 POSIX 文件锁。

## 安装

```bash
uv sync --frozen
uv run --frozen python server.py
```

## 获取存档并指定目录

从 [Kiwix 官方库](https://library.kiwix.org/) 下载 ZIM 文件，例如
`wikipedia_zh_all_maxi_2026-08.zim`。存档直接从磁盘读取，不会被修改。

默认存档目录是 `~/.local/share/kiwix-mcp/archives`：

```bash
mkdir -p ~/.local/share/kiwix-mcp/archives
```

或者把服务器指向你存放存档的位置：

```bash
export KIWIX_ARCHIVE_DIR=/path/to/your/zim/files
```

单文件 `.zim` 和分卷 `.zimaa` 存档都会被识别。如果存档确实存在却没被识别，调用
`list_archives`——它会报告实际读取的目录以及每个存档的错误信息。

## 配置 MCP 客户端

服务器通过 stdio 讲 MCP 协议。在你的客户端中注册，例如：

```json
{
  "mcpServers": {
    "kiwix": {
      "command": "uv",
      "args": ["run", "--frozen", "python", "server.py"],
      "cwd": "/path/to/kiwix-mcp",
      "env": { "KIWIX_ARCHIVE_DIR": "/path/to/your/zim/files" }
    }
  }
}
```

每次客户端启动都会获得独立进程，因此多个客户端之间不共享状态。单个会话内的调用是串行的，
这让 libzim 的访问行为可预测。客户端侧的懒启动和空闲超时与本服务器配合良好；服务器自身也
会在空闲 10 分钟后退出（见 `KIWIX_MCP_IDLE_TIMEOUT`）。

服务器使用 MCP 2.x 的底层 `Server` API，同时保留旧版 initialize/session 路径以兼容较老的
MCP 客户端。工具名和 stdio 命令属于对外接口，保持稳定。

工具：

- `list_archives`：列出 ZIM 元数据，包括可用的检索索引、`flavour`（例如 `maxi` 或
  `nopic`）、`has_main_entry`，以及用于选择存档的 `main_entry_path`。
- `search`：在选定存档中检索，精确标题优先，支持分页、估算命中数和每条结果的
  `match_type`；用 `archive_id="*"` 做小规模跨库对照。`mode` 参数可选 `auto`（默认）、
  `fulltext` 或 `title`。`fulltext` 是严格模式，遇到没有全文索引的存档会失败；`title`
  强制只走标题建议查询。可选的 `language` 和 `flavour` 仅在跨库模式下过滤存档。
  重定向别名会在分页前去重。续页时用相同的 query、存档选择、mode 和过滤条件配合
  `next_offset`。检索偏移量上限为 0–1000，超出这个窗口请收窄查询词。
  跨库结果的顺序是精确标题优先，其后按存档与变体顺序排列，并非全局可比较的相关性得分。
  `estimated_matches` 是各存档中每个变体最大估算值的累加，不是精确的唯一结果数。
- `inspect_article`：返回干净的导语、带章节 URI 的标题大纲、MediaWiki 信息框事实、重定向
  与规范路径元数据、存档语言与日期，以及引用数量，而不返回全文。
- `read_article`：返回干净且长度有界的正文，支持 `next_offset` 续读、按标题或 anchor 选择
  章节（`section`）、有上限的图片元数据（`primary` 标记去重与过滤小图标后保留的第一张图）、
  通过 `image_offset` 的图片元数据分页，以及相关链接。`see_also` 是精选的「参见」集合，
  `links` 是范围更广的导语与正文链接集合；即使条目存在参见章节也会返回 `links`，以保证
  按链接跳转时的出边数量。同一路径不会同时出现在两个列表中，每个条目都带 `article_path`、
  `title` 和 `uri`。
- `list_references`：分页浏览 MediaWiki 引用、注释和保留的外部链接，或用 `citation_label`
  选择可见标记，例如 `1`、`a` 或 `note 1`。
- `extract_image`：返回原生 MCP 图片内容，并附带临时 `file_path` 作为无法渲染 MCP 图片
  内容的客户端的回退方案。除非需要别的图片，否则优先选 `primary=True` 的条目，然后把该
  条目的 `image_path` 传给本工具。临时图片会去重，并最多保留最近使用的 16 个文件。

MCP 资源（2.x）：

- 资源模板：`kiwix://{archive_id}/{+article_path}`
- 加上标题片段即可只读某一节，例如
  `kiwix://archive.zim/Artificial_intelligence#Knowledge_representation`。
- `list_resources`：把每个存档的主入口作为 `text/plain` 资源返回。
- `read_resource`：通过稳定 URI 返回纯文本正文。文本在 50,000 字符处截断
  （`MAX_RESOURCE_CHARS`）；发生截断时，结果的 `meta` 和 `contents[0].meta` 会带上
  `truncated` 和 `next_offset_hint`（长条目请改用 `read_article` 配合 `next_offset`）。
- 无法使用图片的客户端仍然可以通过 `read_resource` 加载正文。

0.1.0 之前的版本默认使用 `~/.chroma_db/kiwix/archives`。当该路径存在且未设置
`KIWIX_ARCHIVE_DIR` 时仍会沿用，以便既有安装继续工作；推荐显式设置该环境变量。

当 `read_article` 返回 `next_offset` 时，用相同的 `archive_id` 和 `article_path` 加上该
`offset` 再次调用即可继续读长条目。对于很长的维基条目，先调用 `inspect_article`，再把返回
大纲中的 `anchor` 作为 `read_article.section` 传入。
当它返回 `next_image_offset` 时，用同一条目和该 `image_offset` 再次调用即可继续读图片列表。

多跳探索：`links`、`see_also` 和大纲中的每个条目都是同一存档内经过校验的路径，因此 Agent
可以连续跳转（例如 `search` → `read_article` → 跟随 `links[].article_path` →
`read_article`）而不必重新检索。条目内部跳转可把大纲的 `anchor` 作为 `section` 传入，或用
条目的 `uri` 作为 MCP 资源。链接只限于单个存档内部；跨存档跳转需用另一个 `archive_id`
重新检索。目前没有反向链接（「什么链接到这里」）查询。

诊断日志默认关闭。设置 `KIWIX_MCP_LOG_LEVEL=INFO` 输出到 stderr，或设置
`KIWIX_MCP_LOG_FILE=/path/to/kiwix.log` 写入小型轮转日志（最多三个 1 MiB 文件）。

stdio 进程默认空闲 600 秒后退出。可用 `KIWIX_MCP_IDLE_TIMEOUT=<秒数>` 覆盖，设为 `0`
则禁用空闲退出。任何入站 MCP 消息（包括初始化和 ping）都会重置计时器。仅在客户端支持下次
调用时重新拉起 stdio 服务器的情况下使用空闲退出。

开发检查（在项目目录下执行）：

```bash
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv lock --check
```

制作便携源码包请对已验证的提交使用 `git archive`。它包含 `uv.lock`，但不含本地虚拟环境、
图片缓存和 ZIM 数据。解压后执行 `uv sync --frozen`，按需设置 `KIWIX_ARCHIVE_DIR`，再使用
上面的 stdio 命令。首次安装需要联网下载依赖，之后条目检索和阅读即可离线进行。回答的语言
由 Agent 负责翻译成用户的语言；本服务器只返回原文，不做机器翻译。

## 安全模型

- **只读。** 没有任何工具会写入存档。`extract_image` 是唯一在读取之外触及文件系统的工具：
  它把解码后的图片作为临时文件缓存在系统临时目录下，去重并最多保留 16 个文件。
- **查询时不联网。** 正文、图片和元数据全部来自本地 ZIM 文件。唯一的网络使用是安装时下载
  依赖。
- **本地路径不外泄。** `list_archives` 会暴露所配置目录下的存档文件名，因此请把
  `KIWIX_ARCHIVE_DIR` 指向一个你愿意让 Agent 知道其内容的目录。
- **内容视为不可信。** 正文从 ZIM 数据中提取后原样交给模型。请把检索到的文本当作数据，
  它不是指令通道。
- **空闲退出。** stdio 进程默认在 600 秒无流量后退出（`KIWIX_MCP_IDLE_TIMEOUT=0` 可禁用）。

如需报告漏洞，请使用 GitHub 安全公告，而不要开公开 issue。

## 错误信息设计

工具错误被设计成可以自我纠正的，因为调用方通常是猜错参数名或 `archive_id` 的 Agent。参数
未知或缺失时，错误会返回该工具合法与必填的参数名；存档未知时，会返回可用的 `archive_id`
列表；条目不存在时，会指回 `search` 和 `list_archives`。

## 许可证

GPL-3.0-or-later。详见 [LICENSE](LICENSE)。

这不是可以自由选择的：本服务器链接了 [`libzim`](https://github.com/openzim/python-libzim)，
后者采用 GPL-3.0-or-later，因此分发构建必须是 GPL 兼容的。这也与 Kiwix 工具链的其余部分
保持一致。
