#!/usr/bin/env python3
"""Update this summary directory from the benchmark results in runs/.

The summary directory holds one re-rendered copy of each run's report.html
plus an index.html that ranks every listed run. The re-render is produced
from the run's report.json via harness/report.py, with file links rebased to
../runs/<run>/ so they resolve from this directory.

A run needs a re-render when the SHA-256 of its runs/<run>/report.json, or
the redact choice, differs from what is recorded in run_map.json, or the
stored page is missing. Nothing
is written until the user confirms each individual change, so a partially
written run can be skipped and picked up on a later pass. At each prompt you
can answer y, n, or type the artifact filename to use. A declined new run
stays unmapped and is offered again on the next pass, while a declined
update keeps the run listed in the index.

Every non-dry pass also rewrites trajectories.html, three linked graphs
(passed solutions, accumulated score, cumulative context tokens) over the runs
listed in the index. The page loads the vendored plotly.js bundle
(trajectories.PLOTLY_BUNDLE) from this directory and needs no network.

By default the stored page drops the embedded prompt, the agent logs,
and the links to the prompt, session log, workspace, and log files, so it
can be shared without the run's task materials. Pass --no-redact to keep
those parts (for example when the run is already public or is not shared).

An optional runs/<run>/title.json customizes the displayed name: a JSON
object with a required "title", an optional "subtitle", and an optional
"logo" naming a file in this directory's assets/ folder. It is shown in the
index row and in the stored report page, and its contents are part of the
change-detection digest. For a run without a title.json, the summary uses
an in-memory placeholder (title = run directory name, subtitle = model name,
logo = best lab match or generic.svg) without modifying the run directory.

Usage:
    python3 summary/update_summary.py             # interactive (redacted by default)
    python3 summary/update_summary.py --dry-run   # report only, write nothing
    python3 summary/update_summary.py --yes       # accept all changes
    python3 summary/update_summary.py --no-redact # keep prompts, logs, and task links

Options --runs-dir and --summary-dir override the default locations, which
are resolved relative to this script: runs/ in the repository root and the
directory containing this file.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# harness/ holds report.py; the repo root is needed for its harness.* fallback imports.
for _p in (os.path.join(ROOT, "harness"), ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import report  # noqa: E402  harness/report.py, shared with the runs/ reports
import trajectories  # noqa: E402  harness/trajectories.py, the trajectory graphs page

MAP_FILE = "run_map.json"
INDEX_FILE = "index.html"
TRAJECTORIES_FILE = "trajectories.html"

NAME_PATTERN = r"[A-Za-z0-9._ -]+"


def report_path(run_dir: str) -> str:
    return os.path.join(run_dir, "report.html")


def totals_path(run_dir: str) -> str:
    return os.path.join(run_dir, "report.json")


def load_report(run_dir: str) -> tuple[dict, bytes]:
    """Return (parsed report.json, raw file bytes) for a run.

    The raw bytes let the caller fold in the title file's bytes for the
    change-detection digest, so editing the display title of an existing run
    triggers a re-render.
    """
    with open(totals_path(run_dir), "rb") as fh:
        raw = fh.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{totals_path(run_dir)} must contain an object")
    return data, raw


def propose_filename(model: str, run_id: str) -> str:
    """Derive a readable artifact name from the reported model name."""
    base = (model or run_id).rsplit("/", 1)[-1]
    parts = [p for p in base.split(":") if p]
    if parts:
        parts[0] = re.sub(r"-gguf$", "", parts[0], flags=re.I)
    name = "_".join(parts) or run_id
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name if name.lower().endswith(".html") else name + ".html"


def existing_artifacts(summary_dir: str) -> set:
    """Return the .html files already in the summary directory, excluding the index.

    These names are claimed too, so a hand-placed file the script does not own
    is never overwritten.
    """
    if not os.path.isdir(summary_dir):
        return set()
    return {f for f in os.listdir(summary_dir)
            if f.endswith(".html") and f != INDEX_FILE}


def unique_name(name: str, taken: set) -> str:
    """Return `name`, or a -2/-3/... variant of it, that is not in `taken`."""
    stem = re.sub(r"\.html$", "", name)
    candidate, n = name, 2
    while candidate in taken:
        candidate = f"{stem}-{n}.html"
        n += 1
    return candidate


def load_map(summary_dir: str) -> dict:
    """Return the run id -> {file, digest, redacted} map, empty if absent.

    digest is the SHA-256 of the run's report.json when the stored page was
    last rendered; None (or a pre-renders string filename) means the stored
    page needs re-rendering.
    """
    path = os.path.join(summary_dir, MAP_FILE)
    if os.path.exists(path):
        with open(path) as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            mapping = {}
            for key, value in data.items():
                if isinstance(value, str):
                    mapping[key] = {"file": value, "digest": None, "redacted": False}
                elif (isinstance(value, dict) and isinstance(value.get("file"), str)
                      and isinstance(value.get("redacted"), bool)):
                    mapping[key] = {"file": value["file"],
                                    "digest": value.get("digest"),
                                    "redacted": value["redacted"]}
            return mapping
    return {}


def save_map(summary_dir: str, mapping: dict) -> None:
    """Persist the run id -> {file, digest, redacted} map."""
    path = os.path.join(summary_dir, MAP_FILE)
    ordered = {k: {"file": v["file"], "digest": v["digest"], "redacted": v["redacted"]}
               for k, v in sorted(mapping.items())}
    with open(path, "w") as fh:
        json.dump(ordered, fh, indent=2)
        fh.write("\n")


def discover_runs(runs_dir: str) -> list:
    """Return (run_id, run_dir) for each run directory holding both report files."""
    runs = []
    for entry in sorted(os.listdir(runs_dir)):
        d = os.path.join(runs_dir, entry)
        if not os.path.isdir(d) or entry.startswith("."):
            continue
        if os.path.exists(report_path(d)) and os.path.exists(totals_path(d)):
            runs.append((entry, d))
    return runs


def read_totals(report_data: dict, run_id: str) -> dict:
    """Return the index row data for a run, zeroing any null totals.

    run_id comes from the runs/ directory name, which is also the map key,
    the index row's link text, and the base of the stored page's file links,
    so rows stay addressable even if report.json disagrees about its own id.
    """
    totals = report_data.get("totals", {})
    # report.py writes null for final_score_pct when the weight total is zero.
    title = report_data.get("title") or {}
    return {
        "run_id": run_id,
        "title": title.get("title") or "",
        "subtitle": title.get("subtitle") or "",
        "logo": title.get("logo") or "",
        "model": (report_data.get("model") or {}).get("name") or "unknown",
        "challenges": int(totals.get("challenges") or 0),
        "passed": int(totals.get("passed") or 0),
        "failed": int(totals.get("failed") or 0),
        "error": int(totals.get("error") or 0),
        "score": float(totals.get("sum_final_score") or 0.0),
        "max_possible": float(totals.get("max_possible") or 0.0),
        "pct": float(totals.get("final_score_pct") or 0.0) * 100.0,
        # None when no task in the run has session logs.
        "avg_tokens": totals.get("avg_context_tokens_per_task"),
        # Recomputed from challenges when the report predates this metric.
        "avg_time": report.avg_duration_s(report_data),
    }


def seed_mapping(runs: list, reports: dict, mapping: dict, summary_dir: str) -> None:
    """Claim an existing summary copy by its expected name for unmapped runs.

    This recovers the run -> artifact mapping for artifacts created before the
    map file existed. Claimed copies are marked with a null digest, so the
    first pass re-renders them under the same name.
    """
    claimed = {v["file"] for v in mapping.values()}
    for run_id, run_dir in runs:
        if run_id in mapping:
            continue
        model = (reports[run_id].get("model") or {}).get("name") or "unknown"
        name = propose_filename(model, run_id)
        if name not in claimed and os.path.isfile(os.path.join(summary_dir, name)):
            mapping[run_id] = {"file": name, "digest": None, "redacted": False}
            claimed.add(name)
            print(f"claimed existing artifact {name} for run {run_id}")


def ask_name(prompt: str, default: str | None = None) -> str | None:
    """Ask the user for an artifact filename.

    Returns the filename to use, or None when the user declines. An empty answer
    or y selects `default`, which is only offered when a default is given.
    """
    suffix = " [Y/n or filename]: " if default else " [filename or n]: "
    help_text = "  answer y or n, or type a filename ending in .html" if default \
        else "  type a filename ending in .html, or n to skip"
    while True:
        try:
            answer = input(prompt + suffix).strip()
        except EOFError:
            print()
            return None
        low = answer.lower()
        if low in ("n", "no"):
            return None
        if default and low in ("", "y", "yes"):
            return default
        if re.fullmatch(NAME_PATTERN, answer):
            return answer if answer.lower().endswith(".html") else answer + ".html"
        print(help_text)


def confirm_run(run_id: str, state: str, name: str, mode: str) -> str | None:
    """Ask whether to store this run's re-rendered report; return its filename or None."""
    print(f"\n{run_id}: {state}")
    print(f"  artifact -> {name}")
    if mode == "yes":
        return name
    if mode == "skip":
        return None
    return ask_name(f"  store {state} report for {run_id}?", default=name)


def title_for_summary(run_dir: str, run_id: str, model: str) -> tuple[dict | None, bytes]:
    """Return the title override and digest bytes without modifying the run."""
    path = os.path.join(run_dir, "title.json")
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            raw = fh.read()
        return report.load_title_file(Path(run_dir)), raw
    title = report.placeholder_title(run_id, model)
    return title, (json.dumps(title, indent=2) + "\n").encode()


def render_index(rows: list) -> str:
    """Render index.html, ranked by final score.

    Row links point at the stored artifacts in this directory, so the directory
    is self-contained and a redacted page is never bypassed. A run stays listed
    even when its copy is out of date or missing.
    """
    rows = sorted(rows, key=lambda r: -r["score"])
    body = []
    for i, r in enumerate(rows, 1):
        href = html.escape(r["artifact"])
        run_label = r["title"] or r["run_id"]
        sub_label = r["subtitle"] or r["model"]
        # Sorted by the visible label; escaped so quotes still parse after readback.
        # data-s columns must line up with the table headers in order.
        # Sort keys use -1 when the raw value is absent so undefined ratios sort first.
        score = r["score"]
        avg_tokens = r["avg_tokens"]
        avg_time = r["avg_time"]
        avg = avg_tokens if avg_tokens is not None else -1
        avg_time_key = avg_time if avg_time is not None else -1
        # Derived efficiency ratios (index-only); -1 when the divisor is absent.
        score_per_sec = score / avg_time if avg_time else -1          # score / avg time/task
        score_per_1k = score * 1000.0 / avg_tokens if avg_tokens else -1  # score / 1k tokens
        logo = r["logo"] or report.get_logo(run_label or sub_label) \
            or report.GENERIC_LOGO
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", logo):
            logo = report.GENERIC_LOGO
        # data-s order must match the <th> order: pct stays last (% of max).
        # Index 11 holds the chart-bar icon and is not a sortable column.
        # Index 12 holds the run's results filename (used by the chart's
        # clickable bar links) and is also not a sortable column.
        data = html.escape(json.dumps([i, run_label, r["passed"], r["failed"],
                                      r["error"], round(score, 2), avg, avg_time_key,
                                      score_per_sec, score_per_1k, round(r["pct"], 2),
                                      logo, r["artifact"]]))
        bar = round(r["pct"], 1)
        avg_time_str = f"{report.compact(avg_time)}s" if avg_time is not None else '—'
        score_per_sec_str = f"{score / avg_time:.3f}" if avg_time else '—'
        score_per_1k_str = f"{score * 1000 / avg_tokens:.3f}" if avg_tokens else '—'
        logo_img = f'<img src="assets/{html.escape(logo, quote=True)}" width="32" height="32" style="flex-shrink:0">'
        body.append(
            f'<tr data-s="{data}"><td>{i}</td>\n'
            f'<td class="run-col"><div class="run-cell">{logo_img}'
            f'<a href="{href}">{html.escape(run_label)}</a>'
            f'<div class="m">{html.escape(sub_label)}</div></div></td>\n'
            f'<td class="n">{r["passed"]}</td><td class="n">{r["failed"]}</td>'
            f'<td class="n">{r["error"]}</td>\n'
            f'<td class="n">{score:.2f} <span class="d">/ {r["max_possible"]:.2f}</span></td>\n'
            f'<td class="n">{report.compact(avg_tokens)}</td>\n'
            f'<td class="n">{avg_time_str}</td>\n'
            f'<td class="n">{score_per_sec_str}</td>\n'
            f'<td class="n">{score_per_1k_str}</td>\n'
            f'<td class="bar"><div><i style="width:{bar}%"></i></div><b>{bar:.1f}%</b></td></tr>'
        )
    spans = {r["challenges"] for r in rows}
    maxima = {r["max_possible"] for r in rows}
    if len(spans) == 1 and len(maxima) == 1:
        scale = f"summed over {rows[0]['challenges']} challenges (max {rows[0]['max_possible']:.2f})"
    elif not spans:
        scale = "summed over 0 challenges (max 0.00)"
    else:
        scale = (f"summed over {min(spans)}-{max(spans)} challenges, with maxima "
                 f"from {min(maxima):.2f} to {max(maxima):.2f}, varying by run")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AD-100 run scores</title>
<style>
:root{{color-scheme:dark}}
body{{margin:0;background:#0d1117;color:#e6edf3;font:14px system-ui,sans-serif;padding:2rem}}
h1{{font-size:1.3rem;margin:0 0 .3rem}}p{{color:#8b949e;margin:0 0 1.2rem}}
table{{border-collapse:collapse;width:100%;max-width:1000px}}
th,td{{padding:.55rem .8rem;border-bottom:1px solid #21262d;text-align:left}}
th{{color:#8b949e;font-weight:600;cursor:pointer;user-select:none}}th:hover{{color:#e6edf3}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}th.n{{text-align:right}}
a{{color:#58a6ff;text-decoration:none}}a:hover{{text-decoration:underline}}
.m{{color:#8b949e;font-size:.78rem;white-space:pre-line}}.d{{color:#8b949e}}
.bar{{min-width:180px}}.bar div{{display:inline-block;width:110px;height:8px;background:#21262d;border-radius:4px;vertical-align:middle;margin-right:.5rem}}
.bar i{{display:block;height:100%;background:#3fb950;border-radius:4px}}
.top{{margin:0 0 1rem}}
.top a{{display:inline-flex;align-items:center;gap:.4rem;padding:.35rem .75rem;border:1px solid #58a6ff;border-radius:999px;background:#388bfd1a;font-weight:600}}.top a:hover{{background:#388bfd33;text-decoration:none}}
.run-cell{{display:flex;align-items:center;gap:.6rem;min-width:11rem}}
.table-wrap{{overflow-x:auto;max-width:1000px}}
.chart-wrap{{overflow-x:auto;max-width:1000px;margin:0 0 1.2rem}}
#t{{min-width:920px}}
#t th,#t td{{box-sizing:border-box}}
#t th:nth-child(1),#t td:nth-child(1){{position:sticky;left:0;width:2.4rem;min-width:2.4rem;background:#0d1117;z-index:1;padding:.55rem .5rem;text-align:right}}
#t th.run-col,#t td.run-col{{position:sticky;left:2.4rem;background:#0d1117;z-index:1}}
#t th:nth-child(1),#t th.run-col{{z-index:2}}
#t td:nth-child(1),#t td.run-col{{box-shadow:1px 0 0 #21262d}}
#chart{{position:relative}}.bar-icon{{position:absolute;pointer-events:none;z-index:4}}
.bar-link{{position:absolute;cursor:pointer;z-index:3}}
.bar-link:hover{{background:#3fb95014}}
@media (max-width:640px){{body{{padding:1rem}}h1{{font-size:1.15rem}}#chart{{min-width:900px}}.run-cell{{min-width:0;max-width:min(240px,60vw)}}.run-cell a,.run-cell .m{{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}}}
</style></head><body>
<h1>AD-100 run scores</h1>
<p>Final score = challenge weight × partial score, {scale}. Click a header to sort. <b>Score / s</b> and <b>Score / 1k tok</b> are derived from score, avg time/task, and avg tokens/task.</p>
<div class="top"><a href="{TRAJECTORIES_FILE}"><svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 2v12h12"/><path d="M4.5 10l3-3.5 2.5 2L14 4.5"/></svg>Trajectories &rarr;</a></div>
<div class="chart-wrap"><div id="chart" style="height:460px"></div></div>
<div class="table-wrap"><table id="t"><thead><tr><th>#</th><th class="run-col">Run</th><th class="n">Passed</th><th class="n">Failed</th><th class="n">Error</th><th class="n">Final score</th><th class="n">Avg tokens/task</th><th class="n">Avg time/task</th><th class="n">Score / s</th><th class="n">Score / 1k tok</th><th>% of max</th></tr></thead>
<tbody>
{chr(10).join(body)}
</tbody></table></div>
<script src="plotly-cartesian-4.3.0.min.js"></script>
<script>
const tb=document.querySelector('tbody'),ths=[...document.querySelectorAll('th')];
const gd=document.getElementById('chart');
let last=-1,dir=1,cur=null;
// One 16px logo under each bar, between the axis line and the tick labels
// (ticklabelstandoff reserves that band; the vendored cartesian build has
// no tickpadding). Plot-area bottom is _size.t+_size.h. Re-run on every
// plotly render -- initial, table-sort redraws, responsive resizes -- so
// the icons track the bars; fixedrange keeps the axes static meanwhile.
function placeIcons(){{
  const L=gd._fullLayout;
  gd.querySelectorAll('.bar-icon').forEach(e=>e.remove());
  if(!L||!L._size||!cur)return;
  const step=L._size.w/cur.length;
  cur.forEach((d,i)=>{{
    const el=document.createElement('div');
    el.className='bar-icon';
    el.style.left=(L._size.l+(i+.5)*step-8)+'px';
    el.style.top=(L._size.t+L._size.h+3)+'px';
    const img=document.createElement('img');
    img.src='assets/'+d[11];img.width=16;img.height=16;img.alt='';
    el.appendChild(img);
    gd.appendChild(el);
  }});
}}
// One <a> link per bar covering the bar and its rotated tick label band, so
// clicking either the bar or the label opens that run's results page. The
// label band is the x-axis bottom margin (expanded by automargin for the
// rotated labels). d[12] is the run's results filename from data-s.
function placeLinks(){{
  const L=gd._fullLayout;
  gd.querySelectorAll('.bar-link').forEach(e=>e.remove());
  if(!L||!L._size||!cur)return;
  const step=L._size.w/cur.length;
  const band=L._size.b||0;
  cur.forEach((d,i)=>{{
    const a=document.createElement('a');
    a.className='bar-link';a.href=d[12];a.title=d[1];
    a.style.left=(L._size.l+(i+.5)*step-step/2)+'px';
    a.style.width=step+'px';
    a.style.top=(L._size.t)+'px';
    a.style.height=(L._size.h+band)+'px';
    gd.appendChild(a);
  }});
}}
function draw(){{
  const rs=[...tb.rows].map(r=>JSON.parse(r.dataset.s));
  cur=rs;
  const sc=rs.map(d=>d[5]),lo=Math.min(...sc),hi=Math.max(...sc);
  // gd.on only exists after the first render, so subscribe there (once);
  // plotly_afterplot then covers sort redraws and responsive resizes.
  Plotly.react('chart',[{{type:'bar',x:rs.map((_,i)=>i),y:rs.map(d=>d[5]),
    marker:{{color:'#3fb950'}},text:rs.map(d=>d[5].toFixed(1)),textposition:'outside',cliponaxis:false,
    customdata:rs.map(d=>d[1]),
    hovertemplate:'%{{customdata}}<br>score %{{y:.2f}}<extra></extra>'}}],
    {{paper_bgcolor:'#0d1117',plot_bgcolor:'#0d1117',font:{{color:'#e6edf3'}},
     margin:{{t:30,l:50,r:10,b:10}},
     xaxis:{{tickangle:-90,automargin:true,type:'linear',tickvals:rs.map((_,i)=>i),
       ticktext:rs.map(d=>d[1]),range:[-0.5,rs.length-0.5],ticklabelstandoff:18,fixedrange:true}},
     yaxis:{{title:'Final score',gridcolor:'#21262d',range:[lo,hi+(hi-lo)*.08],fixedrange:true}}}},
    {{displayModeBar:false,responsive:true}})
    .then(()=>{{
      if(!gd._iconsSub){{gd._iconsSub=true;gd.on('plotly_afterplot',()=>{{placeIcons();placeLinks();}});}}
      placeIcons();placeLinks();
    }});
}}
draw();
const nat=new Intl.Collator(undefined,{{numeric:true,sensitivity:'base'}});
ths.forEach((th,i)=>th.addEventListener('click',()=>{{
  dir=(last===i)?-dir:(i===1?1:-1);last=i;
  const rs=[...tb.rows].map(r=>({{r,d:JSON.parse(r.dataset.s)}}));
  rs.sort((a,b)=>{{const x=a.d[i],y=b.d[i];return (typeof x==='string'?nat.compare(x,y):x-y)*dir}});
  rs.forEach(o=>tb.appendChild(o.r));
  draw();
}}));
</script></body></html>"""


def render_trajectories(rows: list, reports: dict) -> str:
    """Render the trajectories page for the runs listed in the index."""
    runs = [{"label": r["title"] or r["run_id"], "sub": r["subtitle"] or r["model"],
             "score": r["score"], "report": reports[r["run_id"]]} for r in rows]
    return trajectories.render_html(trajectories.build_series(runs),
                                    index_href=INDEX_FILE)


def ensure_plotly(summary_dir: str) -> None:
    """Copy the vendored plotly bundle into a custom --summary-dir; warn if absent."""
    target = os.path.join(summary_dir, trajectories.PLOTLY_BUNDLE)
    source = os.path.join(HERE, trajectories.PLOTLY_BUNDLE)
    if os.path.isfile(target):
        return
    if os.path.isfile(source):
        shutil.copyfile(source, target)
        print(f"copied {trajectories.PLOTLY_BUNDLE} into {summary_dir}")
    else:
        print(f"warning: {trajectories.PLOTLY_BUNDLE} not found; "
              f"{TRAJECTORIES_FILE} will not render", file=sys.stderr)


def ensure_assets(summary_dir: str) -> None:
    """Copy missing bundled logos into a custom summary directory."""
    source = Path(HERE) / "assets"
    target = Path(summary_dir) / "assets"
    if source.resolve() == target.resolve():
        return
    target.mkdir(parents=True, exist_ok=True)
    for asset in source.iterdir():
        if asset.is_file() and not (target / asset.name).exists():
            shutil.copyfile(asset, target / asset.name)


def main() -> int:
    """Update the summary artifacts and index from the runs directory."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs-dir", default=os.path.join(ROOT, "runs"))
    ap.add_argument("--summary-dir", default=HERE)
    ap.add_argument("--dry-run", action="store_true", help="show changes without writing")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="accept every new or updated run")
    ap.add_argument("--no-redact", action="store_true",
                    help="keep the prompt, logs, and task links (do not redact)")
    args = ap.parse_args()
    # Redaction is the default; --no-redact opts out and keeps the sensitive parts.
    redact = not args.no_redact

    if not os.path.isdir(args.runs_dir):
        print(f"runs directory not found: {args.runs_dir}", file=sys.stderr)
        return 2
    if not args.dry_run:
        os.makedirs(args.summary_dir, exist_ok=True)

    runs = discover_runs(args.runs_dir)
    if not runs:
        print(f"no runs with report.html/report.json under {args.runs_dir}")
        return 1
    reports, digests = {}, {}
    for run_id, run_dir in runs:
        data, raw = load_report(run_dir)
        # Read the title separately because it may have changed since the report.
        model = (data.get("model") or {}).get("name") or "unknown"
        title, title_bytes = title_for_summary(run_dir, run_id, model)
        if title:
            data["title"] = title
        reports[run_id] = data
        digests[run_id] = hashlib.sha256(raw + title_bytes).hexdigest()

    mapping = load_map(args.summary_dir)
    if not os.path.exists(os.path.join(args.summary_dir, MAP_FILE)):
        seed_mapping(runs, reports, mapping, args.summary_dir)
    taken = {v["file"] for v in mapping.values()} | existing_artifacts(args.summary_dir)

    mode = "yes" if args.yes else ("ask" if sys.stdin.isatty() else "skip")
    if mode == "skip":
        print("non-interactive session: pass --yes to accept new or updated runs")

    rendered = 0
    index_rows = []
    skipped = []

    for run_id, run_dir in runs:
        totals = read_totals(reports[run_id], run_id)
        entry = mapping.get(run_id)
        name = entry["file"] if entry else None
        # A missing stored page is treated as stale even when the map still
        # matches, so deleting artifacts restores them on the next pass.
        up_to_date = (entry is not None and entry["digest"] == digests[run_id]
                      and entry["redacted"] == redact
                      and os.path.isfile(os.path.join(args.summary_dir, entry["file"])))
        if up_to_date:
            totals["artifact"] = entry["file"]
            index_rows.append(totals)
            continue

        state = "updated" if name else "new"
        if not name:
            name = unique_name(propose_filename(totals["model"], run_id), taken)

        chosen = confirm_run(run_id, state, name, mode)
        while chosen is not None and chosen in taken and chosen != name:
            print(f"  {chosen} is already used by another run")
            chosen = ask_name(f"  choose another filename for {run_id}")

        if chosen is None:
            skipped.append(run_id)
            print(f"  skipped; {run_id} not added to the summary")
            # A declined update stays listed in the index; its row keeps
            # linking the previous stored page.
            if state == "updated":
                totals["artifact"] = name
                index_rows.append(totals)
            continue

        name = chosen
        totals["artifact"] = name
        previous = entry["file"] if entry else None
        if previous and previous != name:
            old = os.path.join(args.summary_dir, previous)
            if os.path.isfile(old):
                if args.dry_run:
                    print(f"  dry run: would remove stale artifact {previous}")
                else:
                    os.remove(old)
                    print(f"  removed stale artifact {previous}")

        if args.dry_run:
            print(f"  dry run: would re-render {totals_path(run_dir)} -> {name}")
        else:
            page = report.render_html(reports[run_id], Path(run_dir),
                                      link_base=f"../runs/{run_id}/",
                                      redact=redact)
            report.atomic_write(Path(args.summary_dir) / name, page)
            print(f"  wrote {name}")

        mapping[run_id] = {"file": name, "digest": digests[run_id],
                           "redacted": redact}
        taken.add(name)
        index_rows.append(totals)
        rendered += 1

    if args.dry_run:
        print(f"\ndry run: {rendered} run(s) would change the summary; "
              f"{INDEX_FILE} and {TRAJECTORIES_FILE} not rewritten")
    else:
        # Render both pages before writing anything, so a failure leaves the
        # map, the index and the trajectories page consistent with each other.
        index_page = render_index(index_rows)
        trajectories_page = render_trajectories(index_rows, reports)
        save_map(args.summary_dir, mapping)
        report.atomic_write(Path(args.summary_dir) / INDEX_FILE, index_page)
        ensure_plotly(args.summary_dir)
        ensure_assets(args.summary_dir)
        report.atomic_write(Path(args.summary_dir) / TRAJECTORIES_FILE, trajectories_page)
        print(f"\nrendered {rendered} run report(s); rewrote {INDEX_FILE} with "
              f"{len(index_rows)} rows and {TRAJECTORIES_FILE}")

    if skipped:
        print("skipped: " + ", ".join(skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
