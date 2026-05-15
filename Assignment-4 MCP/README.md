# Research & Study MCP Server

An MCP server for Claude Desktop with four tools and two slash commands. Designed for daily use by a data scientist who wants to stay current on AI research, products, and concepts.

## What's inside

### Tools
| Tool | Use for |
|---|---|
| `search_internet` | Tavily web search — blogs, essays, product launches, news |
| `fetch_arxiv` | arXiv API — academic papers with full metadata |
| `manage_local_file` | Local file CRUD — persist reports, audit logs, plans |
| `generate_custom_ui` | Dynamic Prefab UI — render dashboards on the fly |

### Slash commands
| Command | Purpose |
|---|---|
| `/project_insight` | Project Insight — 3-phase intelligence and risk assessment on a topic |
| `/weekly_study_plan` | Personalized weekly study plan for a data scientist |

## Setup

### 1. Install dependencies

```bash
cd "Assignment-4 MCP"
uv sync
```

Or with pip:

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and add your TAVILY_API_KEY
```

You can get a free Tavily key at <https://tavily.com>. The arXiv API needs no key.

### 3. Register with a client

**Claude Desktop:**

```bash
uv run fastmcp install claude-desktop mcp_server.py:mcp
```

Restart Claude Desktop. You should see the slash commands `/project_insight` and `/weekly_study_plan` in the autocomplete.

**Claude Code (CLI on PATH):**

```bash
uv run fastmcp install claude-code mcp_server.py:mcp
```

**Claude Code (VSCode extension, no `claude` CLI on PATH):**

`fastmcp install` shells out to the `claude` CLI, which the bundled extension does not expose. Register the server with a project-scoped `.mcp.json` at the repo root instead:

```json
{
  "mcpServers": {
    "research-study": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/Assignment-4 MCP",
        "fastmcp",
        "run",
        "mcp_server.py:mcp"
      ]
    }
  }
}
```

`--directory` makes `uv` resolve this project's pinned deps (`pyproject.toml` + `uv.lock`) and locate `.env` at runtime — don't use the bare `fastmcp install mcp-json` output, which pulls an ephemeral fastmcp and misses `prefab-ui`/`tavily-python`/`arxiv`. Reload the window, then approve the server when Claude Code prompts (check status with `/mcp`).

## Usage

> In Claude Desktop the prompts appear as `/project_insight` and `/weekly_study_plan`. In Claude Code, MCP prompts are namespaced — use `/mcp__research-study__project_insight` and `/mcp__research-study__weekly_study_plan`. The tools work identically in both clients.

### Weekly study plan

Run on Sunday evening or Monday morning:

```
/weekly_study_plan
```

No arguments. The agent surveys arXiv + web, picks 5-8 items, persists a markdown file, and renders a dashboard.

The plan covers three categories tuned for a data scientist:
- **Deep reads** — 2-3 papers or longform essays per week (30-60 min each)
- **Quick scans** — 2-3 product launches and blog posts (5-10 min each)
- **Concept refreshers** — 1-2 foundational topics paired with the deep reads

Sources prioritized: arXiv direct, Noema, Quanta, Aeon, official company blogs, canonical technical blogs (Lilian Weng, Sebastian Raschka, Distill).

Sources avoided: TechCrunch coverage, Medium thinkpieces, YouTube as primary source.

### Project Insight

```
/project_insight Tesla
/project_insight "NVIDIA Corporation"
```

3-phase workflow: intelligence gathering, audit trail to disk, dynamic dashboard render.

### Direct tool use

You can also invoke the tools without a slash command:

```
Use fetch_arxiv to find papers about retrieval augmented generation from the last 14 days
```

## Development

Local preview (no install required):

```bash
uv run fastmcp dev apps mcp_server.py:mcp
```

This opens a browser preview where you can invoke tools manually and see Prefab dashboards render. Useful for testing `generate_custom_ui` output.

## File layout

```
Assignment-4 MCP/
├── mcp_server.py        # The server — all tools and slash prompts
├── pyproject.toml       # Dependencies (pinned)
├── .env.example         # Environment variables template
├── .env                 # Your local keys (gitignored)
└── README.md            # This file
```

## Pinned versions

- `prefab-ui==0.19.1` (exact pin — prefab-ui has breaking changes per release)
- `fastmcp>=3.2`
- `arxiv>=2.1`

When upgrading `prefab-ui`, test all UI generation flows before shipping.

## Quality conventions

- Errors are returned as strings (or as `{success: False, ...}`), not raised.
- Tool docstrings explicitly note what each tool is for and what it's NOT for.
- `fetch_arxiv` over `search_internet` for any arXiv lookup.
- Slash prompts have explicit budgets and quality rules to prevent filler output.

## License

MIT.
