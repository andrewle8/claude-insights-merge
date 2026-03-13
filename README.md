# Claude Insights Merge

Merge Claude Code [`/insights`](https://claude.com/claude-code) across multiple machines into one report.

`/insights` is per-machine. This tool pulls the data from each machine over SSH, deduplicates and merges it, optionally runs `claude -p` for narrative analysis, and outputs an HTML page matching the `/insights` format. Read-only — never modifies source data.

## Prerequisites

- [Claude Code](https://claude.com/claude-code) on all machines, `/insights` run at least once on each
- SSH access to remote machines (Tailscale, LAN, etc.)
- Python 3.8+ on all machines (stdlib only, no pip dependencies)

## Setup

```bash
git clone https://github.com/andrewle8/claude-insights-merge.git
cd claude-insights-merge
cp config.example.json config.json
```

Edit `config.json` with your machines:

```json
{
  "machines": [
    {
      "name": "My Mac",
      "type": "local",
      "claude_home": "~/.claude"
    },
    {
      "name": "My Windows PC",
      "type": "ssh",
      "host": "user@hostname-or-ip",
      "python": "python",
      "claude_home": "C:\\Users\\you\\.claude"
    },
    {
      "name": "My Linux Box",
      "type": "ssh",
      "host": "user@hostname-or-ip",
      "python": "python3",
      "claude_home": "/home/you/.claude"
    }
  ],
  "claude_cmd": "claude",
  "default_model": "sonnet"
}
```

`config.json` is gitignored so your SSH hosts stay private. Optional config keys:

| Key | Default | Description |
|-----|---------|-------------|
| `claude_cmd` | `"claude"` | Path to Claude CLI |
| `default_model` | `"sonnet"` | Default AI model for narratives |
| `output_dir` | system temp dir | Default output directory |

If no `config.json` exists, the script falls back to local-only mode (current machine).

Verify SSH works before running:

```bash
ssh -o ConnectTimeout=5 user@host echo ok
```

## Usage

```bash
python3 claude-insights-merge.py                            # full report (Sonnet narratives)
python3 claude-insights-merge.py --detail max --model opus  # maximum detail with Opus
python3 claude-insights-merge.py --stats-only               # terminal output, no HTML
python3 claude-insights-merge.py --no-ai                    # charts only, skip narratives
python3 claude-insights-merge.py --machine Mac              # only this machine
python3 claude-insights-merge.py --output ~/report.html     # save to specific path
python3 claude-insights-merge.py --json                     # dump merged data as JSON
python3 claude-insights-merge.py --deep-search              # mine transcripts, suggest CLAUDE.md rules
python3 claude-insights-merge.py --deep-search --deep-search-output ~/suggestions.md  # save as Markdown
python3 claude-insights-merge.py --deep-search --deep-search-days 30  # last 30 days only
```

| Flag | Description |
|------|-------------|
| `--detail {normal,high,max}` | Detail level for AI analysis (default: normal) |
| `--stats-only` | Terminal output only, no HTML or AI |
| `--no-ai` | Skip narratives, render charts only |
| `--no-open` | Generate HTML but don't open browser |
| `--model {opus,sonnet,haiku}` | Model for narrative generation (default: sonnet) |
| `--machine NAME` | Filter to matching machine(s), repeatable |
| `--output PATH` | Save HTML to specific path |
| `--json` | Dump merged quantitative data as JSON |
| `--deep-search` | Mine session transcripts for friction patterns, suggest CLAUDE.md rules |
| `--deep-search-output PATH` | Save deep search suggestions as Markdown |
| `--deep-search-days N` | Analyze transcripts from last N days (default: 90) |

### Detail levels

| Level | Facets sent | Data included | AI output |
|-------|------------|---------------|-----------|
| `normal` | 75 most recent | Basic facet summaries | Standard report sections |
| `high` | 150 most recent | + project paths, languages, tools per session | + more project areas, specific examples |
| `max` | **All sessions** | + full project breakdown, first prompts, git commits, per-project goals/summaries | + project deep dives, cross-machine analysis, timeline narrative, quantified wins |

`--detail max` sends every session with enriched metadata (project paths, first prompts, duration, lines changed, languages, tools) plus a full per-project breakdown to the AI. This produces the most comprehensive report possible — specific projects named, cross-machine workflow patterns identified, and chronological evolution analyzed.

### Example output

```
$ python3 claude-insights-merge.py --stats-only

  Loaded config from config.json (3 machines)
  Collecting from 3 machines in parallel...
  MBA M3:       Stats: OK, Facets: 147, Meta: 266
  14900K Win11: Stats: OK, Facets: 200, Meta: 337
  8700K Ubuntu: Stats: none, Facets: 50, Meta: 125

  Total               340,910 msgs    848 sessions
  Lines changed:      +115,127 / -8,510
  Files modified:     1,537
  Facets analyzed:    394

  Top Tools:
    Bash          7,722
    Read          3,062
    Edit          3,024
    Grep          1,038
```

## How It Works

1. **Collect** — Reads `/insights` data from each machine in parallel (facets, session-meta, stats-cache)
2. **Merge** — Deduplicates by session ID, aggregates tools, languages, outcomes, satisfaction, friction
3. **Enrich** — Joins session-meta to facets (project paths, first prompts, durations, git activity)
4. **Analyze** — Sends merged data to `claude -p` for narrative generation
5. **Render** — Outputs a self-contained HTML page

### Data sources

| File | Path | Contains |
|------|------|----------|
| Facets | `~/.claude/usage-data/facets/*.json` | Per-session goals, outcomes, satisfaction, friction |
| Session-meta | `~/.claude/usage-data/session-meta/*.json` | Per-session project path, tools, languages, tokens, timing, first prompt |
| Stats-cache | `~/.claude/stats-cache.json` | Daily activity, model usage, session counts |

### Deep Search (`--deep-search`)

A separate analysis mode that goes beyond pre-computed `/insights` data. Mines raw JSONL session transcripts across all machines to find:

- **User corrections** — short messages starting with "no", "don't", "stop", "revert", "actually" (indicates friction)
- **Tool errors** — failed tool calls and their patterns
- **Interrupted sessions** — where users stopped Claude mid-action
- **Repeated first prompts** — instructions given in 3+ sessions (should probably be in CLAUDE.md)

Feeds the evidence to Claude, which suggests specific CLAUDE.md rules — each backed by real session data. Output can be terminal (with ANSI colors) or Markdown file (with checkboxes for review).

### Beyond standard `/insights`

- **Cross-machine merge** — unified view across all your development machines
- **Per-machine breakdown** — message counts, session counts, contribution %
- **Per-project breakdown** (high/max) — every project with lines changed, tools used, goals, and outcomes
- **Project deep dives** (max) — what was built, tech stack, impact, and status per project
- **Cross-machine patterns** (max) — which projects live where, machine roles
- **Timeline narrative** (max) — how your work evolved chronologically
- **Multi-clauding detection** — finds overlapping parallel sessions across merged data

## Output

Reports save to:
- `$TMPDIR/claude-insights-combined-YYYY-MM-DD.html` (or `--output PATH`)
- `~/.claude/insights/combined-YYYY-MM-DD.html` (persistent backup)

## Related

- [Claude Code Sync](https://github.com/andrewle8/claude-code-sync) — sync Claude Code config (CLAUDE.md, skills, memory) across machines with Syncthing

## License

MIT
