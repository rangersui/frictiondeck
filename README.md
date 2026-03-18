# FrictionDeck

**AI draws. You stamp. Judgments are forever.**

**AI 画画。你盖章。判断不可逆。**

---

## What is this / 这是什么

FrictionDeck is a persistent Stage where AI renders anything onto a live HTML page.
You review, promote judgments, and commit them with HMAC signatures.
Commits are irreversible.

FrictionDeck 是一面持久的墙。AI 在上面画任何东西。
你审查、提取判断、签章提交。
提交不可逆。

```
"埏埴以为器，当其无，有器之用。"
— 道德经 第十一章

Shape clay into a vessel; it is the emptiness within that makes it useful.
```

---

## Philosophy / 设计哲学

FrictionDeck defines nothing. FrictionDeck 什么都不定义。

```
No card types.        没有卡片类型。
No component library. 没有组件库。
No templates.         没有模板。
No layout system.     没有布局系统。
No built-in AI.       没有内置 AI。
No RAG.               没有 RAG。
No embedding.         没有向量搜索。
No LLM.               没有大语言模型。

Just an empty page, a signature, and a counter.
只有一个空页面、一个签名、一个计数器。
```

AI decides how to present. You decide what to commit.
AI 决定怎么展示。你决定什么值得提交。

---

## How it works / 运作方式

```
AI connects via MCP          AI 通过 MCP 连接
       ↓                            ↓
Renders HTML onto Stage      在 Stage 上渲染 HTML
       ↓                            ↓
You see it at localhost:3004 你在浏览器里看到
       ↓                            ↓
AI promotes key judgments    AI 提取关键判断
       ↓                            ↓
You approve → HMAC signed    你批准 → HMAC 签名
       ↓                            ↓
Judgment is forever          判断永久保存
```

Stage is whatever AI makes it. A comparison table. A news dashboard.
A power flow diagram. A travel itinerary. A study guide. A full web app.

Stage 是 AI 创造的任何东西。对比表格、新闻仪表盘、
功率流向图、旅行行程、学习笔记、完整的 web 应用。

You don't design the UI. AI does. Every user's Stage looks different.

你不设计界面。AI 设计。每个用户的 Stage 长得不一样。

---

## Quick start / 快速开始

```bash
git clone https://github.com/rangersui/frictiondeck-v4
cd frictiondeck-v4
pip install -r requirements.txt
python server.py          # HTTP server (browser UI)
python mcp_server.py      # MCP server (Claude Desktop spawns this)
```

Open `http://localhost:3004` — you'll see an empty wall.

打开 `http://localhost:3004` — 你会看到一面空墙。

Connect your AI via MCP. The wall comes alive.

连接你的 AI（通过 MCP）。墙会活起来。

### Claude Desktop configuration / Claude Desktop 配置

```json
{
  "mcpServers": {
    "frictiondeck": {
      "command": "python",
      "args": ["path/to/frictiondeck-v4/mcp_server.py"]
    }
  }
}
```

---

## What's on the wall / 墙上有什么

Whatever your AI puts there. 你的 AI 放什么就有什么。

FrictionDeck has been used for: / FrictionDeck 被用来做过：

* Engineering motor selection analysis with interactive comparison tables
  工程电机选型分析（交互式对比表格）
* Breaking news dashboards with live source links
  突发新闻仪表盘（带实时来源链接）
* Newspaper-style war reporting layouts
  报纸风格的战事报道排版

We didn't build any of these. AI did. On an empty wall.
这些都不是我们做的。是 AI 做的。在一面空墙上。

---

## Architecture / 架构

```
server.py              → FastAPI (HTTP + static files)
mcp_server.py          → MCP endpoint (stdio)
pipeline/stage.py      → stage_html + judgment_objects + version++
pipeline/audit.py      → HMAC-SHA256 hash chain
pipeline/commits.py    → Sealed judgments
pipeline/mcp_adapter.py → AI's hands (append, mutate, query, promote, commit)
pipeline/gui_adapter.py → Human's hands (approve, reject)
static/index.html      → Three tabs, a few buttons, an empty div
```

```
Total: ~2000 lines of Python + 1 HTML file
Dependencies: fastapi, uvicorn, mcp
Models: none
Frameworks: none
```

---

## The three tabs / 三个标签页

```
Stage    AI's canvas. An empty <div>. AI fills it.
         AI 的画布。一个空 <div>。AI 来填。

Log      Committed judgments. Raw JSON. Immutable history.
         已提交的判断。原始 JSON。不可篡改的历史。

Commit   Pending proposals. Approve or reject.
         待审提案。批准或拒绝。
```

---

## MCP tools / MCP 工具

AI operates the Stage through these tools:
AI 通过这些工具操作 Stage：

```
DOM operations (AI's hands):
  append_stage(html)          — Add content
  mutate_stage(html)          — Replace all content
  query_stage()               — Read current page
  execute_js(script)          — Run JavaScript

Judgment operations (structured output):
  promote_to_judgment(selector, claim_text, params)
  flag_negative_space(description, severity)
  propose_commit(judgments, reasoning, engineer)

Queries (AI's memory):
  get_world_state()           — Call this first. Always.
  get_stage_state()
  search_commits(...)
  get_audit_trail(...)
  wait_for_stage_update(...)
```

AI cannot approve commits. Only humans can.
AI 不能批准提交。只有人能。

---

## What makes this different / 为什么不同

```
Claude Artifacts    → Ephemeral. Close the chat, gone.
                      临时的。关掉对话就没了。

Notion / Canvas     → You design the structure. AI fills blanks.
                      你设计结构。AI 填空。

FrictionDeck        → AI designs everything. You just stamp.
                      AI 设计一切。你只盖章。
                      Persistent. Signed. Irreversible.
                      持久的。签名的。不可逆的。
```

---

## Security model / 安全模型

```
Stage runs in your browser.       Stage 跑在你的浏览器里。
Data stays on your machine.       数据留在你的电脑上。
No cloud. No API keys. No LLM.   没有云。没有 API 密钥。没有 LLM。
AI writes HTML. Browsers render.  AI 写 HTML。浏览器渲染。

Attack surface: one HTML page.    攻击面：一个 HTML 页面。
Worst case: refresh the page.     最坏情况：刷新页面。
```

---

## Skill / 技能包

Install the FrictionDeck skill to teach your AI the optimal workflow:

安装 FrictionDeck 技能包，教你的 AI 最佳工作流：

```
Skill effectiveness (A/B tested):

| Scenario            | With Skill | Without | Δ     |
|---------------------|-----------|---------|-------|
| Datasheet Compare   | 100%      | 50%     | +50%  |
| Session Recovery    | 100%      | 80%     | +20%  |
| Commit Flow         | 83%       | 83%     | —     |
| Visual Iteration    | 100%      | 100%    | —     |
| **Average**         | **96%**   | **78%** | **+18%** |
```

---

## Roadmap / 路线图

```
Done:
  ✅ Empty wall + MCP + HMAC + commit
  ✅ AI renders anything onto Stage
  ✅ Skill with A/B tested effectiveness

Next:
  □ Cloudflare Tunnel relay (public access, zero server cost)
  □ E2E encryption (relay sees nothing)
  □ Cowork plugin (one-click install)
  □ FrictionHub (push/pull committed knowledge across projects)
  □ Enterprise mode (multi-user + SSO + iframe sandbox)
```

---

## The emptiness / 空

```
We removed RAG.               我们砍了 RAG。
We removed the LLM.           砍了 LLM。
We removed embeddings.        砍了向量搜索。
We removed the component library. 砍了组件库。
We removed card templates.    砍了卡片模板。
We removed the HTML sanitizer. 砍了 HTML 消毒器。
We removed NiceGUI.           砍了 NiceGUI。
We removed the Python sandbox. 砍了 Python 沙箱。
We removed NLI verification.  砍了 NLI 验证。
We removed the Friction Gate. 砍了摩擦门。

What's left:
剩下的：

  An HTTP server.             一个 HTTP 服务器。
  An HTML file.               一个 HTML 文件。
  Three SQLite files.         三个 SQLite 文件。
  A hash function.            一个哈希函数。
  An incrementing integer.    一个自增整数。

本来无一物。
```

---

*Copyright © 2026 Ranger Chen. AGPL v3.0.*
