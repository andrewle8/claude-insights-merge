# Claude Insights Merge

Merge Claude Code [`/insights`](https://claude.com/claude-code) across multiple machines into one report.

`/insights` is per-machine. This tool pulls the data from each machine over SSH, deduplicates and merges it, optionally runs `claude -p` for narrative analysis, and outputs an HTML page matching the `/insights` format. Read-only — never modifies source data.

## Prerequisites

- [Claude Code](https://claude.com/claude-code) on all machines, `/insights` run at least once on each
- SSH access to remote machines (Tailscale, LAN, etc.)
- Python 3.8+ on all machines (stdlib only, no pip dependencies)

## Setup

```bash
cp claude-insights-merge.py ~/.local/bin/claude-insights-merge
chmod +x ~/.local/bin/claude-insights-merge
```

Edit the `MACHINES` list at the top of the script:

```python
MACHINES = [
    {
        "name": "My Mac",
        "type": "local",
        "claude_home": str(Path.home() / ".claude"),
    },
    {
        "name": "My Windows PC",
        "type": "ssh",
        "host": "user@hostname-or-ip",
        "python": "python",          # "python3" on Linux
        "claude_home": r"C:\Users\you\.claude",
    },
    {
        "name": "My Linux Box",
        "type": "ssh",
        "host": "user@hostname-or-ip",
        "python": "python3",
        "claude_home": "/home/you/.claude",
    },
]
```

Verify SSH works before running:

```bash
ssh -o ConnectTimeout=5 user@host echo ok
```

## Usage

```bash
claude-insights-merge                          # full report (Sonnet narratives)
claude-insights-merge --model opus             # use Opus for narratives
claude-insights-merge --stats-only             # terminal output, no HTML
claude-insights-merge --no-ai                  # charts only, skip narratives
claude-insights-merge --machine Mac            # only this machine
claude-insights-merge --output ~/report.html   # save to specific path
claude-insights-merge --json                   # dump merged data as JSON
```

| Flag | Description |
|------|-------------|
| `--stats-only` | Terminal output only, no HTML |
| `--no-ai` | Skip narratives, render charts only |
| `--no-open` | Generate HTML but don't open browser |
| `--model {opus,sonnet,haiku}` | Model for narrative generation (default: sonnet) |
| `--machine NAME` | Filter to matching machine(s), repeatable |
| `--output PATH` | Save HTML to specific path |
| `--json` | Dump merged quantitative data as JSON |

### Example output

```
$ claude-insights-merge --stats-only

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

1. **Collect** — Reads `/insights` data from each machine (facets, session-meta, stats-cache)
2. **Merge** — Deduplicates by session ID, aggregates tools, languages, outcomes, satisfaction, friction
3. **Analyze** — Sends merged data to `claude -p` for narrative generation
4. **Render** — Outputs a self-contained HTML page

### Data sources

| File | Path | Contains |
|------|------|----------|
| Facets | `~/.claude/usage-data/facets/*.json` | Per-session goals, outcomes, satisfaction, friction |
| Session-meta | `~/.claude/usage-data/session-meta/*.json` | Per-session tools, languages, tokens, timing |
| Stats-cache | `~/.claude/stats-cache.json` | Daily activity, model usage, session counts |

### Beyond standard `/insights`

- **Per-machine breakdown** — message counts, session counts, contribution %
- **Multi-clauding detection** — finds overlapping sessions across merged data
- **Merged stats** — tool usage, languages, friction, and outcomes across all machines

## Output

Reports save to:
- `$TMPDIR/claude-insights-combined-YYYY-MM-DD.html`
- `~/.claude/insights/combined-YYYY-MM-DD.html` (persistent copy)

## Related

- [Claude Code Sync](https://github.com/andrewle8/claude-code-sync) — sync Claude Code config (CLAUDE.md, skills, memory) across machines with Syncthing

## License

MIT
