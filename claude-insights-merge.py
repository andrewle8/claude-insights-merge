#!/usr/bin/env python3
"""Cross-machine Claude Code insights merger with AI narrative generation.

Merges pre-computed /insights data (facets + session-meta) from multiple
machines, generates AI narrative analysis via claude CLI, and produces an
HTML report matching the built-in /insights format.

Prerequisites: Run /insights on each machine first to generate facets.

Usage:
    claude-insights-merge                        # Full HTML with AI narratives
    claude-insights-merge --stats-only           # Terminal output, no AI
    claude-insights-merge --no-open              # Generate HTML but don't open browser
    claude-insights-merge --json                 # Dump merged data as JSON
    claude-insights-merge --model opus           # Use a specific model (opus/sonnet/haiku)
    claude-insights-merge --no-ai                # Skip AI narratives, render charts only
    claude-insights-merge --machine MBA          # Only collect from matching machine(s)
    claude-insights-merge --machine MBA --machine 8700K  # Multiple machines
    claude-insights-merge --output ~/report.html # Save HTML to specific path
"""

import argparse
import json
import subprocess
import sys
import os
import html as html_mod
import tempfile
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# ─── Configuration ───────────────────────────────────────────────────────────

# Edit this list to match your machines.
# "type": "local" reads from the local filesystem; "type": "ssh" connects via SSH.
MACHINES = [
    {
        "name": "My Mac",
        "type": "local",
        "claude_home": str(Path.home() / ".claude"),
    },
    # {
    #     "name": "My Windows PC",
    #     "type": "ssh",
    #     "host": "user@hostname-or-ip",
    #     "python": "python",
    #     "claude_home": r"C:\Users\you\.claude",
    # },
    # {
    #     "name": "My Linux Box",
    #     "type": "ssh",
    #     "host": "user@hostname-or-ip",
    #     "python": "python3",
    #     "claude_home": "/home/you/.claude",
    # },
]

CLAUDE_CMD = "claude"
NARRATIVE_MODEL = "sonnet"
OUTPUT_DIR = Path(tempfile.gettempdir())

# ─── SSH Helpers ─────────────────────────────────────────────────────────────

def ssh_run(host, cmd, timeout=90):
    """Run a command on a remote host via SSH."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5",
             "-o", "ControlMaster=auto",
             "-o", "ControlPath=/tmp/claude-insights-ssh-%h",
             "-o", "ControlPersist=60",
             host, cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            if result.stderr.strip():
                print(f"    SSH stderr: {result.stderr.strip()[:200]}", file=sys.stderr)
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, Exception) as e:
        print(f"    SSH error: {e}", file=sys.stderr)
        return None


def ssh_run_python(host, python_cmd, script, timeout=90):
    """Run a Python script on a remote host by piping it via stdin."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5",
             "-o", "ControlMaster=auto",
             "-o", "ControlPath=/tmp/claude-insights-ssh-%h",
             "-o", "ControlPersist=60",
             host, f"{python_cmd} -"],
            input=script, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            if result.stderr.strip():
                print(f"    SSH stderr: {result.stderr.strip()[:200]}", file=sys.stderr)
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, Exception) as e:
        print(f"    SSH error: {e}", file=sys.stderr)
        return None


def ssh_read_json_files(host, python_cmd, directory, glob_pattern="*.json"):
    """Read all JSON files in a remote directory, return as list of dicts."""
    escaped_dir = directory.replace("\\", "\\\\")
    script = f"""
import json, glob, os
files = glob.glob(os.path.join(r'{escaped_dir}', '{glob_pattern}'))
data = []
for f in sorted(files):
    if os.path.isfile(f):
        try:
            data.append(json.load(open(f)))
        except Exception:
            pass
print(json.dumps(data))
"""
    raw = ssh_run_python(host, python_cmd, script, timeout=120)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


# ─── Data Collection ─────────────────────────────────────────────────────────

def collect_machine_data(machine):
    """Collect all data from a single machine."""
    name = machine["name"]
    data = {
        "name": name,
        "stats": None,
        "facets": [],
        "session_meta": [],
    }
    progress = []

    claude_home = machine["claude_home"]

    if machine["type"] == "local":
        home = Path(claude_home)

        # [1/3] Stats cache
        stats_file = home / "stats-cache.json"
        if stats_file.exists():
            try:
                with open(stats_file) as f:
                    data["stats"] = json.load(f)
            except Exception:
                pass
        progress.append(f"    [1/3] stats-cache... {'OK' if data['stats'] else 'none'}")

        # [2/3] Facets
        facets_dir = home / "usage-data" / "facets"
        if facets_dir.exists():
            for f in sorted(facets_dir.glob("*.json")):
                try:
                    with open(f) as fh:
                        data["facets"].append(json.load(fh))
                except Exception:
                    pass
        count = len(data["facets"])
        progress.append(f"    [2/3] facets ({count} files)... {'OK' if count else 'none'}")

        # [3/3] Session meta
        meta_dir = home / "usage-data" / "session-meta"
        if meta_dir.exists():
            for f in sorted(meta_dir.glob("*.json")):
                try:
                    with open(f) as fh:
                        data["session_meta"].append(json.load(fh))
                except Exception:
                    pass
        count = len(data["session_meta"])
        progress.append(f"    [3/3] session-meta ({count} files)... {'OK' if count else 'none'}")

    elif machine["type"] == "ssh":
        host = machine["host"]
        py = machine.get("python", "python3")

        # Determine path separator
        is_windows = "\\" in claude_home

        # [1/3] Stats cache
        if is_windows:
            stats_cmd = f"type {claude_home}\\stats-cache.json"
        else:
            stats_cmd = f"cat {claude_home}/stats-cache.json"
        raw = ssh_run(host, stats_cmd)
        if raw:
            try:
                data["stats"] = json.loads(raw)
            except json.JSONDecodeError:
                pass
        progress.append(f"    [1/3] stats-cache... {'OK' if data['stats'] else 'none'}")

        # [2/3] Facets
        if is_windows:
            facets_path = f"{claude_home}\\usage-data\\facets"
        else:
            facets_path = f"{claude_home}/usage-data/facets"
        data["facets"] = ssh_read_json_files(host, py, facets_path)
        count = len(data["facets"])
        progress.append(f"    [2/3] facets ({count} files)... {'OK' if count else 'none'}")

        # [3/3] Session meta
        if is_windows:
            meta_path = f"{claude_home}\\usage-data\\session-meta"
        else:
            meta_path = f"{claude_home}/usage-data/session-meta"
        data["session_meta"] = ssh_read_json_files(host, py, meta_path)
        count = len(data["session_meta"])
        progress.append(f"    [3/3] session-meta ({count} files)... {'OK' if count else 'none'}")

    data["_progress"] = progress
    return data


def collect_all():
    """Collect data from all machines in parallel."""
    machine_data = [None] * len(MACHINES)
    machine_names = [m["name"] for m in MACHINES]
    print(f"  Collecting from {len(MACHINES)} machines in parallel...")

    with ThreadPoolExecutor(max_workers=len(MACHINES)) as executor:
        future_to_idx = {
            executor.submit(collect_machine_data, m): i
            for i, m in enumerate(MACHINES)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            name = machine_names[idx]
            try:
                data = future.result()
                machine_data[idx] = data
                # Print progress collected during this machine's run
                print(f"  {name}:")
                for line in data.pop("_progress", []):
                    print(line)
                print(f"    Stats: {'OK' if data['stats'] else 'none'}, "
                      f"Facets: {len(data['facets'])}, "
                      f"Meta: {len(data['session_meta'])}")
            except Exception as e:
                print(f"  {name}: FAILED ({e})", file=sys.stderr)
                machine_data[idx] = {
                    "name": name, "stats": None,
                    "facets": [], "session_meta": [],
                }

    return machine_data


# ─── Data Aggregation ────────────────────────────────────────────────────────

def aggregate_all(machine_data):
    """Aggregate facets, session-meta, and stats across all machines."""

    # ── Merge stats-cache (for top-line numbers) ──
    all_stats = [m["stats"] for m in machine_data if m["stats"]]
    total_sessions = sum(s.get("totalSessions", 0) for s in all_stats)
    total_messages = sum(s.get("totalMessages", 0) for s in all_stats)

    # Merge daily activity
    daily_by_date = defaultdict(lambda: {"messageCount": 0, "sessionCount": 0, "toolCallCount": 0})
    for s in all_stats:
        for entry in s.get("dailyActivity", []):
            d = entry["date"]
            daily_by_date[d]["messageCount"] += entry["messageCount"]
            daily_by_date[d]["sessionCount"] += entry["sessionCount"]
            daily_by_date[d]["toolCallCount"] += entry["toolCallCount"]
    daily_activity = [{"date": d, **counts} for d, counts in sorted(daily_by_date.items())]

    # Merge model usage
    merged_models = {}
    for s in all_stats:
        for model, usage in s.get("modelUsage", {}).items():
            if model not in merged_models:
                merged_models[model] = {}
            for k, v in usage.items():
                merged_models[model][k] = merged_models[model].get(k, 0) + v

    # Merge hour counts
    merged_hours = {}
    for h in range(24):
        key = str(h)
        merged_hours[key] = sum(s.get("hourCounts", {}).get(key, 0) for s in all_stats)

    first_dates = [s.get("firstSessionDate") for s in all_stats if s.get("firstSessionDate")]
    first_session = min(first_dates) if first_dates else None

    longest = None
    for s in all_stats:
        ls = s.get("longestSession")
        if ls and (longest is None or ls.get("duration", 0) > longest.get("duration", 0)):
            longest = ls

    # Per-machine breakdown
    per_machine = []
    for m in machine_data:
        msgs = sum(d["messageCount"] for d in (m["stats"].get("dailyActivity", []) if m["stats"] else []))
        sessions = m["stats"].get("totalSessions", 0) if m["stats"] else 0
        # If no stats, estimate from session-meta
        if not m["stats"] and m["session_meta"]:
            msgs = sum(sm.get("user_message_count", 0) + sm.get("assistant_message_count", 0)
                       for sm in m["session_meta"])
            sessions = len(m["session_meta"])
        per_machine.append({"name": m["name"], "messages": msgs, "sessions": sessions})

    # ── Merge facets (qualitative data) ──
    all_facets = []
    seen_sessions = set()
    for m in machine_data:
        for facet in m["facets"]:
            sid = facet.get("session_id", "")
            if sid and sid not in seen_sessions:
                seen_sessions.add(sid)
                facet["_machine"] = m["name"]
                all_facets.append(facet)

    # Aggregate facet fields
    goal_categories = defaultdict(int)
    outcomes = defaultdict(int)
    satisfaction = defaultdict(int)
    friction_counts = defaultdict(int)
    session_types = defaultdict(int)
    helpfulness = defaultdict(int)
    primary_success = defaultdict(int)

    for f in all_facets:
        for cat, count in f.get("goal_categories", {}).items():
            goal_categories[cat] += count
        outcomes[f.get("outcome", "unknown")] += 1
        for sat, count in f.get("user_satisfaction_counts", {}).items():
            satisfaction[sat] += count
        for ftype, count in f.get("friction_counts", {}).items():
            friction_counts[ftype] += count
        session_types[f.get("session_type", "unknown")] += 1
        helpfulness[f.get("claude_helpfulness", "unknown")] += 1
        if f.get("primary_success"):
            primary_success[f["primary_success"]] += 1

    # ── Merge session-meta (quantitative data) ──
    all_meta = []
    seen_meta = set()
    for m in machine_data:
        for meta in m["session_meta"]:
            sid = meta.get("session_id", "")
            if sid and sid not in seen_meta:
                seen_meta.add(sid)
                meta["_machine"] = m["name"]
                all_meta.append(meta)

    # Aggregate meta fields
    tool_counts = defaultdict(int)
    languages = defaultdict(int)
    tool_errors = defaultdict(int)
    total_tool_errors = 0
    total_lines_added = 0
    total_lines_removed = 0
    total_files_modified = 0
    response_time_buckets = {"2-10s": 0, "10-30s": 0, "30s-1m": 0, "1-2m": 0, "2-5m": 0, "5-15m": 0, ">15m": 0}
    all_response_times = []

    for meta in all_meta:
        for tool, count in meta.get("tool_counts", {}).items():
            tool_counts[tool] += count
        for lang, count in meta.get("languages", {}).items():
            languages[lang] += count
        for etype, count in meta.get("tool_error_categories", {}).items():
            tool_errors[etype] += count
        total_tool_errors += meta.get("tool_errors", 0)
        total_lines_added += meta.get("lines_added", 0)
        total_lines_removed += meta.get("lines_removed", 0)
        total_files_modified += meta.get("files_modified", 0)
        # Collect response times for distribution
        for rt in meta.get("user_response_times", []):
            if isinstance(rt, (int, float)) and 2 <= rt <= 7200:
                all_response_times.append(rt)
                if rt <= 10: response_time_buckets["2-10s"] += 1
                elif rt <= 30: response_time_buckets["10-30s"] += 1
                elif rt <= 60: response_time_buckets["30s-1m"] += 1
                elif rt <= 120: response_time_buckets["1-2m"] += 1
                elif rt <= 300: response_time_buckets["2-5m"] += 1
                elif rt <= 900: response_time_buckets["5-15m"] += 1
                else: response_time_buckets[">15m"] += 1

    # Multi-clauding detection (overlapping sessions)
    overlap_count = 0
    sessions_involved = set()
    sorted_meta = sorted(all_meta, key=lambda m: m.get("start_time", ""))
    for i in range(len(sorted_meta)):
        for j in range(i + 1, min(i + 10, len(sorted_meta))):
            si = sorted_meta[i]
            sj = sorted_meta[j]
            si_end = si.get("start_time", "")
            sj_start = sj.get("start_time", "")
            si_dur = si.get("duration_minutes", 0)
            if si_end and sj_start and si_dur > 1:
                try:
                    from datetime import timedelta
                    t_start_i = datetime.fromisoformat(si.get("start_time", "").replace("Z", "+00:00"))
                    t_start_j = datetime.fromisoformat(sj.get("start_time", "").replace("Z", "+00:00"))
                    t_end_i = t_start_i + timedelta(minutes=si_dur)
                    if t_start_j < t_end_i:
                        overlap_count += 1
                        sessions_involved.add(si.get("session_id"))
                        sessions_involved.add(sj.get("session_id"))
                except (ValueError, TypeError):
                    pass

    # Compute median response time
    median_rt = 0
    avg_rt = 0
    if all_response_times:
        srt = sorted(all_response_times)
        median_rt = srt[len(srt) // 2]
        avg_rt = sum(srt) / len(srt)

    # Sort facets by session start time for proper recency sampling
    meta_times = {m["session_id"]: m.get("start_time", "") for m in all_meta}
    all_facets.sort(key=lambda f: meta_times.get(f.get("session_id", ""), ""))

    return {
        # Top-line stats
        "totalSessions": total_sessions,
        "totalMessages": total_messages,
        "dailyActivity": daily_activity,
        "modelUsage": merged_models,
        "hourCounts": merged_hours,
        "firstSessionDate": first_session,
        "longestSession": longest,
        "perMachine": per_machine,
        # From facets
        "facets": all_facets,
        "goal_categories": dict(sorted(goal_categories.items(), key=lambda x: x[1], reverse=True)),
        "outcomes": dict(outcomes),
        "satisfaction": dict(satisfaction),
        "friction_counts": dict(sorted(friction_counts.items(), key=lambda x: x[1], reverse=True)),
        "session_types": dict(sorted(session_types.items(), key=lambda x: x[1], reverse=True)),
        "primary_success": dict(sorted(primary_success.items(), key=lambda x: x[1], reverse=True)),
        # From session-meta
        "session_meta": all_meta,
        "tool_counts": dict(sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)),
        "languages": dict(sorted(languages.items(), key=lambda x: x[1], reverse=True)),
        "tool_errors": dict(sorted(tool_errors.items(), key=lambda x: x[1], reverse=True)),
        "total_tool_errors": total_tool_errors,
        "lines_added": total_lines_added,
        "lines_removed": total_lines_removed,
        "files_modified": total_files_modified,
        "response_times": response_time_buckets,
        "median_response_time": round(median_rt, 1),
        "avg_response_time": round(avg_rt, 1),
        "multi_clauding": {
            "overlap_events": overlap_count,
            "sessions_involved": len(sessions_involved),
        },
        # Counts
        "total_facets": len(all_facets),
        "total_meta": len(all_meta),
    }


# ─── AI Narrative Generation ─────────────────────────────────────────────────

def build_ai_prompt(agg, machine_data):
    """Build the prompt for narrative generation."""
    machines_str = ", ".join(m["name"] for m in machine_data)

    # Summarize facets for the prompt — cap at 75 most recent to keep prompt manageable
    # (the built-in /insights uses ~50 sessions for narrative generation)
    recent_facets = agg["facets"][-75:]
    facet_summaries = []
    for f in recent_facets:
        facet_summaries.append({
            "machine": f.get("_machine", "unknown"),
            "goal": f.get("underlying_goal", ""),
            "outcome": f.get("outcome", ""),
            "session_type": f.get("session_type", ""),
            "satisfaction": f.get("user_satisfaction_counts", {}),
            "friction": f.get("friction_counts", {}),
            "friction_detail": f.get("friction_detail", ""),
            "success": f.get("primary_success", ""),
            "summary": f.get("brief_summary", ""),
        })

    stats_summary = {
        "machines": machines_str,
        "total_sessions": agg["totalSessions"],
        "total_messages": agg["totalMessages"],
        "active_days": len(agg["dailyActivity"]),
        "per_machine": agg["perMachine"],
        "top_tools": dict(list(agg["tool_counts"].items())[:10]),
        "languages": dict(list(agg["languages"].items())[:10]),
        "goal_categories": dict(list(agg["goal_categories"].items())[:15]),
        "outcomes": agg["outcomes"],
        "satisfaction": agg["satisfaction"],
        "friction_counts": agg["friction_counts"],
        "session_types": agg["session_types"],
        "primary_success": agg["primary_success"],
        "lines_added": agg["lines_added"],
        "lines_removed": agg["lines_removed"],
        "files_modified": agg["files_modified"],
        "total_facets_analyzed": agg["total_facets"],
    }

    prompt = f"""You are analyzing a Claude Code power user's activity across {len(machine_data)} machines to generate a comprehensive insights report. The data comes from pre-computed /insights facets merged across all machines.

## Aggregated Stats
{json.dumps(stats_summary, indent=2)}

## Per-Session Facets ({len(facet_summaries)} sessions analyzed)
{json.dumps(facet_summaries, indent=2, default=str)}

## Instructions

Generate a comprehensive insights analysis as a JSON object. Be specific, reference actual projects and patterns from the facet summaries. Be honest about friction. The tone should be direct and personalized.

Respond with ONLY a JSON object (no markdown fencing) with these exact keys:

{{
  "at_a_glance": {{
    "working": "<2-3 sentences>",
    "hindering": "<2-3 sentences>",
    "quick_wins": "<2-3 sentences>",
    "ambitious": "<2-3 sentences>"
  }},
  "project_areas": [
    {{"name": "<name>", "session_count": "<~N sessions>", "description": "<2-3 sentences>"}}
  ],
  "usage_narrative": {{
    "paragraph1": "<analysis paragraph>",
    "paragraph2": "<deeper analysis>",
    "key_insight": "<one sentence key pattern>"
  }},
  "big_wins": [
    {{"title": "<achievement>", "description": "<2-3 sentences>"}}
  ],
  "friction_intro": "<1 sentence summary>",
  "friction_categories": [
    {{"title": "<type>", "description": "<advice>", "examples": ["<specific example>"]}}
  ],
  "claude_md_suggestions": [
    {{"code": "<rule>", "why": "<reason>"}}
  ],
  "features": [
    {{"title": "<name>", "oneliner": "<one line>", "why": "<personalized reason>", "code": "<example>"}}
  ],
  "patterns": [
    {{"title": "<pattern>", "summary": "<one line>", "detail": "<2-3 sentences>", "prompt": "<paste-into-claude prompt>"}}
  ],
  "horizon": [
    {{"title": "<possibility>", "possible": "<2-3 sentences>", "tip": "<getting started>", "prompt": "<paste prompt>"}}
  ],
  "fun_ending": {{
    "headline": "<funny one-liner in quotes>",
    "detail": "<2-3 sentence elaboration>"
  }}
}}

Important:
- Reference specific projects and patterns from the facet data
- This user works across {len(machine_data)} machines: {machines_str}
- Be personalized, not generic"""

    return prompt


def generate_narratives(agg, machine_data):
    """Call claude -p to generate AI narrative sections."""
    print("  Generating AI narrative analysis...")
    prompt = build_ai_prompt(agg, machine_data)

    try:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        result = subprocess.run(
            [
                CLAUDE_CMD, "-p",
                "--model", NARRATIVE_MODEL,
                "--tools", "",
                "--no-session-persistence",
                "--output-format", "json",
            ],
            input=prompt,
            capture_output=True, text=True, timeout=600,
            env=env,
        )

        if result.returncode != 0:
            print(f"  Warning: claude -p failed: {result.stderr[:200]}", file=sys.stderr)
            return None

        output = result.stdout.strip()
        if not output:
            return None

        # Parse output — --output-format json wraps in a result object
        try:
            wrapper = json.loads(output)
            if isinstance(wrapper, dict) and "result" in wrapper:
                text = wrapper["result"]
            elif isinstance(wrapper, dict) and "content" in wrapper:
                content = wrapper["content"]
                if isinstance(content, list):
                    text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
                else:
                    text = str(content)
            elif isinstance(wrapper, str):
                text = wrapper
            else:
                text = output
        except json.JSONDecodeError:
            text = output

        # Clean markdown fencing
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        narratives = json.loads(text)
        print("  AI narrative generated successfully")
        return narratives

    except subprocess.TimeoutExpired:
        print("  Warning: claude -p timed out", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"  Warning: Could not parse AI response: {e}", file=sys.stderr)
        debug_file = OUTPUT_DIR / "claude-insights-debug.txt"
        with open(debug_file, "w") as f:
            f.write(text if "text" in dir() else "no output")
        print(f"  Raw output saved to {debug_file}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  Warning: Narrative generation failed: {e}", file=sys.stderr)
        return None


# ─── HTML Rendering ──────────────────────────────────────────────────────────

def esc(text):
    return html_mod.escape(str(text)) if text else ""


def bar_chart_html(title, items, color="#2563eb", max_items=6):
    if not items:
        return f'<div class="chart-card"><div class="chart-title">{esc(title)}</div><div class="empty">No data</div></div>'
    items = items[:max_items]
    max_val = max(item["count"] for item in items) if items else 1
    if max_val == 0:
        max_val = 1
    rows = []
    for item in items:
        pct = (item["count"] / max_val) * 100
        rows.append(f'<div class="bar-row"><div class="bar-label">{esc(item["name"])}</div>'
                     f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%;background:{color}"></div></div>'
                     f'<div class="bar-value">{item["count"]}</div></div>')
    return f'<div class="chart-card"><div class="chart-title">{esc(title)}</div>{"".join(rows)}</div>'


def dict_to_items(d, label_map=None):
    """Convert {name: count} dict to [{name, count}] list."""
    items = []
    for k, v in d.items():
        name = label_map.get(k, k) if label_map else k
        # Clean up snake_case names
        name = name.replace("_", " ").title()
        items.append({"name": name, "count": v})
    return sorted(items, key=lambda x: x["count"], reverse=True)


def render_html(agg, machine_data, narratives):
    """Render the full HTML insights page."""

    n = narratives or {}
    glance = n.get("at_a_glance", {})
    areas = n.get("project_areas", [])
    usage = n.get("usage_narrative", {})
    wins = n.get("big_wins", [])
    friction_cats = n.get("friction_categories", [])
    features = n.get("features", [])
    patterns = n.get("patterns", [])
    horizon = n.get("horizon", [])
    fun = n.get("fun_ending", {})
    claude_md = n.get("claude_md_suggestions", [])

    # Stats
    total_msgs = agg["totalMessages"]
    total_sessions = agg["totalSessions"]
    total_days = len(agg["dailyActivity"])
    total_tools = sum(d["toolCallCount"] for d in agg["dailyActivity"])
    msgs_per_day = round(total_msgs / total_days, 1) if total_days > 0 else 0
    first_date = (agg.get("firstSessionDate") or "")[:10]
    last_date = agg["dailyActivity"][-1]["date"] if agg["dailyActivity"] else ""

    subtitle = (f"{total_msgs:,} messages across {total_sessions:,} sessions | "
                f"{first_date} to {last_date} | "
                f"{' + '.join(m['name'] for m in machine_data)} | "
                f"{agg['total_facets']} sessions analyzed")

    # Build chart data from aggregated facets + meta
    tool_items = dict_to_items(dict(list(agg["tool_counts"].items())[:6]))
    lang_items = dict_to_items(dict(list(agg["languages"].items())[:6]))
    goal_items = dict_to_items(dict(list(agg["goal_categories"].items())[:6]))
    session_type_items = dict_to_items(agg["session_types"])
    outcome_items = dict_to_items(agg["outcomes"])
    satisfaction_items = dict_to_items(agg["satisfaction"])
    friction_items = dict_to_items(agg["friction_counts"])
    helped_items = dict_to_items(agg["primary_success"])
    error_items = dict_to_items(dict(list(agg["tool_errors"].items())[:6]))
    response_items = [{"name": k, "count": v} for k, v in agg["response_times"].items() if v > 0]

    # Per-machine chart
    machine_rows = ""
    for pm in agg["perMachine"]:
        pct = round(pm["messages"] / total_msgs * 100) if total_msgs > 0 else 0
        machine_rows += (f'<div class="bar-row"><div class="bar-label">{esc(pm["name"])}</div>'
                         f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%;background:#6366f1"></div></div>'
                         f'<div class="bar-value">{pm["messages"]:,} msgs ({pm["sessions"]:,} sessions)</div></div>')

    # At a glance
    glance_html = ""
    if not narratives:
        glance_html = '''<div class="at-a-glance" style="background: linear-gradient(135deg, #fef2f2 0%, #fecaca 100%); border-color: #dc2626;">
      <div class="glance-title" style="color: #991b1b;">AI Narrative Unavailable</div>
      <div class="glance-sections">
        <div class="glance-section" style="color: #7f1d1d;">AI narrative generation was skipped or failed. Run without --no-ai flag, or check stderr for details. Quantitative data below is complete.</div>
      </div>
    </div>'''
    elif glance:
        glance_html = f'''<div class="at-a-glance">
      <div class="glance-title">At a Glance</div>
      <div class="glance-sections">
        <div class="glance-section"><strong>What's working:</strong> {esc(glance.get("working", ""))}</div>
        <div class="glance-section"><strong>What's hindering you:</strong> {esc(glance.get("hindering", ""))}</div>
        <div class="glance-section"><strong>Quick wins to try:</strong> {esc(glance.get("quick_wins", ""))}</div>
        <div class="glance-section"><strong>Ambitious workflows:</strong> {esc(glance.get("ambitious", ""))}</div>
      </div>
    </div>'''

    # Project areas
    areas_html = "".join(f'''<div class="project-area">
      <div class="area-header"><span class="area-name">{esc(a.get("name",""))}</span>
      <span class="area-count">{esc(a.get("session_count",""))}</span></div>
      <div class="area-desc">{esc(a.get("description",""))}</div></div>''' for a in areas)

    # Usage narrative
    usage_html = ""
    if usage:
        usage_html = f'''<div class="narrative">
      <p>{esc(usage.get("paragraph1",""))}</p><p>{esc(usage.get("paragraph2",""))}</p>
      <div class="key-insight"><strong>Key pattern:</strong> {esc(usage.get("key_insight",""))}</div></div>'''

    # Big wins
    wins_html = "".join(f'''<div class="big-win"><div class="big-win-title">{esc(w.get("title",""))}</div>
      <div class="big-win-desc">{esc(w.get("description",""))}</div></div>''' for w in wins)

    # Friction
    friction_html = ""
    for fc in friction_cats:
        examples = "".join(f"<li>{esc(ex)}</li>" for ex in fc.get("examples", []))
        friction_html += f'''<div class="friction-category"><div class="friction-title">{esc(fc.get("title",""))}</div>
          <div class="friction-desc">{esc(fc.get("description",""))}</div>
          <ul class="friction-examples">{examples}</ul></div>'''

    # CLAUDE.md suggestions
    claude_md_html = ""
    if claude_md:
        items_html = "".join(f'''<div class="claude-md-item">
          <input type="checkbox" id="cmd-{i}" class="cmd-checkbox" checked data-text="{esc(c.get("code",""))}">
          <label for="cmd-{i}"><code class="cmd-code">{esc(c.get("code",""))}</code>
          <button class="copy-btn" onclick="copyCmdItem({i})">Copy</button></label>
          <div class="cmd-why">{esc(c.get("why",""))}</div></div>''' for i, c in enumerate(claude_md))
        claude_md_html = f'''<div class="claude-md-section"><h3>Suggested CLAUDE.md Additions</h3>
      <div class="claude-md-actions"><button class="copy-all-btn" onclick="copyAllCheckedClaudeMd()">Copy All Checked</button></div>
      {items_html}</div>'''

    # Features
    features_html = "".join(f'''<div class="feature-card"><div class="feature-title">{esc(f.get("title",""))}</div>
      <div class="feature-oneliner">{esc(f.get("oneliner",""))}</div>
      <div class="feature-why"><strong>Why for you:</strong> {esc(f.get("why",""))}</div>
      <div class="feature-examples"><div class="feature-example"><div class="example-code-row">
        <code class="example-code">{esc(f.get("code",""))}</code>
        <button class="copy-btn" onclick="copyText(this)">Copy</button></div></div></div></div>''' for f in features)

    # Patterns
    patterns_html = "".join(f'''<div class="pattern-card"><div class="pattern-title">{esc(p.get("title",""))}</div>
      <div class="pattern-summary">{esc(p.get("summary",""))}</div>
      <div class="pattern-detail">{esc(p.get("detail",""))}</div>
      <div class="copyable-prompt-section"><div class="prompt-label">Paste into Claude Code:</div>
        <div class="copyable-prompt-row"><code class="copyable-prompt">{esc(p.get("prompt",""))}</code>
        <button class="copy-btn" onclick="copyText(this)">Copy</button></div></div></div>''' for p in patterns)

    # Horizon
    horizon_html = "".join(f'''<div class="horizon-card"><div class="horizon-title">{esc(h.get("title",""))}</div>
      <div class="horizon-possible">{esc(h.get("possible",""))}</div>
      <div class="horizon-tip"><strong>Getting started:</strong> {esc(h.get("tip",""))}</div>
      <div class="pattern-prompt"><div class="prompt-label">Paste into Claude Code:</div>
        <code>{esc(h.get("prompt",""))}</code>
        <button class="copy-btn" onclick="copyText(this)">Copy</button></div></div>''' for h in horizon)

    # Fun ending
    fun_html = ""
    if fun:
        fun_html = f'''<div class="fun-ending"><div class="fun-headline">"{esc(fun.get("headline",""))}"</div>
      <div class="fun-detail">{esc(fun.get("detail",""))}</div></div>'''

    # Multi-clauding
    mc = agg["multi_clauding"]
    mc_pct = round(mc["sessions_involved"] / agg["total_meta"] * 100) if agg["total_meta"] > 0 else 0

    # Response time chart
    rt_max = max((i["count"] for i in response_items), default=1) or 1
    rt_rows = "".join(f'<div class="bar-row"><div class="bar-label">{esc(i["name"])}</div>'
                       f'<div class="bar-track"><div class="bar-fill" style="width:{i["count"]/rt_max*100}%;background:#6366f1"></div></div>'
                       f'<div class="bar-value">{i["count"]}</div></div>' for i in response_items)

    hour_json = json.dumps(agg["hourCounts"])

    html = f'''<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Claude Code Insights — Combined</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; background: #f8fafc; color: #334155; line-height: 1.65; padding: 48px 24px; }}
    .container {{ max-width: 800px; margin: 0 auto; }}
    h1 {{ font-size: 32px; font-weight: 700; color: #0f172a; margin-bottom: 8px; }}
    h2 {{ font-size: 20px; font-weight: 600; color: #0f172a; margin-top: 48px; margin-bottom: 16px; }}
    .subtitle {{ color: #64748b; font-size: 15px; margin-bottom: 32px; }}
    .nav-toc {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 24px 0 32px 0; padding: 16px; background: white; border-radius: 8px; border: 1px solid #e2e8f0; }}
    .nav-toc a {{ font-size: 12px; color: #64748b; text-decoration: none; padding: 6px 12px; border-radius: 6px; background: #f1f5f9; transition: all 0.15s; }}
    .nav-toc a:hover {{ background: #e2e8f0; color: #334155; }}
    .stats-row {{ display: flex; gap: 24px; margin-bottom: 40px; padding: 20px 0; border-top: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0; flex-wrap: wrap; }}
    .stat {{ text-align: center; }}
    .stat-value {{ font-size: 24px; font-weight: 700; color: #0f172a; }}
    .stat-label {{ font-size: 11px; color: #64748b; text-transform: uppercase; }}
    .at-a-glance {{ background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%); border: 1px solid #f59e0b; border-radius: 12px; padding: 20px 24px; margin-bottom: 32px; }}
    .glance-title {{ font-size: 16px; font-weight: 700; color: #92400e; margin-bottom: 16px; }}
    .glance-sections {{ display: flex; flex-direction: column; gap: 12px; }}
    .glance-section {{ font-size: 14px; color: #78350f; line-height: 1.6; }}
    .glance-section strong {{ color: #92400e; }}
    .project-areas {{ display: flex; flex-direction: column; gap: 12px; margin-bottom: 32px; }}
    .project-area {{ background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; }}
    .area-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
    .area-name {{ font-weight: 600; font-size: 15px; color: #0f172a; }}
    .area-count {{ font-size: 12px; color: #64748b; background: #f1f5f9; padding: 2px 8px; border-radius: 4px; }}
    .area-desc {{ font-size: 14px; color: #475569; line-height: 1.5; }}
    .narrative {{ background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px; margin-bottom: 24px; }}
    .narrative p {{ margin-bottom: 12px; font-size: 14px; color: #475569; line-height: 1.7; }}
    .key-insight {{ background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 12px 16px; margin-top: 12px; font-size: 14px; color: #166534; }}
    .section-intro {{ font-size: 14px; color: #64748b; margin-bottom: 16px; }}
    .big-wins {{ display: flex; flex-direction: column; gap: 12px; margin-bottom: 24px; }}
    .big-win {{ background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 16px; }}
    .big-win-title {{ font-weight: 600; font-size: 15px; color: #166534; margin-bottom: 8px; }}
    .big-win-desc {{ font-size: 14px; color: #15803d; line-height: 1.5; }}
    .friction-categories {{ display: flex; flex-direction: column; gap: 16px; margin-bottom: 24px; }}
    .friction-category {{ background: #fef2f2; border: 1px solid #fca5a5; border-radius: 8px; padding: 16px; }}
    .friction-title {{ font-weight: 600; font-size: 15px; color: #991b1b; margin-bottom: 6px; }}
    .friction-desc {{ font-size: 13px; color: #7f1d1d; margin-bottom: 10px; }}
    .friction-examples {{ margin: 0 0 0 20px; font-size: 13px; color: #334155; }}
    .friction-examples li {{ margin-bottom: 4px; }}
    .claude-md-section {{ background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; padding: 16px; margin-bottom: 20px; }}
    .claude-md-section h3 {{ font-size: 14px; font-weight: 600; color: #1e40af; margin: 0 0 12px 0; }}
    .claude-md-actions {{ margin-bottom: 12px; padding-bottom: 12px; border-bottom: 1px solid #dbeafe; }}
    .copy-all-btn {{ background: #2563eb; color: white; border: none; border-radius: 4px; padding: 6px 12px; font-size: 12px; cursor: pointer; font-weight: 500; }}
    .copy-all-btn:hover {{ background: #1d4ed8; }}
    .copy-all-btn.copied {{ background: #16a34a; }}
    .claude-md-item {{ display: flex; flex-wrap: wrap; align-items: flex-start; gap: 8px; padding: 10px 0; border-bottom: 1px solid #dbeafe; }}
    .claude-md-item:last-child {{ border-bottom: none; }}
    .cmd-checkbox {{ margin-top: 2px; }}
    .cmd-code {{ background: white; padding: 8px 12px; border-radius: 4px; font-size: 12px; color: #1e40af; border: 1px solid #bfdbfe; font-family: monospace; display: block; white-space: pre-wrap; word-break: break-word; flex: 1; }}
    .cmd-why {{ font-size: 12px; color: #64748b; width: 100%; padding-left: 24px; margin-top: 4px; }}
    .features-section, .patterns-section {{ display: flex; flex-direction: column; gap: 12px; margin: 16px 0; }}
    .feature-card {{ background: #f0fdf4; border: 1px solid #86efac; border-radius: 8px; padding: 16px; }}
    .pattern-card {{ background: #f0f9ff; border: 1px solid #7dd3fc; border-radius: 8px; padding: 16px; }}
    .feature-title, .pattern-title {{ font-weight: 600; font-size: 15px; color: #0f172a; margin-bottom: 6px; }}
    .feature-oneliner {{ font-size: 14px; color: #475569; margin-bottom: 8px; }}
    .pattern-summary {{ font-size: 14px; color: #475569; margin-bottom: 8px; }}
    .feature-why, .pattern-detail {{ font-size: 13px; color: #334155; line-height: 1.5; }}
    .feature-examples {{ margin-top: 12px; }}
    .feature-example {{ padding: 8px 0; }}
    .example-code-row {{ display: flex; align-items: flex-start; gap: 8px; }}
    .example-code {{ flex: 1; background: #f1f5f9; padding: 8px 12px; border-radius: 4px; font-family: monospace; font-size: 12px; color: #334155; overflow-x: auto; white-space: pre-wrap; }}
    .copyable-prompt-section {{ margin-top: 12px; padding-top: 12px; border-top: 1px solid #e2e8f0; }}
    .copyable-prompt-row {{ display: flex; align-items: flex-start; gap: 8px; }}
    .copyable-prompt {{ flex: 1; background: #f8fafc; padding: 10px 12px; border-radius: 4px; font-family: monospace; font-size: 12px; color: #334155; border: 1px solid #e2e8f0; white-space: pre-wrap; line-height: 1.5; }}
    .prompt-label {{ font-size: 11px; font-weight: 600; text-transform: uppercase; color: #64748b; margin-bottom: 6px; }}
    .copy-btn {{ background: #e2e8f0; border: none; border-radius: 4px; padding: 4px 8px; font-size: 11px; cursor: pointer; color: #475569; flex-shrink: 0; }}
    .copy-btn:hover {{ background: #cbd5e1; }}
    .charts-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin: 24px 0; }}
    .chart-card {{ background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; }}
    .chart-title {{ font-size: 12px; font-weight: 600; color: #64748b; text-transform: uppercase; margin-bottom: 12px; }}
    .bar-row {{ display: flex; align-items: center; margin-bottom: 6px; }}
    .bar-label {{ width: 120px; font-size: 11px; color: #475569; flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .bar-track {{ flex: 1; height: 6px; background: #f1f5f9; border-radius: 3px; margin: 0 8px; }}
    .bar-fill {{ height: 100%; border-radius: 3px; }}
    .bar-value {{ width: 50px; font-size: 11px; font-weight: 500; color: #64748b; text-align: right; }}
    .empty {{ color: #94a3b8; font-size: 13px; }}
    .horizon-section {{ display: flex; flex-direction: column; gap: 16px; }}
    .horizon-card {{ background: linear-gradient(135deg, #faf5ff 0%, #f5f3ff 100%); border: 1px solid #c4b5fd; border-radius: 8px; padding: 16px; }}
    .horizon-title {{ font-weight: 600; font-size: 15px; color: #5b21b6; margin-bottom: 8px; }}
    .horizon-possible {{ font-size: 14px; color: #334155; margin-bottom: 10px; line-height: 1.5; }}
    .horizon-tip {{ font-size: 13px; color: #6b21a8; background: rgba(255,255,255,0.6); padding: 8px 12px; border-radius: 4px; margin-bottom: 8px; }}
    .pattern-prompt {{ background: #f8fafc; padding: 12px; border-radius: 6px; margin-top: 12px; border: 1px solid #e2e8f0; }}
    .pattern-prompt code {{ font-family: monospace; font-size: 12px; color: #334155; display: block; white-space: pre-wrap; margin-bottom: 8px; }}
    .fun-ending {{ background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%); border: 1px solid #fbbf24; border-radius: 12px; padding: 24px; margin-top: 40px; text-align: center; }}
    .fun-headline {{ font-size: 18px; font-weight: 600; color: #78350f; margin-bottom: 8px; }}
    .fun-detail {{ font-size: 14px; color: #92400e; }}
    @media (max-width: 640px) {{ .charts-row {{ grid-template-columns: 1fr; }} .stats-row {{ justify-content: center; }} }}
  </style>
</head>
<body>
  <div class="container">
    <h1>Claude Code Insights <span style="font-size: 16px; color: #64748b; font-weight: 400;">— Combined</span></h1>
    <p class="subtitle">{esc(subtitle)}</p>

    {glance_html}

    <nav class="nav-toc">
      <a href="#section-machines">Machines</a>
      <a href="#section-work">What You Work On</a>
      <a href="#section-usage">How You Use CC</a>
      <a href="#section-wins">Impressive Things</a>
      <a href="#section-friction">Where Things Go Wrong</a>
      <a href="#section-features">Features to Try</a>
      <a href="#section-patterns">New Patterns</a>
      <a href="#section-horizon">On the Horizon</a>
    </nav>

    <div class="stats-row">
      <div class="stat"><div class="stat-value">{total_msgs:,}</div><div class="stat-label">Messages</div></div>
      <div class="stat"><div class="stat-value">{total_sessions:,}</div><div class="stat-label">Sessions</div></div>
      <div class="stat"><div class="stat-value">+{agg["lines_added"]:,}/-{agg["lines_removed"]:,}</div><div class="stat-label">Lines</div></div>
      <div class="stat"><div class="stat-value">{agg["files_modified"]:,}</div><div class="stat-label">Files</div></div>
      <div class="stat"><div class="stat-value">{total_days}</div><div class="stat-label">Days</div></div>
      <div class="stat"><div class="stat-value">{msgs_per_day}</div><div class="stat-label">Msgs/Day</div></div>
    </div>

    <h2 id="section-machines">Cross-Machine Breakdown</h2>
    <div class="chart-card" style="margin: 24px 0;">
      <div class="chart-title">Messages by Machine</div>
      {machine_rows}
    </div>

    <h2 id="section-work">What You Work On</h2>
    <div class="project-areas">{areas_html}</div>

    <div class="charts-row">
      {bar_chart_html("What You Wanted", goal_items, "#2563eb")}
      {bar_chart_html("Top Tools Used", tool_items, "#0891b2")}
    </div>
    <div class="charts-row">
      {bar_chart_html("Languages", lang_items, "#10b981")}
      {bar_chart_html("Session Types", session_type_items, "#8b5cf6")}
    </div>

    <h2 id="section-usage">How You Use Claude Code</h2>
    {usage_html}

    <div class="chart-card" style="margin: 24px 0;">
      <div class="chart-title">User Response Time Distribution</div>
      {rt_rows if rt_rows else '<div class="empty">Not enough data</div>'}
      <div style="font-size: 12px; color: #64748b; margin-top: 8px;">
        Median: {agg["median_response_time"]}s &bull; Average: {agg["avg_response_time"]}s
      </div>
    </div>

    <div class="chart-card" style="margin: 24px 0;">
      <div class="chart-title">Multi-Clauding (Parallel Sessions)</div>
      <div style="display: flex; gap: 24px; margin: 12px 0;">
        <div style="text-align: center;"><div style="font-size: 24px; font-weight: 700; color: #7c3aed;">{mc["overlap_events"]}</div>
          <div style="font-size: 11px; color: #64748b; text-transform: uppercase;">Overlap Events</div></div>
        <div style="text-align: center;"><div style="font-size: 24px; font-weight: 700; color: #7c3aed;">{mc["sessions_involved"]}</div>
          <div style="font-size: 11px; color: #64748b; text-transform: uppercase;">Sessions Involved</div></div>
        <div style="text-align: center;"><div style="font-size: 24px; font-weight: 700; color: #7c3aed;">{mc_pct}%</div>
          <div style="font-size: 11px; color: #64748b; text-transform: uppercase;">Of Sessions</div></div>
      </div>
      <p style="font-size: 13px; color: #475569; margin-top: 12px;">
        Parallel sessions detected across all machines — including cross-machine multi-clauding.
      </p>
    </div>

    <div class="charts-row">
      <div class="chart-card">
        <div class="chart-title" style="display: flex; align-items: center; gap: 12px;">
          Sessions by Time of Day
          <select id="timezone-select" style="font-size: 12px; padding: 4px 8px; border-radius: 4px; border: 1px solid #e2e8f0;">
            <option value="0">PT (UTC-8)</option>
            <option value="3">ET (UTC-5)</option>
            <option value="8">London (UTC)</option>
            <option value="9">CET (UTC+1)</option>
            <option value="17">Tokyo (UTC+9)</option>
          </select>
        </div>
        <div id="hour-histogram"></div>
      </div>
      {bar_chart_html("Tool Errors", error_items, "#dc2626")}
    </div>

    <h2 id="section-wins">Impressive Things You Did</h2>
    <div class="big-wins">{wins_html}</div>

    <div class="charts-row">
      {bar_chart_html("What Helped Most", helped_items, "#16a34a")}
      {bar_chart_html("Outcomes", outcome_items, "#8b5cf6")}
    </div>

    <h2 id="section-friction">Where Things Go Wrong</h2>
    <p class="section-intro">{esc(n.get("friction_intro", ""))}</p>
    <div class="friction-categories">{friction_html}</div>

    <div class="charts-row">
      {bar_chart_html("Primary Friction Types", friction_items, "#dc2626")}
      {bar_chart_html("Inferred Satisfaction", satisfaction_items, "#eab308")}
    </div>

    <h2 id="section-features">Features to Try</h2>
    {claude_md_html}
    <div class="features-section">{features_html}</div>

    <h2 id="section-patterns">New Ways to Use Claude Code</h2>
    <div class="patterns-section">{patterns_html}</div>

    <h2 id="section-horizon">On the Horizon</h2>
    <div class="horizon-section">{horizon_html}</div>

    {fun_html}

  </div>
  <script>
    function copyText(btn) {{
      const code = btn.previousElementSibling;
      navigator.clipboard.writeText(code.textContent).then(() => {{
        btn.textContent = 'Copied!'; setTimeout(() => {{ btn.textContent = 'Copy'; }}, 2000);
      }});
    }}
    function copyCmdItem(idx) {{
      const cb = document.getElementById('cmd-' + idx);
      if (cb) navigator.clipboard.writeText(cb.dataset.text).then(() => {{
        const btn = cb.nextElementSibling.querySelector('.copy-btn');
        if (btn) {{ btn.textContent = 'Copied!'; setTimeout(() => {{ btn.textContent = 'Copy'; }}, 2000); }}
      }});
    }}
    function copyAllCheckedClaudeMd() {{
      const cbs = document.querySelectorAll('.cmd-checkbox:checked');
      const texts = []; cbs.forEach(cb => {{ if (cb.dataset.text) texts.push(cb.dataset.text); }});
      const btn = document.querySelector('.copy-all-btn');
      if (btn) navigator.clipboard.writeText(texts.join('\\n')).then(() => {{
        btn.textContent = 'Copied ' + texts.length + ' items!'; btn.classList.add('copied');
        setTimeout(() => {{ btn.textContent = 'Copy All Checked'; btn.classList.remove('copied'); }}, 2000);
      }});
    }}
    const rawHourCounts = {hour_json};
    function updateHourHistogram(offset) {{
      const periods = [
        {{ label: "Morning (6-12)", range: [6,7,8,9,10,11] }},
        {{ label: "Afternoon (12-18)", range: [12,13,14,15,16,17] }},
        {{ label: "Evening (18-24)", range: [18,19,20,21,22,23] }},
        {{ label: "Night (0-6)", range: [0,1,2,3,4,5] }}
      ];
      const adj = {{}};
      for (const [h, c] of Object.entries(rawHourCounts)) {{
        const nh = (parseInt(h) + offset + 24) % 24;
        adj[nh] = (adj[nh] || 0) + c;
      }}
      const pc = periods.map(p => ({{ label: p.label, count: p.range.reduce((s,h) => s + (adj[h]||0), 0) }}));
      const mx = Math.max(...pc.map(p => p.count)) || 1;
      const el = document.getElementById('hour-histogram');
      el.textContent = '';
      pc.forEach(p => {{
        const r = document.createElement('div'); r.className = 'bar-row';
        const l = document.createElement('div'); l.className = 'bar-label'; l.textContent = p.label;
        const t = document.createElement('div'); t.className = 'bar-track';
        const f = document.createElement('div'); f.className = 'bar-fill';
        f.style.width = (p.count/mx)*100+'%'; f.style.background = '#8b5cf6'; t.appendChild(f);
        const v = document.createElement('div'); v.className = 'bar-value'; v.textContent = p.count;
        r.appendChild(l); r.appendChild(t); r.appendChild(v); el.appendChild(r);
      }});
    }}
    document.getElementById('timezone-select').addEventListener('change', function() {{ updateHourHistogram(parseInt(this.value)); }});
    updateHourHistogram(0);
  </script>
</body>
</html>'''

    return html


# ─── Terminal Output ─────────────────────────────────────────────────────────

def print_terminal(agg):
    total_msgs = agg["totalMessages"]
    total_sessions = agg["totalSessions"]
    total_days = len(agg["dailyActivity"])

    print()
    print("+" + "=" * 54 + "+")
    print("|       Claude Code — Combined Insights              |")
    print("+" + "=" * 54 + "+")
    print()
    for pm in agg["perMachine"]:
        pct = round(pm["messages"] / total_msgs * 100) if total_msgs > 0 else 0
        print(f"  {pm['name']:<20s} {pm['messages']:>8,} msgs  {pm['sessions']:>5,} sessions  ({pct}%)")
    print(f"  {'─' * 55}")
    print(f"  {'Total':<20s} {total_msgs:>8,} msgs  {total_sessions:>5,} sessions")
    print()
    print(f"  Active days:      {total_days}")
    print(f"  Facets analyzed:  {agg['total_facets']}")
    print(f"  Session meta:     {agg['total_meta']}")
    print(f"  Lines changed:    +{agg['lines_added']:,} / -{agg['lines_removed']:,}")
    print(f"  Files modified:   {agg['files_modified']:,}")
    print()

    print("  Top Tools:")
    for tool, count in list(agg["tool_counts"].items())[:8]:
        print(f"    {tool:<20s} {count:>6,}")
    print()

    print("  Outcomes:")
    for outcome, count in agg["outcomes"].items():
        print(f"    {outcome.replace('_', ' ').title():<25s} {count:>4}")
    print()

    print("  Satisfaction:")
    for sat, count in agg["satisfaction"].items():
        print(f"    {sat.replace('_', ' ').title():<25s} {count:>4}")
    print()


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    global NARRATIVE_MODEL, MACHINES

    parser = argparse.ArgumentParser(
        description="Cross-machine Claude Code insights merger with AI narrative generation.",
    )
    parser.add_argument(
        "--stats-only", "--stats", action="store_true",
        help="Terminal output only, no HTML or AI",
    )
    parser.add_argument(
        "--json", action="store_true", dest="dump_json",
        help="Dump merged data as JSON",
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="Don't open browser after generating HTML",
    )
    parser.add_argument(
        "--model", choices=["opus", "sonnet", "haiku"], default="sonnet",
        help="AI model choice (default: sonnet)",
    )
    parser.add_argument(
        "--no-ai", action="store_true",
        help="Skip AI narrative generation, render HTML with data charts only",
    )
    parser.add_argument(
        "--machine", action="append", metavar="NAME",
        help="Only collect from named machine(s); case-insensitive partial match (repeatable)",
    )
    parser.add_argument(
        "--output", type=Path, metavar="PATH",
        help="Save HTML to specific path instead of default /tmp location",
    )
    args = parser.parse_args()

    NARRATIVE_MODEL = args.model

    # Filter machines if --machine was given
    if args.machine:
        patterns = [p.lower() for p in args.machine]
        filtered = [m for m in MACHINES if any(p in m["name"].lower() for p in patterns)]
        if not filtered:
            print(f"  Error: no machines matched {args.machine}", file=sys.stderr)
            print(f"  Available: {[m['name'] for m in MACHINES]}", file=sys.stderr)
            sys.exit(1)
        MACHINES = filtered

    print()
    print("  Claude Code Cross-Machine Insights (v2 — facet-based)")
    print("  " + "─" * 48)

    # 1. Collect
    machine_data = collect_all()

    # 2. Aggregate
    print("  Aggregating data...")
    agg = aggregate_all(machine_data)

    if args.dump_json:
        # Exclude raw facets/meta from JSON dump (too large)
        output = {k: v for k, v in agg.items() if k not in ("facets", "session_meta")}
        print(json.dumps(output, indent=2, default=str))
        return

    if args.stats_only:
        print_terminal(agg)
        return

    # 3. Generate narratives (skip with --no-ai)
    narratives = None
    if not args.no_ai:
        narratives = generate_narratives(agg, machine_data)

    # 4. Render HTML
    print("  Rendering HTML...")
    html_content = render_html(agg, machine_data, narratives)

    # 5. Save and open
    today = datetime.now().strftime("%Y-%m-%d")
    if args.output:
        output_file = args.output
    else:
        output_file = OUTPUT_DIR / f"claude-insights-combined-{today}.html"
    with open(output_file, "w") as f:
        f.write(html_content)
    print(f"  Saved to: {output_file}")

    backup_dir = Path.home() / ".claude" / "insights"
    backup_dir.mkdir(exist_ok=True)
    backup_file = backup_dir / f"combined-{today}.html"
    with open(backup_file, "w") as f:
        f.write(html_content)
    print(f"  Backup:   {backup_file}")

    if not args.no_open:
        print("  Opening in browser...")
        webbrowser.open(f"file://{output_file}")

    print()
    print("  Done!")
    print()


if __name__ == "__main__":
    main()
