# 中国发展改革政策知识库 · MCP Server

一个远程托管的 MCP（Model Context Protocol）服务，让 Claude、Cursor、Cline 等 AI 客户端能直接检索中国国家和省级发展改革政策原文。

**服务地址：** `https://kb.55160406.org/public/mcp`
**传输方式：** Streamable HTTP
**认证：** 公开只读端点，无需 API Key

---

## 为什么需要它

大模型回答政策问题最常见的失败不是「不会推理」，而是「引用不存在」。它会给你一个格式完美、文号看起来很像、但库里查不到的文件名。

这个服务解决的是**出处**问题：返回的每一条结果都带文件名、文号、发文机关、发布日期和原文片段，可以直接核对。没有检索到就明确说没有，不编。

---

## 数据覆盖

| 类别 | 数量 |
|---|---|
| 政策文件 | 21,656 篇 |
| 全文切片 | 136,155 条 |
| 国家政策 | 21,149 篇 |
| 省级政策 | 507 篇（含四川、云南、贵州、重庆等） |
| 规划纲要 | 已收录 |
| 项目经验 | 已收录 |

收录范围为国家部委及省级发改委公开发布的政策文件、规划纲要、项目批复与经验材料。

---

## 工具

### `search_policy`

检索国家、省级、规划三类政策库，返回带出处的原文片段。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 是 | 检索问题，自然语言即可 |
| `region` | string | 否 | 限定地域，如「四川」，留空表示不限 |
| `top_k` | integer | 否 | 返回条数，默认 5 |

**什么时候用：** 用户问政策依据、申报条件、支持标准、某地有什么政策、「有没有文件规定」这类问题时，先用这个工具拿原文再回答。

**调用示例**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "search_policy",
    "arguments": {
      "query": "高标准农田建设每亩补助多少",
      "region": "四川",
      "top_k": 5
    }
  }
}
```

### `list_recent`

列出库中最新收录的政策文件，按发布日期倒序。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `province` | string | 否 | 省份简称，如「四川」，留空表示全部 |
| `limit` | integer | 否 | 返回条数，默认 20 |

**什么时候用：** 用户问「最近有什么新政策」「本月出台了哪些文件」「XX 省最近发了什么」这类没有具体检索词、只想看动态的问题。

### `corpus_stats`

查看知识库规模和覆盖范围，无参数。

**什么时候用：** 用户问「库里有多少文件」「覆盖哪些省」「有没有 XX 领域的政策」时用，先确认覆盖范围再决定要不要检索，避免答非所问。

---

## 客户端配置

### Claude Code

```bash
claude mcp add --transport http fagai-policy https://kb.55160406.org/public/mcp
```

### Cursor

`~/.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "fagai-policy": {
      "url": "https://kb.55160406.org/public/mcp"
    }
  }
}
```

### Claude Desktop

`claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "fagai-policy": {
      "url": "https://kb.55160406.org/public/mcp"
    }
  }
}
```

### VS Code

`.vscode/mcp.json`：

```json
{
  "servers": {
    "fagai-policy": {
      "type": "http",
      "url": "https://kb.55160406.org/public/mcp"
    }
  }
}
```

### 其它支持 Streamable HTTP 的客户端

填 URL `https://kb.55160406.org/public/mcp` 即可，不需要额外参数。

---

## 服务发现

服务在标准路径上暴露了 MCP Server Card，客户端和目录站可以自动发现：

- `https://kb.55160406.org/.well-known/mcp/server-card.json`（SEP-1649）
- `https://kb.55160406.org/.well-known/mcp.json`（SEP-1960）

本仓库的 [`server-card.json`](./server-card.json) 是同一份内容的副本。

---

## 授权端点

公开端点只提供检索与统计，返回的是原文片段，不含完整正文。

需要逐条引用全文（按文号或标题取整篇）的场景，使用授权端点 `https://kb.55160406.org/mcp`，需携带 Bearer Token。申请方式见官网。

---

## 使用条款

- 公开端点匿名可用，请控制调用频率（建议不超过每分钟 30 次）。
- 检索结果来自公开发布的政策文件，版权归原发布机关所有。本服务提供检索与定位，不主张对政策文本的任何权利。
- 引用时请保留返回结果中的文件名与文号，以便读者核对原文。
- 服务可能因数据更新而短暂不可用。

---

## 相关链接

- 官网：https://kb.55160406.org/
- 官方 MCP Registry：`org.55160406.kb/fagai-policy-kb`

---

## License

本仓库内容（文档与配置示例）采用 MIT 许可，见 [LICENSE](./LICENSE)。服务本身为托管服务，不随本仓库分发。
