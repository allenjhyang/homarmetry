#!/usr/bin/env python3
"""
OpenClaw Dashboard — See your agent think 🦞

Real-time observability dashboard for OpenClaw/Moltbot AI agents.
Single-file Flask app with zero config — auto-detects your setup.

Usage:
    openclaw-dashboard                    # Auto-detect everything
    openclaw-dashboard --port 9000        # Custom port
    openclaw-dashboard --workspace ~/bot  # Custom workspace
    OPENCLAW_HOME=~/bot openclaw-dashboard

https://github.com/vivekchand/openclaw-dashboard
MIT License — Built by Vivek Chand
"""

import os
import sys
import glob
import json
import socket
import argparse
import subprocess
import time
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template_string, request, jsonify, Response, make_response

# Optional: OpenTelemetry protobuf support for OTLP receiver
_HAS_OTEL_PROTO = False
try:
    from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
    from opentelemetry.proto.collector.traces.v1 import trace_service_pb2
    _HAS_OTEL_PROTO = True
except ImportError:
    metrics_service_pb2 = None
    trace_service_pb2 = None

__version__ = "0.2.4"

app = Flask(__name__)

# ── Configuration (auto-detected, overridable via CLI/env) ──────────────
MC_URL = os.environ.get("MC_URL", "http://localhost:3002")
WORKSPACE = None
MEMORY_DIR = None
LOG_DIR = None
SESSIONS_DIR = None
USER_NAME = None
CET = timezone(timedelta(hours=1))

# ── OTLP Metrics Store ─────────────────────────────────────────────────
METRICS_FILE = None  # Set via CLI/env, defaults to {WORKSPACE}/.openclaw-dashboard-metrics.json
_metrics_lock = threading.Lock()
_otel_last_received = 0  # timestamp of last OTLP data received

metrics_store = {
    "tokens": [],       # [{timestamp, input, output, total, model, channel, provider}]
    "cost": [],         # [{timestamp, usd, model, channel, provider}]
    "runs": [],         # [{timestamp, duration_ms, model, channel}]
    "messages": [],     # [{timestamp, channel, outcome, duration_ms}]
    "webhooks": [],     # [{timestamp, channel, type}]
}
MAX_STORE_ENTRIES = 10_000
STORE_RETENTION_DAYS = 14


def _metrics_file_path():
    """Get the path to the metrics persistence file."""
    if METRICS_FILE:
        return METRICS_FILE
    if WORKSPACE:
        return os.path.join(WORKSPACE, '.openclaw-dashboard-metrics.json')
    return os.path.expanduser('~/.openclaw-dashboard-metrics.json')


def _load_metrics_from_disk():
    """Load persisted metrics on startup."""
    global metrics_store, _otel_last_received
    path = _metrics_file_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in metrics_store:
                if key in data and isinstance(data[key], list):
                    metrics_store[key] = data[key][-MAX_STORE_ENTRIES:]
            _otel_last_received = data.get('_last_received', 0)
        _expire_old_entries()
    except Exception:
        pass


def _save_metrics_to_disk():
    """Persist metrics store to JSON file."""
    path = _metrics_file_path()
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        data = {}
        with _metrics_lock:
            for k in metrics_store:
                data[k] = list(metrics_store[k])
        data['_last_received'] = _otel_last_received
        data['_saved_at'] = time.time()
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _expire_old_entries():
    """Remove entries older than STORE_RETENTION_DAYS."""
    cutoff = time.time() - (STORE_RETENTION_DAYS * 86400)
    with _metrics_lock:
        for key in metrics_store:
            metrics_store[key] = [
                e for e in metrics_store[key]
                if e.get('timestamp', 0) > cutoff
            ][-MAX_STORE_ENTRIES:]


def _add_metric(category, entry):
    """Add an entry to the metrics store (thread-safe)."""
    global _otel_last_received
    with _metrics_lock:
        metrics_store[category].append(entry)
        if len(metrics_store[category]) > MAX_STORE_ENTRIES:
            metrics_store[category] = metrics_store[category][-MAX_STORE_ENTRIES:]
        _otel_last_received = time.time()


def _metrics_flush_loop():
    """Background thread: save metrics to disk every 60 seconds."""
    while True:
        time.sleep(60)
        try:
            _expire_old_entries()
            _save_metrics_to_disk()
        except Exception:
            pass


def _start_metrics_flush_thread():
    """Start the background metrics flush thread."""
    t = threading.Thread(target=_metrics_flush_loop, daemon=True)
    t.start()


def _has_otel_data():
    """Check if we have any OTLP metrics data."""
    return any(len(metrics_store[k]) > 0 for k in metrics_store)


# ── OTLP Protobuf Helpers ──────────────────────────────────────────────

def _otel_attr_value(val):
    """Convert an OTel AnyValue to a Python value."""
    if val.HasField('string_value'):
        return val.string_value
    if val.HasField('int_value'):
        return val.int_value
    if val.HasField('double_value'):
        return val.double_value
    if val.HasField('bool_value'):
        return val.bool_value
    return str(val)


def _get_data_points(metric):
    """Extract data points from a metric regardless of type."""
    if metric.HasField('sum'):
        return metric.sum.data_points
    elif metric.HasField('gauge'):
        return metric.gauge.data_points
    elif metric.HasField('histogram'):
        return metric.histogram.data_points
    elif metric.HasField('summary'):
        return metric.summary.data_points
    return []


def _get_dp_value(dp):
    """Extract the numeric value from a data point."""
    if hasattr(dp, 'as_double') and dp.as_double:
        return dp.as_double
    if hasattr(dp, 'as_int') and dp.as_int:
        return dp.as_int
    if hasattr(dp, 'sum') and dp.sum:
        return dp.sum
    if hasattr(dp, 'count') and dp.count:
        return dp.count
    return 0


def _get_dp_attrs(dp):
    """Extract attributes from a data point."""
    attrs = {}
    for attr in dp.attributes:
        attrs[attr.key] = _otel_attr_value(attr.value)
    return attrs


def _process_otlp_metrics(pb_data):
    """Decode OTLP metrics protobuf and store relevant data."""
    req = metrics_service_pb2.ExportMetricsServiceRequest()
    req.ParseFromString(pb_data)

    for resource_metrics in req.resource_metrics:
        resource_attrs = {}
        if resource_metrics.resource:
            for attr in resource_metrics.resource.attributes:
                resource_attrs[attr.key] = _otel_attr_value(attr.value)

        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                name = metric.name
                ts = time.time()

                if name == 'openclaw.tokens':
                    for dp in _get_data_points(metric):
                        attrs = _get_dp_attrs(dp)
                        _add_metric('tokens', {
                            'timestamp': ts,
                            'input': attrs.get('input_tokens', 0),
                            'output': attrs.get('output_tokens', 0),
                            'total': _get_dp_value(dp),
                            'model': attrs.get('model', resource_attrs.get('model', '')),
                            'channel': attrs.get('channel', resource_attrs.get('channel', '')),
                            'provider': attrs.get('provider', resource_attrs.get('provider', '')),
                        })
                elif name == 'openclaw.cost.usd':
                    for dp in _get_data_points(metric):
                        attrs = _get_dp_attrs(dp)
                        _add_metric('cost', {
                            'timestamp': ts,
                            'usd': _get_dp_value(dp),
                            'model': attrs.get('model', resource_attrs.get('model', '')),
                            'channel': attrs.get('channel', resource_attrs.get('channel', '')),
                            'provider': attrs.get('provider', resource_attrs.get('provider', '')),
                        })
                elif name == 'openclaw.run.duration_ms':
                    for dp in _get_data_points(metric):
                        attrs = _get_dp_attrs(dp)
                        _add_metric('runs', {
                            'timestamp': ts,
                            'duration_ms': _get_dp_value(dp),
                            'model': attrs.get('model', resource_attrs.get('model', '')),
                            'channel': attrs.get('channel', resource_attrs.get('channel', '')),
                        })
                elif name == 'openclaw.context.tokens':
                    for dp in _get_data_points(metric):
                        attrs = _get_dp_attrs(dp)
                        _add_metric('tokens', {
                            'timestamp': ts,
                            'input': _get_dp_value(dp),
                            'output': 0,
                            'total': _get_dp_value(dp),
                            'model': attrs.get('model', resource_attrs.get('model', '')),
                            'channel': attrs.get('channel', resource_attrs.get('channel', '')),
                            'provider': attrs.get('provider', resource_attrs.get('provider', '')),
                        })
                elif name in ('openclaw.message.processed', 'openclaw.message.queued', 'openclaw.message.duration_ms'):
                    for dp in _get_data_points(metric):
                        attrs = _get_dp_attrs(dp)
                        outcome = 'processed' if 'processed' in name else ('queued' if 'queued' in name else 'duration')
                        _add_metric('messages', {
                            'timestamp': ts,
                            'channel': attrs.get('channel', resource_attrs.get('channel', '')),
                            'outcome': outcome,
                            'duration_ms': _get_dp_value(dp) if 'duration' in name else 0,
                        })
                elif name in ('openclaw.webhook.received', 'openclaw.webhook.error', 'openclaw.webhook.duration_ms'):
                    for dp in _get_data_points(metric):
                        attrs = _get_dp_attrs(dp)
                        wtype = 'received' if 'received' in name else ('error' if 'error' in name else 'duration')
                        _add_metric('webhooks', {
                            'timestamp': ts,
                            'channel': attrs.get('channel', resource_attrs.get('channel', '')),
                            'type': wtype,
                        })


def _process_otlp_traces(pb_data):
    """Decode OTLP traces protobuf and extract relevant span data."""
    req = trace_service_pb2.ExportTraceServiceRequest()
    req.ParseFromString(pb_data)

    for resource_spans in req.resource_spans:
        resource_attrs = {}
        if resource_spans.resource:
            for attr in resource_spans.resource.attributes:
                resource_attrs[attr.key] = _otel_attr_value(attr.value)

        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                attrs = {}
                for attr in span.attributes:
                    attrs[attr.key] = _otel_attr_value(attr.value)

                ts = time.time()
                duration_ns = span.end_time_unix_nano - span.start_time_unix_nano
                duration_ms = duration_ns / 1_000_000

                span_name = span.name.lower()
                if 'run' in span_name or 'completion' in span_name:
                    _add_metric('runs', {
                        'timestamp': ts,
                        'duration_ms': duration_ms,
                        'model': attrs.get('model', resource_attrs.get('model', '')),
                        'channel': attrs.get('channel', resource_attrs.get('channel', '')),
                    })
                elif 'message' in span_name:
                    _add_metric('messages', {
                        'timestamp': ts,
                        'channel': attrs.get('channel', resource_attrs.get('channel', '')),
                        'outcome': 'processed',
                        'duration_ms': duration_ms,
                    })


def _get_otel_usage_data():
    """Aggregate OTLP metrics into usage data for the Usage tab."""
    today = datetime.now()
    today_start = today.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    week_start = (today - timedelta(days=today.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()

    daily_tokens = {}
    daily_cost = {}
    model_usage = {}

    with _metrics_lock:
        for entry in metrics_store['tokens']:
            ts = entry.get('timestamp', 0)
            day = datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
            total = entry.get('total', 0)
            daily_tokens[day] = daily_tokens.get(day, 0) + total
            model = entry.get('model', 'unknown') or 'unknown'
            model_usage[model] = model_usage.get(model, 0) + total

        for entry in metrics_store['cost']:
            ts = entry.get('timestamp', 0)
            day = datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
            daily_cost[day] = daily_cost.get(day, 0) + entry.get('usd', 0)

    days = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        ds = d.strftime('%Y-%m-%d')
        days.append({
            'date': ds,
            'tokens': daily_tokens.get(ds, 0),
            'cost': daily_cost.get(ds, 0),
        })

    today_str = today.strftime('%Y-%m-%d')
    today_tok = daily_tokens.get(today_str, 0)
    week_tok = sum(v for k, v in daily_tokens.items()
                   if _safe_date_ts(k) >= week_start)
    month_tok = sum(v for k, v in daily_tokens.items()
                    if _safe_date_ts(k) >= month_start)
    today_cost_val = daily_cost.get(today_str, 0)
    week_cost_val = sum(v for k, v in daily_cost.items()
                        if _safe_date_ts(k) >= week_start)
    month_cost_val = sum(v for k, v in daily_cost.items()
                         if _safe_date_ts(k) >= month_start)

    run_durations = []
    with _metrics_lock:
        for entry in metrics_store['runs']:
            run_durations.append(entry.get('duration_ms', 0))
    avg_run_ms = sum(run_durations) / len(run_durations) if run_durations else 0

    msg_count = len(metrics_store['messages'])

    # Enhanced cost tracking for OTLP data
    trend_data = _analyze_usage_trends(daily_tokens) 
    warnings = _generate_cost_warnings(today_cost_val, week_cost_val, month_cost_val, trend_data)

    return {
        'source': 'otlp',
        'days': days,
        'today': today_tok,
        'week': week_tok,
        'month': month_tok,
        'todayCost': round(today_cost_val, 4),
        'weekCost': round(week_cost_val, 4),
        'monthCost': round(month_cost_val, 4),
        'avgRunMs': round(avg_run_ms, 1),
        'messageCount': msg_count,
        'modelBreakdown': [
            {'model': k, 'tokens': v}
            for k, v in sorted(model_usage.items(), key=lambda x: -x[1])
        ],
        'trend': trend_data,
        'warnings': warnings,
    }


def _safe_date_ts(date_str):
    """Parse a YYYY-MM-DD date string to a timestamp, returning 0 on failure."""
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').timestamp()
    except Exception:
        return 0


def detect_config(args=None):
    """Auto-detect OpenClaw/Moltbot paths, with CLI and env overrides."""
    global WORKSPACE, MEMORY_DIR, LOG_DIR, SESSIONS_DIR, USER_NAME

    # 1. Workspace — where agent files live (SOUL.md, MEMORY.md, memory/, etc.)
    if args and args.workspace:
        WORKSPACE = os.path.expanduser(args.workspace)
    elif os.environ.get("OPENCLAW_HOME"):
        WORKSPACE = os.path.expanduser(os.environ["OPENCLAW_HOME"])
    elif os.environ.get("OPENCLAW_WORKSPACE"):
        WORKSPACE = os.path.expanduser(os.environ["OPENCLAW_WORKSPACE"])
    else:
        # Auto-detect: check common locations
        candidates = [
            _detect_workspace_from_config(),
            os.path.expanduser("~/.clawdbot/workspace"),
            os.path.expanduser("~/clawd"),
            os.path.expanduser("~/openclaw"),
            os.getcwd(),
        ]
        for c in candidates:
            if c and os.path.isdir(c) and (
                os.path.exists(os.path.join(c, "SOUL.md")) or
                os.path.exists(os.path.join(c, "AGENTS.md")) or
                os.path.exists(os.path.join(c, "MEMORY.md")) or
                os.path.isdir(os.path.join(c, "memory"))
            ):
                WORKSPACE = c
                break
        if not WORKSPACE:
            WORKSPACE = os.getcwd()

    MEMORY_DIR = os.path.join(WORKSPACE, "memory")

    # 2. Log directory
    if args and args.log_dir:
        LOG_DIR = os.path.expanduser(args.log_dir)
    elif os.environ.get("OPENCLAW_LOG_DIR"):
        LOG_DIR = os.path.expanduser(os.environ["OPENCLAW_LOG_DIR"])
    else:
        candidates = ["/tmp/moltbot", "/tmp/openclaw", os.path.expanduser("~/.clawdbot/logs")]
        LOG_DIR = next((d for d in candidates if os.path.isdir(d)), "/tmp/openclaw")

    # 3. Sessions directory (transcript .jsonl files)
    if args and getattr(args, 'sessions_dir', None):
        SESSIONS_DIR = os.path.expanduser(args.sessions_dir)
    elif os.environ.get("OPENCLAW_SESSIONS_DIR"):
        SESSIONS_DIR = os.path.expanduser(os.environ["OPENCLAW_SESSIONS_DIR"])
    else:
        candidates = [
            os.path.expanduser('~/.clawdbot/agents/main/sessions'),
            os.path.join(WORKSPACE, 'sessions') if WORKSPACE else None,
            os.path.expanduser('~/.clawdbot/sessions'),
        ]
        # Also scan ~/.clawdbot/agents/*/sessions/
        agents_base = os.path.expanduser('~/.clawdbot/agents')
        if os.path.isdir(agents_base):
            for agent in os.listdir(agents_base):
                p = os.path.join(agents_base, agent, 'sessions')
                if p not in candidates:
                    candidates.append(p)
        SESSIONS_DIR = next((d for d in candidates if d and os.path.isdir(d)), candidates[0] if candidates else None)

    # 4. User name (shown in Flow visualization)
    if args and args.name:
        USER_NAME = args.name
    elif os.environ.get("OPENCLAW_USER"):
        USER_NAME = os.environ["OPENCLAW_USER"]
    else:
        USER_NAME = "You"


def _detect_workspace_from_config():
    """Try to read workspace from Moltbot/OpenClaw agent config."""
    config_paths = [
        os.path.expanduser("~/.clawdbot/agents/main/config.json"),
        os.path.expanduser("~/.clawdbot/config.json"),
    ]
    for cp in config_paths:
        try:
            with open(cp) as f:
                data = json.load(f)
                ws = data.get("workspace") or data.get("workspaceDir")
                if ws:
                    return os.path.expanduser(ws)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass
    return None


def get_local_ip():
    """Get the machine's LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── HTML Template ───────────────────────────────────────────────────────

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenClaw Dashboard 🦞</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    /* Light theme (default) — premium polish */
    --bg-primary: #ffffff;
    --bg-secondary: #f8f9fa;
    --bg-tertiary: #ffffff;
    --bg-hover: #f1f5f9;
    --bg-accent: #2563eb;
    --border-primary: rgba(0,0,0,0.06);
    --border-secondary: rgba(0,0,0,0.03);
    --text-primary: #1a1a2e;
    --text-secondary: #475569;
    --text-tertiary: #64748b;
    --text-muted: #94a3b8;
    --text-faint: #cbd5e1;
    --text-accent: #2563eb;
    --text-link: #2563eb;
    --text-success: #16a34a;
    --text-warning: #d97706;
    --text-error: #dc2626;
    --bg-success: #f0fdf4;
    --bg-warning: #fffbeb;
    --bg-error: #fef2f2;
    --log-bg: #f8f9fa;
    --file-viewer-bg: #ffffff;
    --button-bg: #f1f5f9;
    --button-hover: #e2e8f0;
    --card-shadow: 0 1px 3px rgba(0,0,0,0.1);
    --card-shadow-hover: 0 4px 16px rgba(0,0,0,0.12), 0 2px 4px rgba(0,0,0,0.06);
  }

  [data-theme="dark"] {
    /* Dark theme */
    --bg-primary: #0a0a14;
    --bg-secondary: #12122a;
    --bg-tertiary: #141428;
    --bg-hover: #1a1a35;
    --bg-accent: #3b82f6;
    --border-primary: #2a2a4a;
    --border-secondary: #1a1a30;
    --text-primary: #e0e0e0;
    --text-secondary: #ccc;
    --text-tertiary: #888;
    --text-muted: #666;
    --text-faint: #555;
    --text-accent: #3b82f6;
    --text-link: #60a0ff;
    --text-success: #27ae60;
    --text-warning: #f0c040;
    --text-error: #e74c3c;
    --bg-success: #1a3a2a;
    --bg-warning: #2a2a1a;
    --bg-error: #3a1a1a;
    --log-bg: #0a0a14;
    --file-viewer-bg: #0d0d1a;
    --button-bg: #2a2a4a;
    --button-hover: #3a3a5a;
    --card-shadow: 0 1px 3px rgba(0,0,0,0.3);
    --card-shadow-hover: 0 4px 12px rgba(0,0,0,0.4);
  }

  * { box-sizing: border-box; margin: 0; padding: 0; transition: background-color 0.3s ease, color 0.3s ease, border-color 0.3s ease, box-shadow 0.3s ease; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', Roboto, sans-serif; background: var(--bg-primary); color: var(--text-primary); min-height: 100vh; font-size: 14px; font-weight: 400; line-height: 1.5; -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }

  .nav { background: var(--bg-secondary); border-bottom: none; padding: 6px 16px; display: flex; align-items: center; gap: 12px; overflow-x: auto; -webkit-overflow-scrolling: touch; box-shadow: 0 1px 3px rgba(0,0,0,0.06); position: relative; z-index: 10; }
  .nav h1 { font-size: 18px; font-weight: 700; color: var(--text-primary); white-space: nowrap; letter-spacing: -0.3px; }
  .nav h1 span { color: var(--text-accent); }
  .theme-toggle { background: var(--button-bg); border: none; border-radius: 8px; padding: 8px 12px; color: var(--text-tertiary); cursor: pointer; font-size: 16px; margin-left: 12px; transition: all 0.15s; box-shadow: var(--card-shadow); }
  .theme-toggle:hover { background: var(--button-hover); color: var(--text-secondary); }
  .theme-toggle:active { transform: scale(0.98); }
  
  /* === Zoom Controls === */
  .zoom-controls { display: flex; align-items: center; gap: 4px; margin-left: 12px; }
  .zoom-btn { background: var(--button-bg); border: 1px solid var(--border-primary); border-radius: 6px; width: 28px; height: 28px; color: var(--text-tertiary); cursor: pointer; font-size: 16px; font-weight: 700; display: flex; align-items: center; justify-content: center; transition: all 0.15s; }
  .zoom-btn:hover { background: var(--button-hover); color: var(--text-secondary); }
  .zoom-level { font-size: 11px; color: var(--text-muted); font-weight: 600; min-width: 36px; text-align: center; }
  .nav-tabs { display: flex; gap: 4px; margin-left: auto; }
  .nav-tab { padding: 8px 16px; border-radius: 8px; background: transparent; border: 1px solid transparent; color: var(--text-tertiary); cursor: pointer; font-size: 13px; font-weight: 600; white-space: nowrap; transition: all 0.2s ease; position: relative; }
  .nav-tab:hover { background: var(--bg-hover); color: var(--text-secondary); }
  .nav-tab.active { background: var(--bg-accent); color: #ffffff; border-color: var(--bg-accent); }
  .nav-tab:active { transform: scale(0.98); }

  .page { display: none; padding: 16px 20px; max-width: 1200px; margin: 0 auto; }
  #page-overview { max-width: 1600px; padding: 8px 12px; }
  .page.active { display: block; }

  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin-bottom: 16px; }
  .card { background: var(--bg-tertiary); border: 1px solid var(--border-primary); border-radius: 12px; padding: 20px; box-shadow: var(--card-shadow); transition: transform 0.2s ease, box-shadow 0.2s ease; }
  .card-title { font-size: 12px; text-transform: uppercase; color: var(--text-muted); letter-spacing: 1px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
  .card-title .icon { font-size: 16px; }
  .card-value { font-size: 32px; font-weight: 700; color: var(--text-primary); letter-spacing: -0.5px; }
  .card-sub { font-size: 12px; color: var(--text-faint); margin-top: 4px; }

  .stat-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--border-secondary); }
  .stat-row:last-child { border-bottom: none; }
  .stat-label { color: var(--text-tertiary); font-size: 13px; }
  .stat-val { color: var(--text-primary); font-size: 13px; font-weight: 600; }
  .stat-val.green { color: var(--text-success); }
  .stat-val.yellow { color: var(--text-warning); }
  .stat-val.red { color: var(--text-error); }

  .session-item { padding: 12px; border-bottom: 1px solid var(--border-secondary); }
  .session-item:last-child { border-bottom: none; }
  .session-name { font-weight: 600; font-size: 14px; color: var(--text-primary); }
  .session-meta { font-size: 12px; color: var(--text-muted); margin-top: 4px; display: flex; gap: 12px; flex-wrap: wrap; }
  .session-meta span { display: flex; align-items: center; gap: 4px; }

  .cron-item { padding: 12px; border-bottom: 1px solid var(--border-secondary); }
  .cron-item:last-child { border-bottom: none; }
  .cron-name { font-weight: 600; font-size: 14px; color: var(--text-primary); }
  .cron-schedule { font-size: 12px; color: var(--text-accent); margin-top: 2px; font-family: 'SF Mono', 'Fira Code', monospace; }
  .cron-meta { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
  .cron-status { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .cron-status.ok { background: var(--bg-success); color: var(--text-success); }
  .cron-status.error { background: var(--bg-error); color: var(--text-error); }
  .cron-status.pending { background: var(--bg-warning); color: var(--text-warning); }

  .log-viewer { background: var(--log-bg); border: 1px solid var(--border-primary); border-radius: 8px; font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace; font-size: 12px; line-height: 1.6; padding: 12px; max-height: 500px; overflow-y: auto; -webkit-overflow-scrolling: touch; white-space: pre-wrap; word-break: break-all; }
  .log-line { padding: 1px 0; }
  .log-line .ts { color: var(--text-muted); }
  .log-line .info { color: var(--text-link); }
  .log-line .warn { color: var(--text-warning); }
  .log-line .err { color: var(--text-error); }
  .log-line .msg { color: var(--text-secondary); }

  .memory-item { padding: 10px 12px; border-bottom: 1px solid var(--border-secondary); display: flex; justify-content: space-between; align-items: center; cursor: pointer; transition: background 0.15s; }
  .memory-item:hover { background: var(--bg-hover); }
  .memory-item:last-child { border-bottom: none; }
  .file-viewer { background: var(--file-viewer-bg); border: 1px solid var(--border-primary); border-radius: 12px; padding: 16px; margin-top: 16px; display: none; }
  .file-viewer-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .file-viewer-title { font-size: 14px; font-weight: 600; color: var(--text-accent); }
  .file-viewer-close { background: var(--button-bg); border: none; color: var(--text-secondary); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .file-viewer-close:hover { background: var(--button-hover); }
  .file-viewer-content { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px; color: var(--text-secondary); white-space: pre-wrap; word-break: break-word; max-height: 60vh; overflow-y: auto; line-height: 1.5; }
  .memory-name { font-weight: 600; font-size: 14px; color: var(--text-link); cursor: pointer; }
  .memory-name:hover { text-decoration: underline; }
  .memory-size { font-size: 12px; color: var(--text-faint); }

  .refresh-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  .refresh-btn { padding: 8px 16px; background: var(--button-bg); border: 1px solid var(--border-primary); border-radius: 8px; color: var(--text-primary); cursor: pointer; font-size: 13px; font-weight: 500; transition: all 0.15s ease; }
  .refresh-btn:hover { background: var(--button-hover); }
  .refresh-btn:active { transform: scale(0.98); }
  .refresh-time { font-size: 12px; color: var(--text-muted); }
  .pulse { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #16a34a; animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; box-shadow: 0 0 4px #16a34a; } 50% { opacity: 0.3; box-shadow: none; } }
  .live-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; background: var(--bg-success); color: var(--text-success); font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; animation: pulse 1.5s infinite; }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .badge.model { background: var(--bg-hover); color: var(--text-accent); }
  .badge.channel { background: var(--bg-hover); color: #7c3aed; }
  .badge.tokens { background: var(--bg-success); color: var(--text-success); }

  .full-width { grid-column: 1 / -1; }
  .section-title { font-size: 16px; font-weight: 700; color: var(--text-primary); margin: 24px 0 12px; display: flex; align-items: center; gap: 8px; }

  /* === Flow Visualization === */
  .flow-container { width: 100%; overflow-x: auto; overflow-y: hidden; position: relative; -webkit-overflow-scrolling: touch; }
  .flow-stats { display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
  .flow-stat { background: var(--bg-tertiary); border: 1px solid var(--border-primary); border-radius: 8px; padding: 8px 14px; flex: 1; min-width: 100px; box-shadow: var(--card-shadow); }
  .flow-stat-label { font-size: 10px; text-transform: uppercase; color: var(--text-muted); letter-spacing: 1px; display: block; }
  .flow-stat-value { font-size: 20px; font-weight: 700; color: var(--text-primary); display: block; margin-top: 2px; }
  #flow-svg { width: 100%; min-width: 800px; height: auto; display: block; overflow: visible; }
  #flow-svg text { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 15px; font-weight: 700; text-anchor: middle; dominant-baseline: central; pointer-events: none; }
  .flow-node-channel text, .flow-node-gateway text, .flow-node-session text, .flow-node-tool text { fill: #ffffff !important; }
  .flow-node-infra > text { fill: #ffffff !important; }
  .flow-node rect { rx: 10; ry: 10; stroke-width: 1.5; transition: all 0.3s ease; }
  .flow-node-brain rect { stroke-width: 2.5; }
  @keyframes dashFlow { to { stroke-dashoffset: -24; } }
  .flow-path { stroke-dasharray: 8 4; animation: dashFlow 1.2s linear infinite; }
  .flow-path.flow-path-infra { stroke-dasharray: 6 3; animation: dashFlow 2s linear infinite; }
  .flow-node-channel.active rect { filter: drop-shadow(0 0 12px rgba(106,64,191,0.8)) drop-shadow(0 0 20px rgba(106,64,191,0.4)); stroke-width: 2.5; transform: scale(1.05); }
  .flow-node-gateway.active rect { filter: drop-shadow(0 0 12px rgba(64,128,224,0.8)) drop-shadow(0 0 20px rgba(64,128,224,0.4)); stroke-width: 2.5; transform: scale(1.05); }
  .flow-node-session.active rect { filter: drop-shadow(0 0 12px rgba(64,192,96,0.8)) drop-shadow(0 0 20px rgba(64,192,96,0.4)); stroke-width: 2.5; transform: scale(1.05); }
  .flow-node-tool.active rect { filter: drop-shadow(0 0 12px rgba(224,96,64,0.9)) drop-shadow(0 0 24px rgba(224,96,64,0.5)); stroke: #ff8050; stroke-width: 2.5; transform: scale(1.1); }
  .flow-path { fill: none; stroke: var(--text-muted); stroke-width: 2; stroke-linecap: round; transition: stroke 0.4s, opacity 0.4s; opacity: 0.6; }
  .flow-path.glow-blue { stroke: #4080e0; filter: drop-shadow(0 0 6px rgba(64,128,224,0.6)); }
  .flow-path.glow-yellow { stroke: #f0c040; filter: drop-shadow(0 0 6px rgba(240,192,64,0.6)); }
  .flow-path.glow-green { stroke: #50e080; filter: drop-shadow(0 0 6px rgba(80,224,128,0.6)); }
  .flow-path.glow-red { stroke: #e04040; filter: drop-shadow(0 0 6px rgba(224,64,64,0.6)); }
  @keyframes brainPulse { 0%,100% { filter: drop-shadow(0 0 6px rgba(240,192,64,0.25)); } 50% { filter: drop-shadow(0 0 22px rgba(240,192,64,0.7)); } }
  .brain-group { animation: brainPulse 2.2s ease-in-out infinite; }
  .tool-indicator { opacity: 0.2; transition: opacity 0.3s ease; }
  .tool-indicator.active { opacity: 1; }
  .flow-label { font-size: 9px !important; fill: var(--text-muted) !important; font-weight: 400 !important; }
  .flow-node-human circle { transition: all 0.3s ease; }
  .flow-node-human.active circle { filter: drop-shadow(0 0 12px rgba(176,128,255,0.7)); }
  @keyframes humanGlow { 0%,100% { filter: drop-shadow(0 0 3px rgba(160,112,224,0.15)); } 50% { filter: drop-shadow(0 0 10px rgba(160,112,224,0.45)); } }
  .flow-node-human { animation: humanGlow 3.5s ease-in-out infinite; }
  .flow-ground { stroke: var(--border-primary); stroke-width: 1; stroke-dasharray: 8 4; }
  .flow-ground-label { font-size: 10px !important; fill: var(--text-muted) !important; font-weight: 600 !important; letter-spacing: 4px; }
  .flow-node-infra rect { rx: 6; ry: 6; stroke-width: 2; stroke-dasharray: 5 2; transition: all 0.3s ease; }
  .flow-node-infra text { font-size: 12px !important; }
  .flow-node-infra .infra-sub { font-size: 9px !important; fill: var(--text-muted) !important; font-weight: 400 !important; }
  .flow-node-runtime rect { stroke: #4a7090; }
  .flow-node-machine rect { stroke: #606880; }
  .flow-node-storage rect { stroke: #806a30; }
  .flow-node-network rect { stroke: #308080; }
  [data-theme="dark"] .flow-node-runtime rect { fill: #10182a; }
  [data-theme="dark"] .flow-node-machine rect { fill: #141420; }
  [data-theme="dark"] .flow-node-storage rect { fill: #1a1810; }
  [data-theme="dark"] .flow-node-network rect { fill: #0e1c20; }
  .flow-node-runtime.active rect { filter: drop-shadow(0 0 10px rgba(74,112,144,0.7)); stroke-dasharray: none; stroke-width: 2.5; }
  .flow-node-machine.active rect { filter: drop-shadow(0 0 10px rgba(96,104,128,0.7)); stroke-dasharray: none; stroke-width: 2.5; }
  .flow-node-storage.active rect { filter: drop-shadow(0 0 10px rgba(128,106,48,0.7)); stroke-dasharray: none; stroke-width: 2.5; }
  .flow-node-network.active rect { filter: drop-shadow(0 0 10px rgba(48,128,128,0.7)); stroke-dasharray: none; stroke-width: 2.5; }
  .flow-path-infra { stroke-dasharray: 6 3; opacity: 0.3; }
  .flow-path.glow-cyan { stroke: #40a0b0; filter: drop-shadow(0 0 6px rgba(64,160,176,0.6)); stroke-dasharray: none; opacity: 1; }
  .flow-path.glow-purple { stroke: #b080ff; filter: drop-shadow(0 0 6px rgba(176,128,255,0.6)); }

  /* === Activity Heatmap === */
  .heatmap-wrap { overflow-x: auto; padding: 8px 0; }
  .heatmap-grid { display: grid; grid-template-columns: 60px repeat(24, 1fr); gap: 2px; min-width: 650px; }
  .heatmap-label { font-size: 11px; color: #666; display: flex; align-items: center; padding-right: 8px; justify-content: flex-end; }
  .heatmap-hour-label { font-size: 10px; color: #555; text-align: center; padding-bottom: 4px; }
  .heatmap-cell { aspect-ratio: 1; border-radius: 3px; min-height: 16px; transition: all 0.15s; cursor: default; position: relative; }
  .heatmap-cell:hover { transform: scale(1.3); z-index: 2; outline: 1px solid #f0c040; }
  .heatmap-cell[title]:hover::after { content: attr(title); position: absolute; bottom: 120%; left: 50%; transform: translateX(-50%); background: #222; color: #eee; padding: 3px 8px; border-radius: 4px; font-size: 10px; white-space: nowrap; z-index: 10; pointer-events: none; }
  .heatmap-legend { display: flex; align-items: center; gap: 6px; margin-top: 10px; font-size: 11px; color: #666; }
  .heatmap-legend-cell { width: 14px; height: 14px; border-radius: 3px; }

  /* === Health Checks === */
  .health-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
  .health-item { background: var(--bg-tertiary); border: 1px solid var(--border-primary); border-radius: 10px; padding: 14px 16px; display: flex; align-items: center; gap: 12px; transition: border-color 0.3s; box-shadow: var(--card-shadow); }
  .health-item.healthy { border-left: 3px solid #16a34a; }
  .health-item.warning { border-left: 3px solid #d97706; }
  .health-item.critical { border-left: 3px solid #dc2626; }
  .health-dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
  .health-dot.green { background: #16a34a; box-shadow: 0 0 8px rgba(22,163,74,0.5); }
  .health-dot.yellow { background: #d97706; box-shadow: 0 0 8px rgba(217,119,6,0.5); }
  .health-dot.red { background: #dc2626; box-shadow: 0 0 8px rgba(220,38,38,0.5); }
  .health-info { flex: 1; }
  .health-name { font-size: 13px; font-weight: 600; color: var(--text-primary); }
  .health-detail { font-size: 11px; color: var(--text-muted); margin-top: 2px; }

  /* === Usage/Token Charts === */
  .usage-chart { display: flex; align-items: flex-end; gap: 6px; height: 200px; padding: 16px 8px 32px; position: relative; }
  .usage-bar-wrap { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; position: relative; }
  .usage-bar { width: 100%; min-width: 20px; max-width: 48px; border-radius: 6px 6px 0 0; background: linear-gradient(180deg, var(--bg-accent), #1d4ed8); transition: height 0.4s ease; position: relative; cursor: default; }
  .usage-bar:hover { filter: brightness(1.25); }
  .usage-bar-label { font-size: 9px; color: var(--text-muted); margin-top: 6px; text-align: center; white-space: nowrap; }
  .usage-bar-value { font-size: 9px; color: var(--text-tertiary); text-align: center; position: absolute; top: -16px; width: 100%; white-space: nowrap; }
  .usage-grid-line { position: absolute; left: 0; right: 0; border-top: 1px dashed var(--border-secondary); }
  .usage-grid-label { position: absolute; right: 100%; padding-right: 8px; font-size: 10px; color: var(--text-muted); white-space: nowrap; }
  .usage-table { width: 100%; border-collapse: collapse; }
  .usage-table th { text-align: left; font-size: 12px; color: var(--text-muted); padding: 8px 12px; border-bottom: 1px solid var(--border-primary); font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
  .usage-table td { padding: 8px 12px; font-size: 13px; color: var(--text-secondary); border-bottom: 1px solid var(--border-secondary); }
  .usage-table tr:last-child td { border-bottom: none; font-weight: 700; color: var(--text-accent); }
  
  /* === Cost Warnings === */
  .cost-warning { padding: 12px 16px; border-radius: 8px; margin-bottom: 8px; display: flex; align-items: center; gap: 10px; font-size: 13px; }
  .cost-warning.error { background: var(--bg-error); border: 1px solid var(--text-error); color: var(--text-error); }
  .cost-warning.warning { background: var(--bg-warning); border: 1px solid var(--text-warning); color: var(--text-warning); }
  .cost-warning-icon { font-size: 16px; }
  .cost-warning-message { flex: 1; }

  /* === Transcript Viewer === */
  .transcript-item { padding: 12px 16px; border-bottom: 1px solid var(--border-secondary); cursor: pointer; transition: background 0.15s; display: flex; justify-content: space-between; align-items: center; }
  .transcript-item:hover { background: var(--bg-hover); }
  .transcript-item:last-child { border-bottom: none; }
  .transcript-name { font-weight: 600; font-size: 14px; color: var(--text-link); }
  .transcript-meta-row { font-size: 12px; color: var(--text-muted); margin-top: 4px; display: flex; gap: 12px; flex-wrap: wrap; }
  .transcript-viewer-meta { background: var(--bg-secondary); border: 1px solid var(--border-primary); border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  .transcript-viewer-meta .stat-row { padding: 6px 0; }
  .chat-messages { display: flex; flex-direction: column; gap: 10px; padding: 8px 0; }
  .chat-msg { max-width: 85%; padding: 12px 16px; border-radius: 16px; font-size: 13px; line-height: 1.5; word-wrap: break-word; position: relative; }
  .chat-msg.user { background: #1a2a4a; border: 1px solid #2a4a7a; color: #c0d8ff; align-self: flex-end; border-bottom-right-radius: 4px; }
  .chat-msg.assistant { background: #1a3a2a; border: 1px solid #2a5a3a; color: #c0ffc0; align-self: flex-start; border-bottom-left-radius: 4px; }
  .chat-msg.system { background: #2a2a1a; border: 1px solid #4a4a2a; color: #f0e0a0; align-self: center; font-size: 12px; font-style: italic; max-width: 90%; }
  .chat-msg.tool { background: #1a1a24; border: 1px solid #2a2a3a; color: #a0a0b0; align-self: flex-start; font-family: 'SF Mono', monospace; font-size: 12px; border-left: 3px solid #555; }
  .chat-role { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px; opacity: 0.7; }
  .chat-ts { font-size: 10px; color: #555; margin-top: 6px; text-align: right; }
  .chat-expand { display: inline-block; color: #f0c040; font-size: 11px; cursor: pointer; margin-top: 4px; }
  .chat-expand:hover { text-decoration: underline; }
  .chat-content-truncated { max-height: 200px; overflow: hidden; position: relative; }
  .chat-content-truncated::after { content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 40px; background: linear-gradient(transparent, rgba(26,42,74,0.9)); pointer-events: none; }
  .chat-msg.assistant .chat-content-truncated::after { background: linear-gradient(transparent, rgba(26,58,42,0.9)); }
  .chat-msg.tool .chat-content-truncated::after { background: linear-gradient(transparent, rgba(26,26,36,0.9)); }

  /* === Mini Dashboard Widgets === */
  .tool-spark { font-size: 11px; color: var(--text-muted); padding: 3px 8px; background: var(--bg-secondary); border-radius: 6px; border: 1px solid var(--border-secondary); }
  .tool-spark span { color: var(--text-accent); font-weight: 600; }
  .card:hover { transform: translateY(-1px); box-shadow: var(--card-shadow-hover); }
  .card[onclick] { cursor: pointer; }

  /* === Sub-Agent Worker Bees === */
  .subagent-item { display: flex; align-items: center; gap: 6px; padding: 2px 0; font-size: 10px; }
  .subagent-status { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .subagent-status.active { background: #16a34a; box-shadow: 0 0 4px rgba(22,163,74,0.5); }
  .subagent-status.idle { background: #d97706; box-shadow: 0 0 4px rgba(217,119,6,0.5); }
  .subagent-status.stale { background: #dc2626; box-shadow: 0 0 4px rgba(220,38,38,0.5); }
  .subagent-name { font-weight: 600; color: var(--text-secondary); }
  .subagent-task { color: var(--text-muted); font-size: 9px; }
  .subagent-runtime { color: var(--text-faint); font-size: 9px; margin-left: auto; }

  /* === Sub-Agent Detailed View === */
  .subagent-row { padding: 12px 16px; border-bottom: 1px solid var(--border-secondary); display: flex; align-items: center; gap: 12px; }
  .subagent-row:last-child { border-bottom: none; }
  .subagent-row:hover { background: var(--bg-hover); }
  .subagent-indicator { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
  .subagent-indicator.active { background: #16a34a; box-shadow: 0 0 8px rgba(22,163,74,0.6); animation: pulse 2s infinite; }
  .subagent-indicator.idle { background: #d97706; box-shadow: 0 0 8px rgba(217,119,6,0.6); }
  .subagent-indicator.stale { background: #dc2626; box-shadow: 0 0 8px rgba(220,38,38,0.6); opacity: 0.7; }
  .subagent-info { flex: 1; }
  .subagent-header { display: flex; justify-content: between; align-items: center; margin-bottom: 4px; }
  .subagent-id { font-weight: 600; font-size: 14px; color: var(--text-primary); }
  .subagent-runtime-badge { background: var(--bg-accent); color: var(--bg-primary); padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .subagent-meta { font-size: 12px; color: var(--text-muted); display: flex; gap: 16px; flex-wrap: wrap; }
  .subagent-meta span { display: flex; align-items: center; gap: 4px; }
  .subagent-description { font-size: 13px; color: var(--text-secondary); margin-top: 4px; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }

  /* === Active Tasks Cards === */
  .task-card { background: var(--bg-tertiary); border: 1px solid var(--border-primary); border-radius: 12px; padding: 16px; box-shadow: var(--card-shadow); position: relative; overflow: hidden; }
  .task-card.running { border-left: 4px solid #16a34a; }
  .task-card.complete { border-left: 4px solid #2563eb; opacity: 0.7; }
  .task-card.failed { border-left: 4px solid #dc2626; }
  .task-card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; }
  .task-card-name { font-weight: 700; font-size: 14px; color: var(--text-primary); line-height: 1.3; }
  .task-card-badge { padding: 2px 10px; border-radius: 12px; font-size: 11px; font-weight: 700; white-space: nowrap; }
  .task-card-badge.running { background: #dcfce7; color: #166534; }
  .task-card-badge.complete { background: #dbeafe; color: #1e40af; }
  .task-card-badge.failed { background: #fef2f2; color: #991b1b; }
  [data-theme="dark"] .task-card-badge.running { background: #14532d; color: #86efac; }
  [data-theme="dark"] .task-card-badge.complete { background: #1e3a5f; color: #93c5fd; }
  [data-theme="dark"] .task-card-badge.failed { background: #450a0a; color: #fca5a5; }
  .task-card-duration { font-size: 12px; color: var(--text-muted); margin-bottom: 6px; }
  .task-card-action { font-size: 12px; color: var(--text-secondary); font-family: 'JetBrains Mono', monospace; background: var(--bg-secondary); padding: 6px 10px; border-radius: 6px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .task-card-pulse { position: absolute; top: 12px; right: 12px; width: 10px; height: 10px; border-radius: 50%; background: #22c55e; }
  .task-card-pulse.active { animation: taskPulse 1.5s ease-in-out infinite; }
  @keyframes taskPulse { 0%,100% { box-shadow: 0 0 0 0 rgba(34,197,94,0.4); } 50% { box-shadow: 0 0 0 8px rgba(34,197,94,0); } }

  /* === Enhanced Active Tasks Panel === */
  .tasks-panel-scroll { max-height: 70vh; overflow-y: auto; overflow-x: hidden; scrollbar-width: thin; scrollbar-color: var(--border-primary) transparent; }
  .tasks-panel-scroll::-webkit-scrollbar { width: 6px; }
  .tasks-panel-scroll::-webkit-scrollbar-track { background: transparent; }
  .tasks-panel-scroll::-webkit-scrollbar-thumb { background: var(--border-primary); border-radius: 3px; }
  .task-group-header { font-size: 13px; font-weight: 700; color: var(--text-secondary); padding: 8px 4px 6px; margin-top: 4px; letter-spacing: 0.3px; }
  .task-group-header:first-child { margin-top: 0; }
  @keyframes idleBreathe { 0%,100% { opacity: 0.5; transform: scale(1); } 50% { opacity: 1; transform: scale(1.05); } }
  .tasks-empty-icon { animation: idleBreathe 3s ease-in-out infinite; display: inline-block; }
  @keyframes statusPulseGreen { 0%,100% { box-shadow: 0 0 0 0 rgba(34,197,94,0.5); } 50% { box-shadow: 0 0 0 6px rgba(34,197,94,0); } }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
  .status-dot.running { background: #22c55e; animation: statusPulseGreen 1.5s ease-in-out infinite; }
  .status-dot.complete { background: #3b82f6; }
  .status-dot.failed { background: #ef4444; }

  /* === Zoom Wrapper === */
  .zoom-wrapper { transform-origin: top left; transition: transform 0.3s ease; }

  /* === Split-Screen Overview === */
  .overview-split { display: grid; grid-template-columns: 60fr 1px 40fr; gap: 0; margin-bottom: 0; height: calc(100vh - 90px); }
  .overview-flow-pane { position: relative; border: 1px solid var(--border-primary); border-radius: 8px 0 0 8px; overflow: hidden; background: var(--bg-secondary); padding: 4px; }
  .overview-flow-pane .flow-container { height: 100%; }
  .overview-flow-pane svg { width: 100%; height: 100%; min-width: 0 !important; }
  .overview-divider { background: var(--border-primary); width: 1px; }
  .overview-tasks-pane { overflow: visible; border: 1px solid var(--border-primary); border-left: none; border-radius: 0 8px 8px 0; padding: 10px 12px; }
  /* Scanline overlay */
  .scanline-overlay { pointer-events: none; position: absolute; inset: 0; z-index: 2; background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,255,65,0.015) 2px, rgba(0,255,65,0.015) 4px); }
  .grid-overlay { pointer-events: none; position: absolute; inset: 0; z-index: 1; background-image: linear-gradient(var(--border-secondary) 1px, transparent 1px), linear-gradient(90deg, var(--border-secondary) 1px, transparent 1px); background-size: 40px 40px; opacity: 0.3; }
  /* Task cards in overview */
  .ov-task-card { background: var(--bg-tertiary); border: 1px solid var(--border-primary); border-radius: 10px; padding: 14px 16px; margin-bottom: 10px; box-shadow: var(--card-shadow); position: relative; transition: box-shadow 0.2s; }
  .ov-task-card:hover { box-shadow: var(--card-shadow-hover); }
  .ov-task-card.running { border-left: 4px solid #16a34a; }
  .ov-task-card.complete { border-left: 4px solid #2563eb; opacity: 0.75; }
  .ov-task-card.failed { border-left: 4px solid #dc2626; }
  .ov-task-pulse { width: 10px; height: 10px; border-radius: 50%; background: #22c55e; display: inline-block; animation: taskPulse 1.5s ease-in-out infinite; }
  .ov-details { display: none; margin-top: 10px; padding: 10px; background: var(--bg-secondary); border: 1px solid var(--border-secondary); border-radius: 8px; font-family: 'JetBrains Mono', 'SF Mono', monospace; font-size: 11px; line-height: 1.7; color: var(--text-tertiary); }
  .ov-details.open { display: block; }
  .ov-toggle-btn { background: none; border: 1px solid var(--border-primary); border-radius: 6px; padding: 3px 10px; font-size: 11px; color: var(--text-tertiary); cursor: pointer; transition: all 0.15s; }
  .ov-toggle-btn:hover { background: var(--bg-hover); color: var(--text-secondary); }

  /* === Task Detail Modal === */
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 1000; justify-content: center; align-items: center; }
  .modal-overlay.open { display: flex; }
  .modal-card { background: var(--bg-primary); border: 1px solid var(--border-primary); border-radius: 16px; width: 95%; max-width: 900px; max-height: 80vh; display: flex; flex-direction: column; box-shadow: 0 25px 50px rgba(0,0,0,0.25); }
  .modal-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 20px; border-bottom: 1px solid var(--border-primary); flex-shrink: 0; }
  .modal-header-left { flex: 1; min-width: 0; }
  .modal-title { font-size: 16px; font-weight: 700; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .modal-session-key { font-size: 11px; color: var(--text-muted); font-family: monospace; margin-top: 2px; }
  .modal-header-right { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
  .modal-auto-refresh { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-tertiary); cursor: pointer; }
  .modal-auto-refresh input { cursor: pointer; }
  .modal-close { background: var(--button-bg); border: 1px solid var(--border-primary); border-radius: 8px; width: 32px; height: 32px; display: flex; align-items: center; justify-content: center; cursor: pointer; font-size: 18px; color: var(--text-tertiary); transition: all 0.15s; }
  .modal-close:hover { background: var(--bg-error); color: var(--text-error); }
  .modal-tabs { display: flex; gap: 0; border-bottom: 1px solid var(--border-primary); padding: 0 20px; flex-shrink: 0; }
  .modal-tab { padding: 10px 18px; font-size: 13px; font-weight: 600; color: var(--text-muted); cursor: pointer; border-bottom: 2px solid transparent; transition: all 0.15s; }
  .modal-tab:hover { color: var(--text-secondary); }
  .modal-tab.active { color: var(--text-accent); border-bottom-color: var(--text-accent); }
  .modal-content { flex: 1; overflow-y: auto; padding: 20px; -webkit-overflow-scrolling: touch; }
  .modal-footer { border-top: 1px solid var(--border-primary); padding: 10px 20px; display: flex; gap: 16px; font-size: 12px; color: var(--text-muted); flex-shrink: 0; }
  /* Modal event items */
  .evt-item { border: 1px solid var(--border-secondary); border-radius: 8px; margin-bottom: 8px; overflow: hidden; }
  .evt-header { display: flex; align-items: center; gap: 8px; padding: 10px 14px; cursor: pointer; transition: background 0.15s; }
  .evt-header:hover { background: var(--bg-hover); }
  .evt-icon { font-size: 16px; flex-shrink: 0; }
  .evt-summary { flex: 1; font-size: 13px; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .evt-summary strong { color: var(--text-primary); }
  .evt-ts { font-size: 11px; color: var(--text-muted); flex-shrink: 0; font-family: monospace; }
  .evt-body { display: none; padding: 0 14px 12px; font-family: 'JetBrains Mono', 'SF Mono', monospace; font-size: 12px; line-height: 1.6; color: var(--text-tertiary); white-space: pre-wrap; word-break: break-word; max-height: 400px; overflow-y: auto; }
  .evt-body.open { display: block; }
  .evt-item.type-agent { border-left: 3px solid #3b82f6; }
  .evt-item.type-exec { border-left: 3px solid #16a34a; }
  .evt-item.type-read { border-left: 3px solid #8b5cf6; }
  .evt-item.type-result { border-left: 3px solid #ea580c; }
  .evt-item.type-thinking { border-left: 3px solid #6b7280; }
  .evt-item.type-user { border-left: 3px solid #7c3aed; }
  /* === Compact Stats Footer Bar === */
  .stats-footer { display: flex; gap: 0; border: 1px solid var(--border-primary); border-radius: 8px; margin-top: 6px; background: var(--bg-tertiary); overflow: hidden; }
  .stats-footer-item { flex: 1; padding: 6px 12px; display: flex; align-items: center; gap: 8px; border-right: 1px solid var(--border-primary); cursor: pointer; transition: background 0.15s; }
  .stats-footer-item:last-child { border-right: none; }
  .stats-footer-item:hover { background: var(--bg-hover); }
  .stats-footer-icon { font-size: 14px; }
  .stats-footer-label { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .stats-footer-value { font-size: 14px; font-weight: 700; color: var(--text-primary); }
  .stats-footer-sub { font-size: 10px; color: var(--text-faint); }
  @media (max-width: 1024px) {
    .stats-footer { flex-wrap: wrap; }
    .stats-footer-item { flex: 1 1 45%; min-width: 0; }
  }

  /* Narrative view */
  .narrative-item { padding: 10px 0; border-bottom: 1px solid var(--border-secondary); font-size: 13px; line-height: 1.6; color: var(--text-secondary); }
  .narrative-item:last-child { border-bottom: none; }
  .narrative-item .narr-icon { margin-right: 8px; }
  .narrative-item code { background: var(--bg-secondary); padding: 1px 6px; border-radius: 4px; font-family: 'JetBrains Mono', monospace; font-size: 12px; }
  /* Summary view */
  .summary-section { margin-bottom: 16px; }
  .summary-label { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); margin-bottom: 6px; }
  .summary-text { font-size: 14px; color: var(--text-secondary); line-height: 1.6; white-space: pre-wrap; }

  @media (max-width: 1024px) {
    .overview-split { grid-template-columns: 1fr; height: auto; }
    .overview-flow-pane { height: 40vh; min-height: 250px; border-radius: 8px 8px 0 0; }
    .overview-divider { width: auto; height: 1px; }
    .overview-tasks-pane { height: 60vh; border-radius: 0 0 8px 8px; border-left: 1px solid var(--border-primary); border-top: none; }
  }

  @media (max-width: 768px) {
    .nav { padding: 10px 12px; gap: 8px; }
    .nav h1 { font-size: 16px; }
    .nav-tab { padding: 6px 12px; font-size: 12px; }
    .page { padding: 12px; }
    .grid { grid-template-columns: 1fr; gap: 12px; }
    .card-value { font-size: 22px; }
    .flow-stats { gap: 8px; }
    .flow-stat { min-width: 70px; padding: 6px 10px; }
    .flow-stat-value { font-size: 16px; }
    #flow-svg { min-width: 600px; }
    .heatmap-grid { min-width: 500px; }
    .chat-msg { max-width: 95%; }
    .usage-chart { height: 150px; }
    
    /* Enhanced Flow mobile optimizations */
    .flow-container { 
      padding-bottom: 20px; 
      max-height: 70vh; 
      overflow-y: auto; 
    }
    #flow-svg text { font-size: 10px !important; }
    .flow-label { font-size: 7px !important; }
    .flow-node rect { stroke-width: 1 !important; }
    .flow-node.active rect { stroke-width: 1.5 !important; }
    .brain-group { animation-duration: 1.8s; } /* Faster on mobile */
    
    /* Mobile zoom controls */
    .zoom-controls { margin-left: 8px; gap: 2px; }
    .zoom-btn { width: 24px; height: 24px; font-size: 14px; }
    .zoom-level { min-width: 32px; font-size: 10px; }
  }
</style>
</head>
<body data-theme="light"><script>var t=localStorage.getItem('openclaw-theme');if(t==='dark')document.body.setAttribute('data-theme','dark');</script>
<div class="zoom-wrapper" id="zoom-wrapper">
<div class="nav">
  <h1><span>🦞</span> OpenClaw</h1>
  <div class="theme-toggle" onclick="toggleTheme()" title="Toggle theme">🌙</div>
  <div class="zoom-controls">
    <button class="zoom-btn" onclick="zoomOut()" title="Zoom out (Ctrl/Cmd + -)">−</button>
    <span class="zoom-level" id="zoom-level" title="Current zoom level. Ctrl/Cmd + 0 to reset">100%</span>
    <button class="zoom-btn" onclick="zoomIn()" title="Zoom in (Ctrl/Cmd + +)">+</button>
  </div>
  <div class="nav-tabs">
    <div class="nav-tab" onclick="switchTab('flow')">Flow</div>
    <div class="nav-tab active" onclick="switchTab('overview')">Overview</div>
    <div class="nav-tab" onclick="switchTab('sessions')">Sessions</div>
    <div class="nav-tab" onclick="switchTab('crons')">Schedules</div>
    <div class="nav-tab" onclick="switchTab('logs')">Logs</div>
    <div class="nav-tab" onclick="switchTab('memory')">Memory</div>
  </div>
</div>

<!-- OVERVIEW (Split-Screen Hacker Dashboard) -->
<div class="page active" id="page-overview">
  <div class="refresh-bar" style="margin-bottom:6px;">
    <button class="refresh-btn" onclick="loadAll()" style="padding:4px 12px;font-size:12px;">↻</button>
    <span class="pulse"></span>
    <span class="live-badge">LIVE</span>
    <span class="refresh-time" id="refresh-time" style="font-size:11px;">Loading...</span>
  </div>

  <!-- Split Screen: Flow Left | Tasks Right -->
  <div class="overview-split">
    <!-- LEFT: Flow Visualization -->
    <div class="overview-flow-pane">
      <div class="grid-overlay"></div>
      <div class="scanline-overlay"></div>
      <div class="flow-container" id="overview-flow-container">
        <!-- Flow SVG cloned here by JS -->
        <div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:13px;">Loading flow...</div>
      </div>
    </div>

    <!-- DIVIDER -->
    <div class="overview-divider"></div>

    <!-- RIGHT: Active Tasks Panel -->
    <div class="overview-tasks-pane">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
        <div style="display:flex;align-items:center;gap:8px;">
          <span style="font-size:15px;font-weight:700;color:var(--text-primary);">🐝 Active Tasks</span>
          <span id="overview-tasks-count-badge" style="font-size:11px;color:var(--text-muted);"></span>
        </div>
        <span style="font-size:10px;color:var(--text-faint);letter-spacing:0.5px;">⟳ 10s</span>
      </div>
      <div class="tasks-panel-scroll" id="overview-tasks-list">
        <div style="text-align:center;padding:32px;color:var(--text-muted);">
          <div style="font-size:28px;margin-bottom:8px;" class="tasks-empty-icon">🐝</div>
          <div style="font-size:13px;">Loading tasks...</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Compact Stats Footer Bar -->
  <div class="stats-footer">
    <div class="stats-footer-item" onclick="openDetailView('cost')">
      <span class="stats-footer-icon">💰</span>
      <div>
        <div class="stats-footer-label">Spending</div>
        <div class="stats-footer-value" id="cost-today">$0.00</div>
      </div>
      <div style="margin-left:auto;text-align:right;">
        <div class="stats-footer-sub">wk: <span id="cost-week">—</span></div>
        <div class="stats-footer-sub">mo: <span id="cost-month">—</span></div>
      </div>
      <span id="cost-trend" style="display:none;">Today's running total</span>
    </div>
    <div class="stats-footer-item" onclick="openDetailView('models')">
      <span class="stats-footer-icon">🤖</span>
      <div>
        <div class="stats-footer-label">Model</div>
        <div class="stats-footer-value" id="model-primary">—</div>
      </div>
      <div id="model-breakdown" style="display:none;">Loading...</div>
    </div>
    <div class="stats-footer-item" onclick="openDetailView('tokens')">
      <span class="stats-footer-icon">📊</span>
      <div>
        <div class="stats-footer-label">Tokens</div>
        <div class="stats-footer-value" id="token-rate">—</div>
      </div>
      <span class="stats-footer-sub" style="margin-left:auto;">today: <span id="tokens-today" style="color:var(--text-success);font-weight:600;">—</span></span>
    </div>
    <div class="stats-footer-item" onclick="switchTab('sessions')">
      <span class="stats-footer-icon">💬</span>
      <div>
        <div class="stats-footer-label">Sessions</div>
        <div class="stats-footer-value" id="hot-sessions-count">—</div>
      </div>
      <div id="hot-sessions-list" style="display:none;">Loading...</div>
    </div>
  </div>

  <!-- Hidden elements referenced by existing JS -->
  <div style="display:none;">
    <span id="tokens-peak">—</span>
    <span id="subagents-count">—</span>
    <span id="subagents-status">—</span>
    <span id="subagents-preview"></span>
    <span id="tools-active">—</span>
    <span id="tools-recent">—</span>
    <div id="tools-sparklines"><div class="tool-spark"><span>—</span></div><div class="tool-spark"><span>—</span></div><div class="tool-spark"><span>—</span></div></div>
    <div id="active-tasks-grid"></div>
    <div id="activity-stream"></div>
  </div>

  <!-- System Health (Compact) -->
  <div class="section-title">🟢 System Status</div>
  <div class="health-grid" id="health-grid">
    <div class="health-item" id="health-gateway"><div class="health-dot" id="health-dot-gateway"></div><div class="health-info"><div class="health-name">Gateway</div><div class="health-detail" id="health-detail-gateway">Checking...</div></div></div>
    <div class="health-item" id="health-disk"><div class="health-dot" id="health-dot-disk"></div><div class="health-info"><div class="health-name">Disk Space</div><div class="health-detail" id="health-detail-disk">Checking...</div></div></div>
    <div class="health-item" id="health-memory"><div class="health-dot" id="health-dot-memory"></div><div class="health-info"><div class="health-name">Memory</div><div class="health-detail" id="health-detail-memory">Checking...</div></div></div>
    <div class="health-item" id="health-otel"><div class="health-dot" id="health-dot-otel"></div><div class="health-info"><div class="health-name">📡 Data Source</div><div class="health-detail" id="health-detail-otel">Checking...</div></div></div>
  </div>
</div>

<!-- USAGE -->
<div class="page" id="page-usage">
  <div class="refresh-bar">
    <button class="refresh-btn" onclick="loadUsage()">↻ Refresh</button>
    <button class="refresh-btn" onclick="exportUsageData()" style="margin-left: 8px;">📥 Export CSV</button>
  </div>
  
  <!-- Cost Warnings -->
  <div id="cost-warnings" style="display:none; margin-bottom: 16px;"></div>
  
  <!-- Main Usage Stats -->
  <div class="grid">
    <div class="card">
      <div class="card-title"><span class="icon">📊</span> Today</div>
      <div class="card-value" id="usage-today">—</div>
      <div class="card-sub" id="usage-today-cost"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="icon">📅</span> This Week</div>
      <div class="card-value" id="usage-week">—</div>
      <div class="card-sub" id="usage-week-cost"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="icon">📆</span> This Month</div>
      <div class="card-value" id="usage-month">—</div>
      <div class="card-sub" id="usage-month-cost"></div>
    </div>
    <div class="card" id="trend-card" style="display:none;">
      <div class="card-title"><span class="icon">📈</span> Trend</div>
      <div class="card-value" id="trend-direction">—</div>
      <div class="card-sub" id="trend-prediction"></div>
    </div>
  </div>
  <div class="section-title">📊 Token Usage (14 days)</div>
  <div class="card">
    <div class="usage-chart" id="usage-chart">Loading...</div>
  </div>
  <div class="section-title">💰 Cost Breakdown</div>
  <div class="card"><table class="usage-table" id="usage-cost-table"><tbody><tr><td colspan="3" style="color:#666;">Loading...</td></tr></tbody></table></div>
  <div id="otel-extra-sections" style="display:none;">
    <div class="grid" style="margin-top:16px;">
      <div class="card">
        <div class="card-title"><span class="icon">⏱️</span> Avg Run Duration</div>
        <div class="card-value" id="usage-avg-run">—</div>
        <div class="card-sub">from OTLP openclaw.run.duration_ms</div>
      </div>
      <div class="card">
        <div class="card-title"><span class="icon">💬</span> Messages Processed</div>
        <div class="card-value" id="usage-msg-count">—</div>
        <div class="card-sub">from OTLP openclaw.message.processed</div>
      </div>
    </div>
    <div class="section-title">🤖 Model Breakdown</div>
    <div class="card"><table class="usage-table" id="usage-model-table"><tbody><tr><td colspan="2" style="color:#666;">No model data</td></tr></tbody></table></div>
    <div style="margin-top:12px;padding:8px 12px;background:#1a3a2a;border:1px solid #2a5a3a;border-radius:8px;font-size:12px;color:#60ff80;">📡 Data source: OpenTelemetry OTLP — real-time metrics from OpenClaw</div>
  </div>
</div>

<!-- SESSIONS (with Sub-Agent Tree) -->
<div class="page" id="page-sessions">
  <div class="refresh-bar">
    <button class="refresh-btn" onclick="loadSessions(); loadSubAgentsPage();">↻ Refresh</button>
    <label style="margin-left:12px;font-size:13px;color:var(--text-muted);display:flex;align-items:center;gap:6px;cursor:pointer;">
      <input type="checkbox" id="sa-auto-refresh" checked onchange="toggleSAAutoRefresh()" style="accent-color:var(--bg-accent);"> Auto-refresh
    </label>
    <span style="margin-left:auto;font-size:12px;color:var(--text-faint);" id="sa-refresh-time"></span>
  </div>

  <!-- Sub-Agent Mission Control -->
  <div class="section-title">🐝 Sub-Agent Activity <span style="font-size:12px;font-weight:400;color:var(--text-muted);">— click to see live details</span></div>

  <!-- Sub-Agent Stats Row -->
  <div class="grid" style="margin-bottom:16px;">
    <div class="card">
      <div class="card-title"><span class="icon">🟢</span> Active Now</div>
      <div class="card-value" id="subagents-active-count">—</div>
      <div class="card-sub">Currently working</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="icon">✅</span> Completed</div>
      <div class="card-value" id="subagents-stale-count">—</div>
      <div class="card-sub">Finished tasks</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="icon">📊</span> Total</div>
      <span id="subagents-idle-count" style="display:none;">—</span>
      <div class="card-value" id="subagents-total-count">—</div>
      <div class="card-sub">All sub-agents</div>
    </div>
  </div>

  <div class="card" id="subagents-list" style="padding:0;">Loading agents...</div>

  <!-- Activity detail panel -->
  <div id="sa-activity-panel" style="display:none;margin-top:12px;">
    <div class="card" style="padding:0;">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border-secondary);background:var(--bg-secondary);border-radius:12px 12px 0 0;">
        <div>
          <span style="font-weight:700;font-size:15px;color:var(--text-primary);" id="sa-panel-title">Sub-Agent</span>
          <span style="font-size:11px;color:var(--text-muted);margin-left:8px;" id="sa-panel-status"></span>
        </div>
        <button onclick="closeSAPanel()" style="background:var(--button-bg);border:1px solid var(--border-primary);color:var(--text-secondary);padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;">✕ Close</button>
      </div>
      <div id="sa-activity-timeline" style="max-height:500px;overflow-y:auto;padding:8px 0;">
        <div style="padding:20px;text-align:center;color:var(--text-muted);">Loading activity...</div>
      </div>
    </div>
  </div>

  <!-- Mission Control Tasks -->
  <div id="mc-bar-wrapper" style="display:none;margin-top:16px;">
    <div id="mc-summary-bar" style="background:var(--bg-tertiary);border:1px solid var(--border-primary);border-radius:10px;padding:10px 16px;display:flex;align-items:center;gap:6px;flex-wrap:wrap;box-shadow:var(--card-shadow);"></div>
    <div id="mc-expanded-section" style="display:none;background:var(--bg-tertiary);border:1px solid var(--border-primary);border-top:none;border-radius:0 0 10px 10px;padding:12px 16px;max-height:300px;overflow-y:auto;"></div>
  </div>

  <!-- Session Tree -->
  <div class="section-title" style="margin-top:24px;">💬 Session Tree</div>
  <div class="card" id="sessions-list">Loading...</div>
</div>

<!-- SUB-AGENTS (hidden, merged into Sessions) -->
<div class="page" id="page-subagents" style="display:none !important;">
  <div class="refresh-bar">
    <button class="refresh-btn" onclick="loadSubAgentsPage()">↻ Refresh</button>
    <label style="margin-left:12px;font-size:12px;color:#888;display:flex;align-items:center;gap:4px;cursor:pointer;">
      <input type="checkbox" id="sa-auto-refresh" checked onchange="toggleSAAutoRefresh()" style="accent-color:#60a0ff;"> Auto-refresh (5s)
    </label>
    <span style="margin-left:auto;font-size:11px;color:#555;" id="sa-refresh-time"></span>
  </div>

  <!-- Status legend -->
  <div style="display:flex;gap:16px;margin-bottom:12px;padding:8px 12px;background:var(--bg-secondary,#111128);border-radius:8px;font-size:12px;color:#888;flex-wrap:wrap;align-items:center;">
    <span style="font-weight:600;color:#aaa;">Status:</span>
    <span style="display:flex;align-items:center;gap:4px;"><span style="width:8px;height:8px;border-radius:50%;background:#27ae60;display:inline-block;"></span> Active — working right now</span>
    <span style="display:flex;align-items:center;gap:4px;"><span style="width:8px;height:8px;border-radius:50%;background:#f0c040;display:inline-block;"></span> Idle — finished recently (&lt;30m)</span>
    <span style="display:flex;align-items:center;gap:4px;"><span style="width:8px;height:8px;border-radius:50%;background:#e74c3c;display:inline-block;"></span> Done — completed or timed out</span>
  </div>

  <!-- Sub-Agent Stats Overview -->
  <div class="grid">
    <div class="card">
      <div class="card-title"><span class="icon">🟢</span> Active Now</div>
      <div class="card-value" id="subagents-active-count">—</div>
      <div class="card-sub">Currently working</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="icon">🟡</span> Recently Idle</div>
      <div class="card-value" id="subagents-idle-count">—</div>
      <div class="card-sub">Finished in last 30m</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="icon">✅</span> Completed</div>
      <div class="card-value" id="subagents-stale-count">—</div>
      <div class="card-sub">Done &amp; dusted</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="icon">📊</span> Total Spawned</div>
      <div class="card-value" id="subagents-total-count">—</div>
      <div class="card-sub">All sub-agents ever</div>
    </div>
  </div>

  <div class="section-title">🐝 Sub-Agent Activity <span style="font-size:12px;font-weight:400;color:#666;">— click a worker to see what it's doing</span></div>
  <div class="card" id="subagents-list" style="padding:0;">Loading workforce...</div>

  <!-- Expanded activity panel (shown when clicking a sub-agent) -->
  <div id="sa-activity-panel" style="display:none;margin-top:12px;">
    <div class="card" style="padding:0;">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border-secondary,#2a2a4a);background:var(--bg-secondary,#111128);">
        <div>
          <span style="font-weight:700;font-size:15px;color:var(--text-primary,#e0e0e0);" id="sa-panel-title">Sub-Agent</span>
          <span style="font-size:11px;color:#666;margin-left:8px;" id="sa-panel-status"></span>
        </div>
        <button onclick="closeSAPanel()" style="background:none;border:1px solid #444;color:#aaa;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:12px;">✕ Close</button>
      </div>
      <div id="sa-activity-timeline" style="max-height:500px;overflow-y:auto;padding:8px 0;">
        <div style="padding:20px;text-align:center;color:#666;">Loading activity...</div>
      </div>
    </div>
  </div>
</div>

<!-- CRONS -->
<div class="page" id="page-crons">
  <div class="refresh-bar"><button class="refresh-btn" onclick="loadCrons()">↻ Refresh</button></div>
  <div class="card" id="crons-list">Loading...</div>
</div>

<!-- LOGS -->
<div class="page" id="page-logs">
  <div class="refresh-bar">
    <button class="refresh-btn" onclick="loadLogs()">↻ Refresh</button>
    <select id="log-lines" onchange="loadLogs()" style="background:var(--bg-tertiary);color:var(--text-primary);border:1px solid var(--border-primary);padding:6px 10px;border-radius:6px;font-size:13px;">
      <option value="50">50 lines</option>
      <option value="100" selected>100 lines</option>
      <option value="300">300 lines</option>
      <option value="500">500 lines</option>
    </select>
  </div>
  <div class="log-viewer" id="logs-full" style="max-height:calc(100vh - 140px);">Loading...</div>
</div>

<!-- MEMORY -->
<div class="page" id="page-memory">
  <div class="refresh-bar">
    <button class="refresh-btn" onclick="loadMemory()">↻ Refresh</button>
  </div>
  <div class="card" id="memory-list">Loading...</div>
  <div class="file-viewer" id="file-viewer">
    <div class="file-viewer-header">
      <span class="file-viewer-title" id="file-viewer-title"></span>
      <button class="file-viewer-close" onclick="closeFileViewer()">✕ Close</button>
    </div>
    <div class="file-viewer-content" id="file-viewer-content"></div>
  </div>
</div>

<!-- TRANSCRIPTS -->
<div class="page" id="page-transcripts">
  <div class="refresh-bar">
    <button class="refresh-btn" onclick="loadTranscripts()">↻ Refresh</button>
    <button class="refresh-btn" id="transcript-back-btn" style="display:none" onclick="showTranscriptList()">← Back to list</button>
  </div>
  <div class="card" id="transcript-list">Loading...</div>
  <div id="transcript-viewer" style="display:none">
    <div class="transcript-viewer-meta" id="transcript-meta"></div>
    <div class="chat-messages" id="transcript-messages"></div>
  </div>
</div>

<!-- FLOW -->
<div class="page" id="page-flow">
  <div class="flow-stats">
    <div class="flow-stat"><span class="flow-stat-label">Messages / min</span><span class="flow-stat-value" id="flow-msg-rate">0</span></div>
    <div class="flow-stat"><span class="flow-stat-label">Actions Taken</span><span class="flow-stat-value" id="flow-event-count">0</span></div>
    <div class="flow-stat"><span class="flow-stat-label">Active Tools</span><span class="flow-stat-value" id="flow-active-tools">&mdash;</span></div>
    <div class="flow-stat"><span class="flow-stat-label">Tokens Used</span><span class="flow-stat-value" id="flow-tokens">&mdash;</span></div>
  </div>
  <div class="flow-container">
    <svg id="flow-svg" viewBox="0 0 800 550" preserveAspectRatio="xMidYMid meet">
      <defs>
        <pattern id="flow-grid" width="40" height="40" patternUnits="userSpaceOnUse">
          <path d="M 40 0 L 0 0 0 40" fill="none" stroke="var(--border-secondary)" stroke-width="0.5"/>
        </pattern>
        <filter id="dropShadow" x="-10%" y="-10%" width="130%" height="130%">
          <feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="rgba(0,0,0,0.25)" flood-opacity="0.4"/>
        </filter>
        <filter id="dropShadowLight" x="-10%" y="-10%" width="130%" height="130%">
          <feDropShadow dx="0" dy="1" stdDeviation="2" flood-color="rgba(0,0,0,0.15)" flood-opacity="0.3"/>
        </filter>
      </defs>
      <rect width="800" height="550" fill="var(--bg-primary)" rx="12"/>
      <rect width="800" height="550" fill="url(#flow-grid)"/>

      <!-- Human → Channel paths -->
      <path class="flow-path" id="path-human-tg"  d="M 60 56 C 60 70, 65 85, 75 100"/>
      <path class="flow-path" id="path-human-sig" d="M 60 56 C 55 90, 60 140, 75 170"/>
      <path class="flow-path" id="path-human-wa"  d="M 60 56 C 50 110, 55 200, 75 240"/>

      <!-- Channel → Gateway paths -->
      <path class="flow-path" id="path-tg-gw"  d="M 130 120 C 150 120, 160 165, 180 170"/>
      <path class="flow-path" id="path-sig-gw" d="M 130 190 C 150 190, 160 185, 180 183"/>
      <path class="flow-path" id="path-wa-gw"  d="M 130 260 C 150 260, 160 200, 180 195"/>

      <!-- Gateway → Brain -->
      <path class="flow-path" id="path-gw-brain" d="M 290 183 C 305 183, 315 175, 330 175"/>

      <!-- Brain → Tools -->
      <path class="flow-path" id="path-brain-session" d="M 510 155 C 530 130, 545 95, 560 89"/>
      <path class="flow-path" id="path-brain-exec"    d="M 510 160 C 530 150, 545 143, 560 139"/>
      <path class="flow-path" id="path-brain-browser" d="M 510 175 C 530 175, 545 189, 560 189"/>
      <path class="flow-path" id="path-brain-search"  d="M 510 185 C 530 200, 545 230, 560 239"/>
      <path class="flow-path" id="path-brain-cron"    d="M 510 195 C 530 230, 545 275, 560 289"/>
      <path class="flow-path" id="path-brain-tts"     d="M 510 205 C 530 260, 545 325, 560 339"/>
      <path class="flow-path" id="path-brain-memory"  d="M 510 215 C 530 290, 545 370, 560 389"/>

      <!-- Infrastructure paths (dashed) -->
      <path class="flow-path flow-path-infra" id="path-gw-network"    d="M 235 205 C 235 350, 500 400, 590 450"/>
      <path class="flow-path flow-path-infra" id="path-brain-runtime" d="M 380 220 C 300 350, 150 400, 95 450"/>
      <path class="flow-path flow-path-infra" id="path-brain-machine" d="M 420 220 C 380 350, 300 400, 260 450"/>
      <path class="flow-path flow-path-infra" id="path-memory-storage" d="M 615 408 C 550 420, 470 435, 425 450"/>

      <!-- Human Origin -->
      <g class="flow-node flow-node-human" id="node-human">
        <circle cx="60" cy="30" r="22" fill="#7c3aed" stroke="#6a2ec0" stroke-width="2" filter="url(#dropShadow)"/>
        <circle cx="60" cy="24" r="5" fill="#ffffff" opacity="0.6"/>
        <path d="M 50 38 Q 50 45 60 45 Q 70 45 70 38" fill="#ffffff" opacity="0.4"/>
        <text x="60" y="68" style="font-size:13px;fill:#7c3aed;font-weight:800;text-anchor:middle;" id="flow-human-name">You</text>
      </g>

      <!-- Channel Nodes -->
      <g class="flow-node flow-node-channel" id="node-telegram">
        <rect x="20" y="100" width="110" height="40" rx="10" ry="10" fill="#2196F3" stroke="#1565C0" stroke-width="2" filter="url(#dropShadow)"/>
        <text x="75" y="125" style="font-size:13px;font-weight:700;fill:#ffffff;text-anchor:middle;">📱 TG</text>
      </g>
      <g class="flow-node flow-node-channel" id="node-signal">
        <rect x="20" y="170" width="110" height="40" rx="10" ry="10" fill="#2E8B7A" stroke="#1B6B5A" stroke-width="2" filter="url(#dropShadow)"/>
        <text x="75" y="195" style="font-size:13px;font-weight:700;fill:#ffffff;text-anchor:middle;">📡 Signal</text>
      </g>
      <g class="flow-node flow-node-channel" id="node-whatsapp">
        <rect x="20" y="240" width="110" height="40" rx="10" ry="10" fill="#43A047" stroke="#2E7D32" stroke-width="2" filter="url(#dropShadow)"/>
        <text x="75" y="265" style="font-size:13px;font-weight:700;fill:#ffffff;text-anchor:middle;">💬 WA</text>
      </g>

      <!-- Gateway -->
      <g class="flow-node flow-node-gateway" id="node-gateway">
        <rect x="180" y="160" width="110" height="45" rx="10" ry="10" fill="#37474F" stroke="#263238" stroke-width="2" filter="url(#dropShadow)"/>
        <text x="235" y="188" style="font-size:13px;font-weight:700;fill:#ffffff;text-anchor:middle;">🔀 Gateway</text>
      </g>

      <!-- Brain -->
      <g class="flow-node flow-node-brain brain-group" id="node-brain">
        <rect x="330" y="130" width="180" height="90" rx="12" ry="12" fill="#C62828" stroke="#B71C1C" stroke-width="3" filter="url(#dropShadow)"/>
        <text x="420" y="162" style="font-size:24px;text-anchor:middle;">&#x1F9E0;</text>
        <text x="420" y="186" style="font-size:18px;font-weight:800;fill:#FFD54F;text-anchor:middle;" id="brain-model-label">Claude</text>
        <text x="420" y="203" style="font-size:10px;fill:#ffccbc;text-anchor:middle;" id="brain-model-text">claude-opus-4-5</text>
        <circle cx="420" cy="214" r="4" fill="#FF8A65">
          <animate attributeName="r" values="3;5;3" dur="1.1s" repeatCount="indefinite"/>
          <animate attributeName="opacity" values="0.5;1;0.5" dur="1.1s" repeatCount="indefinite"/>
        </circle>
      </g>

      <!-- Tool Nodes -->
      <g class="flow-node flow-node-session" id="node-session">
        <rect x="560" y="70" width="110" height="38" rx="10" ry="10" fill="#1565C0" stroke="#0D47A1" stroke-width="2" filter="url(#dropShadow)"/>
        <text x="615" y="94" style="font-size:13px;font-weight:700;fill:#ffffff;text-anchor:middle;">📋 Sessions</text>
        <circle class="tool-indicator" id="ind-session" cx="665" cy="78" r="5" fill="#42A5F5"/>
      </g>
      <g class="flow-node flow-node-tool" id="node-exec">
        <rect x="560" y="120" width="110" height="38" rx="10" ry="10" fill="#E65100" stroke="#BF360C" stroke-width="2" filter="url(#dropShadow)"/>
        <text x="615" y="144" style="font-size:13px;font-weight:700;fill:#ffffff;text-anchor:middle;">⚡ Exec</text>
        <circle class="tool-indicator" id="ind-exec" cx="665" cy="128" r="5" fill="#FF6E40"/>
      </g>
      <g class="flow-node flow-node-tool" id="node-browser">
        <rect x="560" y="170" width="110" height="38" rx="10" ry="10" fill="#6A1B9A" stroke="#4A148C" stroke-width="2" filter="url(#dropShadow)"/>
        <text x="615" y="194" style="font-size:13px;font-weight:700;fill:#ffffff;text-anchor:middle;">🌐 Web</text>
        <circle class="tool-indicator" id="ind-browser" cx="665" cy="178" r="5" fill="#CE93D8"/>
      </g>
      <g class="flow-node flow-node-tool" id="node-search">
        <rect x="560" y="220" width="110" height="38" rx="10" ry="10" fill="#00695C" stroke="#004D40" stroke-width="2" filter="url(#dropShadow)"/>
        <text x="615" y="244" style="font-size:13px;font-weight:700;fill:#ffffff;text-anchor:middle;">&#x1F50D; Search</text>
        <circle class="tool-indicator" id="ind-search" cx="665" cy="228" r="5" fill="#4DB6AC"/>
      </g>
      <g class="flow-node flow-node-tool" id="node-cron">
        <rect x="560" y="270" width="110" height="38" rx="10" ry="10" fill="#546E7A" stroke="#37474F" stroke-width="2" filter="url(#dropShadow)"/>
        <text x="615" y="294" style="font-size:13px;font-weight:700;fill:#ffffff;text-anchor:middle;">📅 Cron</text>
        <circle class="tool-indicator" id="ind-cron" cx="665" cy="278" r="5" fill="#90A4AE"/>
      </g>
      <g class="flow-node flow-node-tool" id="node-tts">
        <rect x="560" y="320" width="110" height="38" rx="10" ry="10" fill="#F9A825" stroke="#F57F17" stroke-width="2" filter="url(#dropShadow)"/>
        <text x="615" y="344" style="font-size:13px;font-weight:700;fill:#ffffff;text-anchor:middle;">&#x1F5E3;&#xFE0F; TTS</text>
        <circle class="tool-indicator" id="ind-tts" cx="665" cy="328" r="5" fill="#FFF176"/>
      </g>
      <g class="flow-node flow-node-tool" id="node-memory">
        <rect x="560" y="370" width="110" height="38" rx="10" ry="10" fill="#283593" stroke="#1A237E" stroke-width="2" filter="url(#dropShadow)"/>
        <text x="615" y="394" style="font-size:13px;font-weight:700;fill:#ffffff;text-anchor:middle;">&#x1F4BE; Memory</text>
        <circle class="tool-indicator" id="ind-memory" cx="665" cy="378" r="5" fill="#7986CB"/>
      </g>

      <!-- Infrastructure Layer -->
      <line class="flow-ground" x1="20" y1="440" x2="780" y2="440"/>
      <text class="flow-ground-label" x="400" y="438" style="text-anchor:middle;font-size:10px;">I N F R A S T R U C T U R E</text>

      <g class="flow-node flow-node-infra flow-node-runtime" id="node-runtime">
        <rect x="30" y="450" width="130" height="40" rx="8" ry="8" fill="#455A64" stroke="#37474F" filter="url(#dropShadowLight)"/>
        <text x="95" y="466" style="font-size:13px;fill:#ffffff;font-weight:700;text-anchor:middle;">&#x2699;&#xFE0F; Runtime</text>
        <text class="infra-sub" x="95" y="481" style="fill:#B0BEC5;font-size:9px;text-anchor:middle;" id="infra-runtime-text">Node.js · Linux</text>
      </g>
      <g class="flow-node flow-node-infra flow-node-machine" id="node-machine">
        <rect x="195" y="450" width="130" height="40" rx="8" ry="8" fill="#4E342E" stroke="#3E2723" filter="url(#dropShadowLight)"/>
        <text x="260" y="466" style="font-size:13px;fill:#ffffff;font-weight:700;text-anchor:middle;">&#x1F5A5;&#xFE0F; Machine</text>
        <text class="infra-sub" x="260" y="481" style="fill:#BCAAA4;font-size:9px;text-anchor:middle;" id="infra-machine-text">Host</text>
      </g>
      <g class="flow-node flow-node-infra flow-node-storage" id="node-storage">
        <rect x="360" y="450" width="130" height="40" rx="8" ry="8" fill="#5D4037" stroke="#4E342E" filter="url(#dropShadowLight)"/>
        <text x="425" y="466" style="font-size:13px;fill:#ffffff;font-weight:700;text-anchor:middle;">&#x1F4BF; Storage</text>
        <text class="infra-sub" x="425" y="481" style="fill:#BCAAA4;font-size:9px;text-anchor:middle;" id="infra-storage-text">Disk</text>
      </g>
      <g class="flow-node flow-node-infra flow-node-network" id="node-network">
        <rect x="525" y="450" width="130" height="40" rx="8" ry="8" fill="#004D40" stroke="#00332E" filter="url(#dropShadowLight)"/>
        <text x="590" y="466" style="font-size:13px;fill:#ffffff;font-weight:700;text-anchor:middle;">&#x1F310; Network</text>
        <text class="infra-sub" x="590" y="481" style="fill:#80CBC4;font-size:9px;text-anchor:middle;" id="infra-network-text">LAN</text>
      </g>

      <!-- Legend -->
      <g transform="translate(100, 510)">
        <rect x="0" y="0" width="600" height="28" rx="14" ry="14" fill="var(--bg-tertiary)" stroke="var(--border-primary)" stroke-width="1" opacity="0.9"/>
        <text x="300" y="18" style="font-size:12px;font-weight:600;fill:var(--text-secondary);letter-spacing:1px;text-anchor:middle;">&#x1F4E8; Channels  &#x27A1;&#xFE0F;  🔀 Gateway  &#x27A1;&#xFE0F;  &#x1F9E0; AI Brain  &#x27A1;&#xFE0F;  &#x1F6E0;&#xFE0F; Tools</text>
      </g>

      <!-- Flow direction labels -->
      <text class="flow-label" x="120" y="155" style="font-size:9px;">messages in</text>
      <text class="flow-label" x="300" y="155" style="font-size:9px;">routes to AI</text>
      <text class="flow-label" x="520" y="155" style="font-size:9px;">uses tools</text>
    </svg>
  </div>

  <!-- Live activity feed under the flow diagram -->
  <div style="margin-top:12px;background:var(--bg-secondary,#111128);border:1px solid var(--border-secondary,#2a2a4a);border-radius:10px;padding:12px 16px;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
      <span style="font-size:13px;font-weight:600;color:#aaa;">📡 Live Activity Feed</span>
      <span style="font-size:10px;color:#555;" id="flow-feed-count">0 events</span>
    </div>
    <div id="flow-live-feed" style="max-height:120px;overflow-y:auto;font-family:'SF Mono',monospace;font-size:11px;line-height:1.5;color:#777;">
      <div style="color:#555;">Waiting for activity...</div>
    </div>
  </div>
</div>

<script>
function switchTab(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'overview') loadAll();
  if (name === 'usage') loadUsage();
  if (name === 'sessions') { loadSessions(); loadSubAgentsPage(); }
  if (name === 'subagents') loadSubAgentsPage();
  if (name === 'crons') loadCrons();
  if (name === 'logs') loadLogs();
  if (name === 'memory') loadMemory();
  if (name === 'transcripts') loadTranscripts();
  if (name === 'flow') initFlow();
}

function exportUsageData() {
  window.location.href = '/api/usage/export';
}

function toggleTheme() {
  const body = document.body;
  const toggle = document.querySelector('.theme-toggle');
  const isLight = !body.hasAttribute('data-theme') || body.getAttribute('data-theme') !== 'dark';
  
  if (isLight) {
    body.setAttribute('data-theme', 'dark');
    toggle.textContent = '☀️';
    toggle.title = 'Switch to light theme';
    localStorage.setItem('openclaw-theme', 'dark');
  } else {
    body.removeAttribute('data-theme');
    toggle.textContent = '🌙';
    toggle.title = 'Switch to dark theme';
    localStorage.setItem('openclaw-theme', 'light');
  }
}

function initTheme() {
  const savedTheme = localStorage.getItem('openclaw-theme') || 'light';
  const body = document.body;
  const toggle = document.querySelector('.theme-toggle');
  
  if (savedTheme === 'dark') {
    body.setAttribute('data-theme', 'dark');
    toggle.textContent = '☀️';
    toggle.title = 'Switch to light theme';
  } else {
    body.removeAttribute('data-theme');
    toggle.textContent = '🌙';
    toggle.title = 'Switch to dark theme';
  }
}

// === Zoom Controls ===
let currentZoom = 1.0;
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2.0;
const ZOOM_STEP = 0.1;

function initZoom() {
  const savedZoom = localStorage.getItem('openclaw-zoom');
  if (savedZoom) {
    currentZoom = parseFloat(savedZoom);
  }
  applyZoom();
}

function applyZoom() {
  const wrapper = document.getElementById('zoom-wrapper');
  const levelDisplay = document.getElementById('zoom-level');
  
  if (wrapper) {
    wrapper.style.transform = `scale(${currentZoom})`;
  }
  if (levelDisplay) {
    levelDisplay.textContent = Math.round(currentZoom * 100) + '%';
  }
  
  // Save to localStorage
  localStorage.setItem('openclaw-zoom', currentZoom.toString());
}

function zoomIn() {
  if (currentZoom < MAX_ZOOM) {
    currentZoom = Math.min(MAX_ZOOM, currentZoom + ZOOM_STEP);
    applyZoom();
  }
}

function zoomOut() {
  if (currentZoom > MIN_ZOOM) {
    currentZoom = Math.max(MIN_ZOOM, currentZoom - ZOOM_STEP);
    applyZoom();
  }
}

function resetZoom() {
  currentZoom = 1.0;
  applyZoom();
}

// Keyboard shortcuts for zoom
document.addEventListener('keydown', function(e) {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey) {
    if (e.key === '=' || e.key === '+') {
      e.preventDefault();
      zoomIn();
    } else if (e.key === '-') {
      e.preventDefault();
      zoomOut();
    } else if (e.key === '0') {
      e.preventDefault();
      resetZoom();
    }
  }
});

function timeAgo(ms) {
  if (!ms) return 'never';
  var diff = Date.now() - ms;
  if (diff < 60000) return Math.floor(diff/1000) + 's ago';
  if (diff < 3600000) return Math.floor(diff/60000) + 'm ago';
  if (diff < 86400000) return Math.floor(diff/3600000) + 'h ago';
  return Math.floor(diff/86400000) + 'd ago';
}

function formatTime(ms) {
  if (!ms) return '—';
  return new Date(ms).toLocaleString('en-GB', {hour:'2-digit',minute:'2-digit',day:'numeric',month:'short'});
}

async function loadAll() {
  var [overview, logs, usage] = await Promise.all([
    fetch('/api/overview').then(r => r.json()),
    fetch('/api/logs?lines=30').then(r => r.json()),
    fetch('/api/usage').then(r => r.json())
  ]);

  // Load new mini dashboard widgets
  loadMiniWidgets(overview, usage);
  
  // Load active tasks panel
  startActiveTasksRefresh();
  
  // Load activity stream
  loadActivityStream();

  // Load health checks
  loadHealth();

  // Load Mission Control tasks
  loadMCTasks();

  document.getElementById('refresh-time').textContent = 'Updated ' + new Date().toLocaleTimeString();

  // Update flow infra details
  if (overview.infra) {
    var i = overview.infra;
    if (i.runtime) document.getElementById('infra-runtime-text').textContent = i.runtime;
    if (i.machine) document.getElementById('infra-machine-text').textContent = i.machine;
    if (i.storage) document.getElementById('infra-storage-text').textContent = i.storage;
    if (i.network) document.getElementById('infra-network-text').textContent = 'LAN ' + i.network;
    if (i.userName) document.getElementById('flow-human-name').textContent = i.userName;
  }
}

async function loadMiniWidgets(overview, usage) {
  // 💰 Cost Ticker 
  function fmtCost(c) { return c >= 0.01 ? '$' + c.toFixed(2) : c > 0 ? '<$0.01' : '$0.00'; }
  document.getElementById('cost-today').textContent = fmtCost(usage.todayCost || 0);
  document.getElementById('cost-week').textContent = fmtCost(usage.weekCost || 0);
  document.getElementById('cost-month').textContent = fmtCost(usage.monthCost || 0);
  
  var trend = '';
  if (usage.trend && usage.trend.trend) {
    var trendIcon = usage.trend.trend === 'increasing' ? '📈' : usage.trend.trend === 'decreasing' ? '📉' : '➡️';
    trend = trendIcon + ' ' + usage.trend.trend;
  }
  document.getElementById('cost-trend').textContent = trend || 'Today\'s running total';
  
  // ⚡ Tool Activity (load from logs)
  loadToolActivity();
  
  // 📊 Token Burn Rate
  document.getElementById('token-rate').textContent = '—';
  function fmtTokens(n) { return n >= 1000000 ? (n/1000000).toFixed(1) + 'M' : n >= 1000 ? (n/1000).toFixed(0) + 'K' : String(n); }
  document.getElementById('tokens-today').textContent = fmtTokens(usage.today || 0);
  
  // 🔥 Hot Sessions
  document.getElementById('hot-sessions-count').textContent = overview.sessionCount || 0;
  var hotHtml = '';
  if (overview.sessionCount > 0) {
    hotHtml = '<div style="font-size:11px;color:#f0c040;">Main session active</div>';
    if (overview.mainSessionUpdated) {
      hotHtml += '<div style="font-size:10px;color:#666;">Updated ' + timeAgo(overview.mainSessionUpdated) + '</div>';
    }
  } else {
    hotHtml = '<div style="font-size:11px;color:#666;">No active sessions</div>';
  }
  document.getElementById('hot-sessions-list').innerHTML = hotHtml;
  
  // 📈 Model Mix
  document.getElementById('model-primary').textContent = overview.model || 'claude-opus-4-5';
  var modelBreakdown = '';
  if (usage.modelBreakdown && usage.modelBreakdown.length > 0) {
    var primary = usage.modelBreakdown[0];
    var others = usage.modelBreakdown.slice(1, 3);
    modelBreakdown = fmtTokens(primary.tokens) + ' tokens';
    if (others.length > 0) {
      modelBreakdown += ' (+' + others.length + ' others)';
    }
  } else {
    modelBreakdown = 'Primary model';
  }
  document.getElementById('model-breakdown').textContent = modelBreakdown;
  
  // 🐝 Worker Bees (Sub-Agents)
  loadSubAgents();
  
}

async function loadSubAgents() {
  try {
    var data = await fetch('/api/subagents').then(r => r.json());
    var counts = data.counts;
    var subagents = data.subagents;
    
    // Update main counter
    document.getElementById('subagents-count').textContent = counts.total;
    
    // Update status text
    var statusText = '';
    if (counts.active > 0) {
      statusText = counts.active + ' active';
      if (counts.idle > 0) statusText += ', ' + counts.idle + ' idle';
      if (counts.stale > 0) statusText += ', ' + counts.stale + ' stale';
    } else if (counts.total === 0) {
      statusText = 'No sub-agents spawned';
    } else {
      statusText = 'All idle/stale';
    }
    document.getElementById('subagents-status').textContent = statusText;
    
    // Update preview with top sub-agents (human-readable)
    var previewHtml = '';
    if (subagents.length === 0) {
      previewHtml = '<div style="font-size:11px;color:#666;">No active tasks</div>';
    } else {
      // Show active ones first
      var activeFirst = subagents.filter(function(a){return a.status==='active';}).concat(subagents.filter(function(a){return a.status!=='active';}));
      var topAgents = activeFirst.slice(0, 3);
      topAgents.forEach(function(agent) {
        var icon = agent.status === 'active' ? '🔄' : agent.status === 'idle' ? '✅' : '⬜';
        var name = cleanTaskName(agent.displayName);
        if (name.length > 40) name = name.substring(0, 37) + '…';
        previewHtml += '<div class="subagent-item">';
        previewHtml += '<span style="font-size:10px;">' + icon + '</span>';
        previewHtml += '<span class="subagent-name" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + escHtml(name) + '</span>';
        previewHtml += '<span class="subagent-runtime">' + agent.runtime + '</span>';
        previewHtml += '</div>';
      });
      
      if (subagents.length > 3) {
        previewHtml += '<div style="font-size:9px;color:#555;margin-top:4px;">+' + (subagents.length - 3) + ' more</div>';
      }
    }
    
    document.getElementById('subagents-preview').innerHTML = previewHtml;
    
  } catch(e) {
    document.getElementById('subagents-count').textContent = '?';
    document.getElementById('subagents-status').textContent = 'Error loading sub-agents';
    document.getElementById('subagents-preview').innerHTML = '<div style="color:#e74c3c;font-size:11px;">Failed to load workforce</div>';
  }
}

// === Active Tasks for Overview ===
var _activeTasksTimer = null;
function cleanTaskName(raw) {
  // Strip timestamp prefixes like "[Sun 2026-02-08 18:22 GMT+1] "
  var name = (raw || '').replace(/^\[.*?\]\s*/, '');
  // Truncate to first sentence or 80 chars
  var dot = name.indexOf('. ');
  if (dot > 10 && dot < 80) name = name.substring(0, dot + 1);
  if (name.length > 80) name = name.substring(0, 77) + '…';
  return name || 'Background task';
}

function detectProjectBadge(text) {
  var projects = {
    'mockround': { label: 'MockRound', color: '#7c3aed' },
    'vedicvoice': { label: 'VedicVoice', color: '#d97706' },
    'openclaw': { label: 'OpenClaw', color: '#2563eb' },
    'dashboard': { label: 'Dashboard', color: '#0891b2' },
    'shopify': { label: 'Shopify', color: '#16a34a' },
    'sanskrit': { label: 'Sanskrit', color: '#ea580c' },
    'telegram': { label: 'Telegram', color: '#0088cc' },
    'discord': { label: 'Discord', color: '#5865f2' },
  };
  var lower = (text || '').toLowerCase();
  for (var key in projects) {
    if (lower.includes(key)) return projects[key];
  }
  return null;
}

function humanTime(runtimeMs) {
  if (!runtimeMs || runtimeMs === Infinity) return '';
  var sec = Math.floor(runtimeMs / 1000);
  if (sec < 60) return 'Started ' + sec + 's ago';
  var min = Math.floor(sec / 60);
  if (min < 60) return 'Started ' + min + ' min ago';
  var hr = Math.floor(min / 60);
  if (hr < 24) return 'Started ' + hr + 'h ago';
  return 'Started ' + Math.floor(hr / 24) + 'd ago';
}

function humanTimeDone(runtimeMs) {
  if (!runtimeMs || runtimeMs === Infinity) return '';
  var sec = Math.floor(runtimeMs / 1000);
  if (sec < 60) return 'Finished ' + sec + 's ago';
  var min = Math.floor(sec / 60);
  if (min < 60) return 'Finished ' + min + ' min ago';
  var hr = Math.floor(min / 60);
  if (hr < 24) return 'Finished ' + hr + 'h ago';
  return 'Finished ' + Math.floor(hr / 24) + 'd ago';
}

async function loadActiveTasks() {
  try {
    var data = await fetch('/api/subagents').then(r => r.json());
    var grid = document.getElementById('overview-tasks-list') || document.getElementById('active-tasks-grid');
    if (!grid) return;
    var agents = data.subagents || [];
    if (agents.length === 0) {
      grid.innerHTML = '<div class="card" style="text-align:center;padding:24px;color:var(--text-muted);grid-column:1/-1;">'
        + '<div style="font-size:24px;margin-bottom:8px;">✨</div>'
        + '<div style="font-size:13px;">No active tasks — all quiet</div></div>';
      return;
    }

    // Group by status: running first, then recently completed, then truly failed
    var running = [], done = [], failed = [];
    agents.forEach(function(agent) {
      var isRealFailure = agent.status === 'stale' && agent.abortedLastRun && (agent.outputTokens || 0) === 0;
      if (agent.status === 'active') running.push(agent);
      else if (isRealFailure) failed.push(agent);
      else done.push(agent);
    });
    var sorted = running.concat(done).concat(failed);

    // Only show recent ones (last 2 hours for done, all running)
    sorted = sorted.filter(function(a) {
      if (a.status === 'active') return true;
      return a.runtimeMs < 2 * 60 * 60 * 1000; // 2 hours
    });

    if (sorted.length === 0) {
      grid.innerHTML = '<div class="card" style="text-align:center;padding:24px;color:var(--text-muted);grid-column:1/-1;">'
        + '<div style="font-size:24px;margin-bottom:8px;">✨</div>'
        + '<div style="font-size:13px;">No recent tasks</div></div>';
      return;
    }

    var html = '';
    sorted.forEach(function(agent) {
      // Determine true completion status: only "failed" if zero output and aborted
      var isRealFailure = agent.status === 'stale' && agent.abortedLastRun && (agent.outputTokens || 0) === 0;
      var statusClass = agent.status === 'active' ? 'running' : isRealFailure ? 'failed' : 'complete';
      var statusEmoji, statusLabel, timeStr;
      if (agent.status === 'active') {
        var mins = Math.max(1, Math.floor((agent.runtimeMs || 0) / 60000));
        statusEmoji = '🔄';
        statusLabel = 'Running (' + mins + ' min)';
        timeStr = humanTime(agent.runtimeMs);
      } else if (isRealFailure) {
        statusEmoji = '❌';
        statusLabel = 'Failed';
        timeStr = humanTimeDone(agent.runtimeMs);
      } else {
        statusEmoji = '✅';
        statusLabel = 'Done';
        timeStr = humanTimeDone(agent.runtimeMs);
      }

      var taskName = cleanTaskName(agent.displayName);
      var badge = detectProjectBadge(agent.displayName);

      html += '<div class="task-card ' + statusClass + '" style="cursor:pointer;" onclick="openTaskModal(\'' + escHtml(agent.sessionId).replace(/'/g,"\\'") + '\',\'' + escHtml(taskName).replace(/'/g,"\\'") + '\',\'' + escHtml(agent.key || agent.sessionId).replace(/'/g,"\\'") + '\')">';
      if (agent.status === 'active') html += '<div class="task-card-pulse active"></div>';
      html += '<div class="task-card-header">';
      html += '<div class="task-card-name">' + escHtml(taskName) + '</div>';
      html += '<span class="task-card-badge ' + statusClass + '">' + statusEmoji + ' ' + statusLabel + '</span>';
      html += '</div>';
      // Project badge + human time
      html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">';
      if (badge) {
        html += '<span style="display:inline-block;padding:1px 8px;border-radius:10px;font-size:10px;font-weight:700;background:' + badge.color + '22;color:' + badge.color + ';border:1px solid ' + badge.color + '44;">' + badge.label + '</span>';
      }
      if (timeStr) {
        html += '<span style="font-size:12px;color:var(--text-muted);">' + escHtml(timeStr) + '</span>';
      }
      html += '</div>';
      // Show/Hide details toggle + logs link
      var detailId = 'task-detail-' + agent.sessionId.replace(/[^a-z0-9]/gi, '');
      html += '<div style="display:flex;align-items:center;gap:12px;margin-top:4px;">';
      html += '<span style="font-size:11px;color:var(--text-tertiary);cursor:pointer;user-select:none;" onclick="event.stopPropagation();var d=document.getElementById(\'' + detailId + '\');var open=d.style.display!==\'none\';d.style.display=open?\'none\':\'block\';this.textContent=open?\'▶ Show details\':\'▼ Hide details\';">▶ Show details</span>';
      html += '<span style="font-size:11px;color:var(--text-link);cursor:pointer;opacity:0.7;" onclick="event.stopPropagation();switchTab(\'sessions\');setTimeout(function(){openSAActivity(\'' + agent.sessionId + '\',\'' + escHtml(agent.displayName).replace(/'/g, "\\'") + '\',\'' + agent.status + '\')},300)">📋 View logs</span>';
      html += '</div>';
      // Collapsible technical details
      html += '<div id="' + detailId + '" style="display:none;margin-top:8px;padding:10px;background:var(--bg-secondary);border:1px solid var(--border-secondary);border-radius:8px;font-size:11px;line-height:1.7;color:var(--text-tertiary);">';
      html += '<div><span style="color:var(--text-muted);">Session:</span> <span style="font-family:monospace;font-size:10px;">' + escHtml(agent.sessionId) + '</span></div>';
      html += '<div><span style="color:var(--text-muted);">Key:</span> <span style="font-family:monospace;font-size:10px;">' + escHtml(agent.key) + '</span></div>';
      html += '<div><span style="color:var(--text-muted);">Model:</span> ' + escHtml(agent.model || 'unknown') + '</div>';
      html += '<div><span style="color:var(--text-muted);">Channel:</span> ' + escHtml(agent.channel || '—') + '</div>';
      html += '<div><span style="color:var(--text-muted);">Runtime:</span> ' + escHtml(agent.runtime) + ' (' + Math.round((agent.runtimeMs||0)/1000) + 's)</div>';
      // Full task prompt
      html += '<div style="margin-top:6px;"><span style="color:var(--text-muted);">Full prompt:</span></div>';
      html += '<div style="font-size:10px;font-family:monospace;white-space:pre-wrap;word-break:break-word;max-height:120px;overflow-y:auto;padding:6px;background:var(--bg-primary);border-radius:4px;margin-top:2px;">' + escHtml(agent.displayName) + '</div>';
      // Recent tool calls
      if (agent.recentTools && agent.recentTools.length > 0) {
        html += '<div style="margin-top:6px;"><span style="color:var(--text-muted);">Recent tools:</span></div>';
        agent.recentTools.forEach(function(t) {
          html += '<div style="font-size:10px;font-family:monospace;color:var(--text-tertiary);"><span style="color:var(--text-accent);">' + escHtml(t.name) + '</span> ' + escHtml(t.summary) + '</div>';
        });
      }
      if (agent.lastText) {
        html += '<div style="margin-top:6px;"><span style="color:var(--text-muted);">Last output:</span></div>';
        html += '<div style="font-size:10px;font-style:italic;color:var(--text-tertiary);max-height:60px;overflow-y:auto;">' + escHtml(agent.lastText) + '</div>';
      }
      html += '</div>';
      html += '</div>';
    });
    grid.innerHTML = html;
  } catch(e) {
    // silently fail
  }
}
// Auto-refresh active tasks every 5s
function startActiveTasksRefresh() {
  loadActiveTasks();
  if (_activeTasksTimer) clearInterval(_activeTasksTimer);
  _activeTasksTimer = setInterval(loadActiveTasks, 5000);
}

async function loadToolActivity() {
  try {
    var logs = await fetch('/api/logs?lines=100').then(r => r.json());
    var toolCounts = { exec: 0, browser: 0, search: 0, other: 0 };
    var recentTools = [];
    
    logs.lines.forEach(function(line) {
      var msg = line.toLowerCase();
      if (msg.includes('tool') || msg.includes('invoke')) {
        if (msg.includes('exec') || msg.includes('shell')) { 
          toolCounts.exec++; recentTools.push('exec'); 
        } else if (msg.includes('browser') || msg.includes('screenshot')) { 
          toolCounts.browser++; recentTools.push('browser'); 
        } else if (msg.includes('web_search') || msg.includes('web_fetch')) { 
          toolCounts.search++; recentTools.push('search'); 
        } else {
          toolCounts.other++;
        }
      }
    });
    
    document.getElementById('tools-active').textContent = recentTools.slice(0, 3).join(', ') || 'Idle';
    document.getElementById('tools-recent').textContent = 'Last ' + Math.min(logs.lines.length, 100) + ' log entries';
    
    var sparks = document.querySelectorAll('.tool-spark span');
    sparks[0].textContent = toolCounts.exec;
    sparks[1].textContent = toolCounts.browser;  
    sparks[2].textContent = toolCounts.search;
  } catch(e) {
    document.getElementById('tools-active').textContent = '—';
  }
}

async function loadActivityStream() {
  try {
    var transcripts = await fetch('/api/transcripts').then(r => r.json());
    var activities = [];
    
    // Get the most recent transcript to parse for activity
    if (transcripts.transcripts && transcripts.transcripts.length > 0) {
      var recent = transcripts.transcripts[0];
      try {
        var transcript = await fetch('/api/transcript/' + recent.id).then(r => r.json());
        var recentMessages = transcript.messages.slice(-10); // Last 10 messages
        
        recentMessages.forEach(function(msg) {
          if (msg.role === 'assistant' && msg.content) {
            var content = msg.content.toLowerCase();
            var activity = '';
            var time = new Date(msg.timestamp || Date.now()).toLocaleTimeString();
            
            if (content.includes('searching') || content.includes('search')) {
              activity = time + ' 🔍 Searching web for information';
            } else if (content.includes('reading') || content.includes('file')) {
              activity = time + ' 📖 Reading files';
            } else if (content.includes('writing') || content.includes('edit')) {
              activity = time + ' ✏️ Editing files'; 
            } else if (content.includes('exec') || content.includes('command')) {
              activity = time + ' ⚡ Running commands';
            } else if (content.includes('browser') || content.includes('screenshot')) {
              activity = time + ' 🌐 Browser automation';
            } else if (msg.content.length > 50) {
              var preview = msg.content.substring(0, 80).replace(/[^\w\s]/g, ' ').trim();
              activity = time + ' 💭 ' + preview + '...';
            }
            
            if (activity) activities.push(activity);
          }
        });
      } catch(e) {}
    }
    
    if (activities.length === 0) {
      activities = [
        new Date().toLocaleTimeString() + ' 🤖 AI agent initialized',
        new Date().toLocaleTimeString() + ' 📡 Monitoring for activity...'
      ];
    }
    
    var html = activities.slice(-8).map(function(a) {
      return '<div style="padding:4px 0; border-bottom:1px solid #1a1a30; color:#ccc;">' + escHtml(a) + '</div>';
    }).join('');
    
    document.getElementById('activity-stream').innerHTML = html;
  } catch(e) {
    document.getElementById('activity-stream').innerHTML = '<div style="color:#666;">Error loading activity stream</div>';
  }
}

// ===== Sub-Agent Live Activity System =====
var _saAutoRefreshTimer = null;
var _saSelectedId = null;

function toggleSAAutoRefresh() {
  if (document.getElementById('sa-auto-refresh').checked) {
    _saAutoRefreshTimer = setInterval(function() { loadSubAgentsPage(true); }, 5000);
  } else {
    clearInterval(_saAutoRefreshTimer);
    _saAutoRefreshTimer = null;
  }
}

async function loadSubAgentsPage(silent) {
  try {
    var data = await fetch('/api/subagents').then(r => r.json());
    var counts = data.counts;
    var subagents = data.subagents;

    // Update stats
    document.getElementById('subagents-active-count').textContent = counts.active;
    document.getElementById('subagents-idle-count').textContent = counts.idle;
    document.getElementById('subagents-stale-count').textContent = counts.stale;
    document.getElementById('subagents-total-count').textContent = counts.total;
    document.getElementById('sa-refresh-time').textContent = 'Updated ' + new Date().toLocaleTimeString();

    var listHtml = '';
    if (subagents.length === 0) {
      listHtml = '<div style="padding:40px;text-align:center;color:#666;">'
        + '<div style="font-size:48px;margin-bottom:16px;">🐝</div>'
        + '<div style="font-size:16px;margin-bottom:8px;">No Sub-Agents Yet</div>'
        + '<div style="font-size:12px;max-width:400px;margin:0 auto;">Sub-agents are spawned by the main AI to handle complex tasks in parallel. They\'ll appear here when active.</div>'
        + '</div>';
    } else {
      subagents.forEach(function(agent) {
        var isSelected = _saSelectedId === agent.sessionId;
        var statusIcon = agent.status === 'active' ? '🟢' : agent.status === 'idle' ? '🟡' : '⬜';
        var statusLabel = agent.status === 'active' ? 'Working...' : agent.status === 'idle' ? 'Recently finished' : 'Completed';

        listHtml += '<div class="subagent-row" style="cursor:pointer;' + (isSelected ? 'background:var(--bg-hover,#1a1a3a);border-left:3px solid #60a0ff;' : '') + '" onclick="openSAActivity(\'' + agent.sessionId + '\',\'' + escHtml(agent.displayName) + '\',\'' + agent.status + '\')">';
        listHtml += '<div class="subagent-indicator ' + agent.status + '"></div>';
        listHtml += '<div class="subagent-info" style="flex:1;">';

        // Header line: name + status badge + time
        listHtml += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">';
        listHtml += '<span class="subagent-id">' + statusIcon + ' ' + escHtml(agent.displayName) + '</span>';
        listHtml += '<div style="display:flex;gap:8px;align-items:center;">';
        listHtml += '<span style="font-size:11px;padding:2px 8px;border-radius:10px;background:' + (agent.status === 'active' ? '#1a3a2a' : '#1a1a2a') + ';color:' + (agent.status === 'active' ? '#60ff80' : '#888') + ';">' + statusLabel + '</span>';
        listHtml += '<span style="font-size:11px;color:#666;">' + agent.runtime + '</span>';
        listHtml += '</div></div>';

        // Recent tool calls (live activity preview)
        if (agent.recentTools && agent.recentTools.length > 0) {
          listHtml += '<div style="margin-top:4px;display:flex;flex-direction:column;gap:2px;">';
          var showTools = agent.recentTools.slice(-3);  // Show last 3 tools
          showTools.forEach(function(tool) {
            var toolColor = tool.name === 'exec' ? '#f0c040' : tool.name.match(/Read|Write|Edit/) ? '#60a0ff' : tool.name === 'web_search' ? '#c0a0ff' : '#50e080';
            listHtml += '<div style="font-size:11px;color:#888;display:flex;align-items:center;gap:6px;font-family:monospace;">';
            listHtml += '<span style="color:' + toolColor + ';font-weight:600;min-width:70px;">' + escHtml(tool.name) + '</span>';
            listHtml += '<span style="color:#666;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + escHtml(tool.summary) + '</span>';
            listHtml += '</div>';
          });
          listHtml += '</div>';
        }

        // Last assistant text (what it's thinking/saying)
        if (agent.lastText) {
          listHtml += '<div style="margin-top:4px;font-size:11px;color:#777;font-style:italic;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">💭 ' + escHtml(agent.lastText.substring(0, 120)) + '</div>';
        }

        listHtml += '</div></div>';
      });
    }

    document.getElementById('subagents-list').innerHTML = listHtml;

    // If we have a selected sub-agent, refresh its activity panel too
    if (_saSelectedId && !silent) {
      loadSAActivity(_saSelectedId);
    }

    // Start auto-refresh if enabled
    if (!_saAutoRefreshTimer && document.getElementById('sa-auto-refresh').checked) {
      _saAutoRefreshTimer = setInterval(function() { loadSubAgentsPage(true); }, 5000);
    }

  } catch(e) {
    if (!silent) {
      document.getElementById('subagents-list').innerHTML = '<div style="padding:20px;color:#e74c3c;text-align:center;">Failed to load: ' + e.message + '</div>';
    }
  }
}

function openSAActivity(sessionId, name, status) {
  _saSelectedId = sessionId;
  document.getElementById('sa-activity-panel').style.display = 'block';
  document.getElementById('sa-panel-title').textContent = '🐝 ' + name;
  document.getElementById('sa-panel-status').textContent = status === 'active' ? '🟢 Working' : status === 'idle' ? '🟡 Idle' : '⬜ Done';
  loadSAActivity(sessionId);
  // Re-render list to highlight selected
  loadSubAgentsPage(true);
}

function closeSAPanel() {
  _saSelectedId = null;
  document.getElementById('sa-activity-panel').style.display = 'none';
  loadSubAgentsPage(true);
}

async function loadSAActivity(sessionId) {
  var container = document.getElementById('sa-activity-timeline');
  try {
    var data = await fetch('/api/subagent/' + sessionId + '/activity').then(r => r.json());
    if (!data.events || data.events.length === 0) {
      container.innerHTML = '<div style="padding:20px;text-align:center;color:#666;">No activity recorded yet</div>';
      return;
    }

    var html = '';
    data.events.forEach(function(evt, i) {
      var time = evt.ts ? new Date(evt.ts).toLocaleTimeString('en-GB', {hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';

      if (evt.type === 'tool_call') {
        var color = evt.tool === 'exec' ? '#f0c040' : evt.tool.match(/Read|Write|Edit/) ? '#60a0ff' : evt.tool === 'web_search' ? '#c0a0ff' : evt.tool === 'browser' ? '#40a0b0' : '#50e080';
        html += '<div style="display:flex;gap:8px;padding:6px 16px;align-items:flex-start;border-left:3px solid ' + color + ';">';
        html += '<span style="font-size:10px;color:#555;min-width:55px;font-family:monospace;">' + time + '</span>';
        html += '<span style="font-size:11px;color:' + color + ';font-weight:700;min-width:80px;">⚡ ' + escHtml(evt.tool) + '</span>';
        html += '<span style="font-size:11px;color:#aaa;font-family:monospace;word-break:break-all;">' + escHtml(evt.input) + '</span>';
        html += '</div>';
      } else if (evt.type === 'tool_result') {
        var resultColor = evt.isError ? '#e04040' : '#2a5a3a';
        html += '<div style="display:flex;gap:8px;padding:4px 16px 4px 24px;align-items:flex-start;">';
        html += '<span style="font-size:10px;color:#555;min-width:55px;font-family:monospace;">' + time + '</span>';
        html += '<span style="font-size:10px;color:' + (evt.isError ? '#e04040' : '#555') + ';min-width:80px;">' + (evt.isError ? '❌ error' : '✓ result') + '</span>';
        html += '<span style="font-size:10px;color:#666;font-family:monospace;max-height:40px;overflow:hidden;word-break:break-all;">' + escHtml((evt.preview || '').substring(0, 200)) + '</span>';
        html += '</div>';
      } else if (evt.type === 'thinking') {
        html += '<div style="display:flex;gap:8px;padding:8px 16px;align-items:flex-start;border-left:3px solid #50e080;">';
        html += '<span style="font-size:10px;color:#555;min-width:55px;font-family:monospace;">' + time + '</span>';
        html += '<span style="font-size:11px;color:#50e080;min-width:80px;">💬 says</span>';
        html += '<span style="font-size:12px;color:#ccc;">' + escHtml(evt.text) + '</span>';
        html += '</div>';
      } else if (evt.type === 'internal_thought') {
        html += '<div style="display:flex;gap:8px;padding:4px 16px;align-items:flex-start;opacity:0.6;">';
        html += '<span style="font-size:10px;color:#555;min-width:55px;font-family:monospace;">' + time + '</span>';
        html += '<span style="font-size:10px;color:#9070d0;min-width:80px;">🧠 thinks</span>';
        html += '<span style="font-size:10px;color:#888;font-style:italic;">' + escHtml(evt.text) + '</span>';
        html += '</div>';
      } else if (evt.type === 'model_change') {
        html += '<div style="display:flex;gap:8px;padding:4px 16px;align-items:center;opacity:0.5;">';
        html += '<span style="font-size:10px;color:#555;min-width:55px;font-family:monospace;">' + time + '</span>';
        html += '<span style="font-size:10px;color:#888;">🔄 Model: ' + escHtml(evt.model) + '</span>';
        html += '</div>';
      }
    });

    container.innerHTML = html;
    // Auto-scroll to bottom
    container.scrollTop = container.scrollHeight;
  } catch(e) {
    container.innerHTML = '<div style="padding:20px;text-align:center;color:#e74c3c;">Failed to load activity: ' + e.message + '</div>';
  }
}

function openDetailView(type) {
  // Navigate to the appropriate tab with detail view
  if (type === 'cost' || type === 'tokens') {
    switchTab('usage');
  } else if (type === 'sessions') {
    switchTab('sessions');
  } else if (type === 'subagents') {
    switchTab('subagents');
  } else if (type === 'tools') {
    switchTab('logs');
  } else {
    // For thinking feed and models, stay on overview but could expand in future
    alert('Detail view for ' + type + ' coming soon!');
  }
}

function renderLogs(elId, lines) {
  var html = '';
  lines.forEach(function(l) {
    var cls = 'msg';
    var display = l;
    try {
      var obj = JSON.parse(l);
      var ts = '';
      if (obj.time || (obj._meta && obj._meta.date)) {
        var d = new Date(obj.time || obj._meta.date);
        ts = d.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
      }
      var level = (obj.logLevelName || obj.level || 'info').toLowerCase();
      if (level === 'error' || level === 'fatal') cls = 'err';
      else if (level === 'warn' || level === 'warning') cls = 'warn';
      else if (level === 'debug') cls = 'msg';
      else cls = 'info';
      var msg = obj.msg || obj.message || obj.name || '';
      var extras = [];
      // Field "0" is usually a JSON string like {"subsystem":"gateway/ws"} - extract subsystem
      var subsystem = '';
      if (obj["0"]) {
        try { var sub = JSON.parse(obj["0"]); subsystem = sub.subsystem || ''; } catch(e) { subsystem = String(obj["0"]); }
      }
      // Field "1" can be a string or object - stringify objects
      function flatVal(v) { return (typeof v === 'object' && v !== null) ? JSON.stringify(v) : String(v); }
      if (obj["1"]) {
        if (typeof obj["1"] === 'object') {
          var parts = [];
          for (var k in obj["1"]) { if (k !== 'cause') parts.push(k + '=' + flatVal(obj["1"][k])); else parts.unshift(flatVal(obj["1"][k])); }
          extras.push(parts.join(' '));
        } else {
          extras.push(String(obj["1"]));
        }
      }
      if (obj["2"]) extras.push(flatVal(obj["2"]));
      // Build display
      var prefix = subsystem ? '[' + subsystem + '] ' : '';
      if (msg && extras.length) display = prefix + msg + ' ' + extras.join(' ');
      else if (extras.length) display = prefix + extras.join(' ');
      else if (msg) display = prefix + msg;
      else display = l.substring(0, 200);
      if (ts) display = '<span class="ts">' + ts + '</span> ' + escHtml(display);
      else display = escHtml(display);
    } catch(e) {
      if (l.includes('Error') || l.includes('failed')) cls = 'err';
      else if (l.includes('WARN')) cls = 'warn';
      display = escHtml(l.substring(0, 300));
    }
    html += '<div class="log-line"><span class="' + cls + '">' + display + '</span></div>';
  });
  document.getElementById(elId).innerHTML = html || '<span style="color:#555">No logs</span>';
  document.getElementById(elId).scrollTop = document.getElementById(elId).scrollHeight;
}

function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function viewFile(path) {
  var viewer = document.getElementById('file-viewer');
  var title = document.getElementById('file-viewer-title');
  var content = document.getElementById('file-viewer-content');
  title.textContent = path;
  content.textContent = 'Loading...';
  viewer.style.display = 'block';
  try {
    var data = await fetch('/api/file?path=' + encodeURIComponent(path)).then(r => r.json());
    if (data.error) { content.textContent = 'Error: ' + data.error; return; }
    content.textContent = data.content;
  } catch(e) {
    content.textContent = 'Failed to load: ' + e.message;
  }
  viewer.scrollIntoView({behavior:'smooth'});
}

function closeFileViewer() {
  document.getElementById('file-viewer').style.display = 'none';
}

async function loadSessions() {
  var [sessData, saData] = await Promise.all([
    fetch('/api/sessions').then(r => r.json()),
    fetch('/api/subagents').then(r => r.json())
  ]);
  var html = '';
  // Main sessions (non-subagent)
  var mainSessions = sessData.sessions.filter(function(s) { return !(s.sessionId || '').includes('subagent'); });
  var subagents = saData.subagents || [];
  
  mainSessions.forEach(function(s) {
    html += '<div class="session-item" style="border-left:3px solid var(--bg-accent);padding-left:16px;">';
    html += '<div class="session-name">🖥️ ' + escHtml(s.displayName || s.key) + ' <span style="font-size:11px;color:var(--text-muted);font-weight:400;">Main Session</span></div>';
    html += '<div class="session-meta">';
    html += '<span><span class="badge model">' + (s.model||'default') + '</span></span>';
    if (s.channel !== 'unknown') html += '<span><span class="badge channel">' + s.channel + '</span></span>';
    html += '<span>Updated ' + timeAgo(s.updatedAt) + '</span>';
    html += '</div>';
    // Sub-agents nested underneath
    if (subagents.length > 0) {
      html += '<div style="margin-top:8px;margin-left:16px;border-left:2px solid var(--border-primary);padding-left:12px;">';
      subagents.forEach(function(sa) {
        var statusIcon = sa.status === 'active' ? '🟢' : sa.status === 'idle' ? '🟡' : '⬜';
        html += '<details style="margin-bottom:4px;">';
        html += '<summary style="cursor:pointer;font-size:13px;color:var(--text-secondary);padding:4px 0;">';
        html += statusIcon + ' <strong>' + escHtml(sa.displayName) + '</strong>';
        html += ' <span style="color:var(--text-muted);font-size:11px;">' + sa.runtime + '</span>';
        html += '</summary>';
        html += '<div style="padding:6px 0 6px 20px;font-size:12px;color:var(--text-muted);">';
        if (sa.recentTools && sa.recentTools.length > 0) {
          sa.recentTools.slice(-3).forEach(function(t) {
            html += '<div style="font-family:monospace;margin-bottom:2px;">⚡ <span style="color:var(--text-accent);">' + escHtml(t.name) + '</span> ' + escHtml(t.summary.substring(0,80)) + '</div>';
          });
        }
        if (sa.lastText) {
          html += '<div style="font-style:italic;margin-top:4px;">💭 ' + escHtml(sa.lastText.substring(0, 120)) + '</div>';
        }
        html += '</div></details>';
      });
      html += '</div>';
    }
    html += '</div>';
  });
  
  // Show orphan sessions that aren't main
  var subSessions = sessData.sessions.filter(function(s) { return (s.sessionId || '').includes('subagent'); });
  if (subSessions.length > 0 && mainSessions.length === 0) {
    sessData.sessions.forEach(function(s) {
      html += '<div class="session-item">';
      html += '<div class="session-name">' + escHtml(s.displayName || s.key) + '</div>';
      html += '<div class="session-meta">';
      html += '<span><span class="badge model">' + (s.model||'default') + '</span></span>';
      html += '<span>Updated ' + timeAgo(s.updatedAt) + '</span>';
      html += '</div></div>';
    });
  }
  
  document.getElementById('sessions-list').innerHTML = html || '<div style="padding:16px;color:var(--text-muted);">No sessions found</div>';
}

async function loadCrons() {
  var data = await fetch('/api/crons').then(r => r.json());
  var html = '';
  data.jobs.forEach(function(j) {
    var status = j.state && j.state.lastStatus ? j.state.lastStatus : 'pending';
    html += '<div class="cron-item">';
    html += '<div style="display:flex;justify-content:space-between;align-items:center;">';
    html += '<div class="cron-name">' + escHtml(j.name || j.id) + '</div>';
    html += '<span class="cron-status ' + status + '">' + status + '</span>';
    html += '</div>';
    html += '<div class="cron-schedule">' + formatSchedule(j.schedule) + '</div>';
    html += '<div class="cron-meta">';
    if (j.state && j.state.lastRunAtMs) html += 'Last: ' + timeAgo(j.state.lastRunAtMs);
    if (j.state && j.state.nextRunAtMs) html += ' · Next: ' + formatTime(j.state.nextRunAtMs);
    if (j.state && j.state.lastDurationMs) html += ' · Took: ' + (j.state.lastDurationMs/1000).toFixed(1) + 's';
    html += '</div></div>';
  });
  document.getElementById('crons-list').innerHTML = html || 'No cron jobs';
}

function formatSchedule(s) {
  if (s.kind === 'cron') return 'cron: ' + s.expr + (s.tz ? ' (' + s.tz + ')' : '');
  if (s.kind === 'every') return 'every ' + (s.everyMs/60000) + ' min';
  if (s.kind === 'at') return 'once at ' + formatTime(s.atMs);
  return JSON.stringify(s);
}

async function loadLogs() {
  var lines = document.getElementById('log-lines').value;
  var data = await fetch('/api/logs?lines=' + lines).then(r => r.json());
  renderLogs('logs-full', data.lines);
}

async function loadMemory() {
  var data = await fetch('/api/memory-files').then(r => r.json());
  var html = '';
  data.forEach(function(f) {
    var size = f.size > 1024 ? (f.size/1024).toFixed(1) + ' KB' : f.size + ' B';
    html += '<div class="memory-item" onclick="viewFile(\'' + escHtml(f.path) + '\')">';
    html += '<span class="memory-name" style="color:#60a0ff;">' + escHtml(f.path) + '</span>';
    html += '<span class="memory-size">' + size + '</span>';
    html += '</div>';
  });
  document.getElementById('memory-list').innerHTML = html || 'No memory files';
}

// ===== Mission Control Summary Bar =====
var _mcData = null;
var _mcExpanded = null;
var _mcRefreshTimer = null;

async function loadMCTasks() {
  try {
    var r = await fetch('/api/mc-tasks');
    var data = await r.json();
    var wrapper = document.getElementById('mc-bar-wrapper');
    if (!data.available) { wrapper.style.display='none'; return; }
    wrapper.style.display='';
    var tasks = data.tasks || [];
    var cols = [
      {key:'inbox', label:'Inbox', color:'#3b82f6', bg:'#3b82f620', icon:'📥', tasks:[]},
      {key:'in_progress', label:'In Progress', color:'#16a34a', bg:'#16a34a20', icon:'🔄', tasks:[]},
      {key:'review', label:'Review', color:'#d97706', bg:'#d9770620', icon:'👀', tasks:[]},
      {key:'blocked', label:'Blocked', color:'#dc2626', bg:'#dc262620', icon:'🚫', tasks:[]},
      {key:'done', label:'Done', color:'#6b7280', bg:'#6b728020', icon:'✅', tasks:[]}
    ];
    tasks.forEach(function(t) {
      var col = t.column || 'inbox';
      var c = cols.find(function(x){return x.key===col;});
      if (c) c.tasks.push(t);
    });
    _mcData = cols;
    var bar = document.getElementById('mc-summary-bar');
    var html = '<span style="font-size:12px;font-weight:700;color:var(--text-tertiary);margin-right:4px;">🎯 MC</span>';
    cols.forEach(function(c, i) {
      if (i > 0) html += '<span style="color:var(--text-faint);font-size:12px;margin:0 2px;">│</span>';
      var active = _mcExpanded === c.key ? 'outline:2px solid '+c.color+';outline-offset:-2px;' : '';
      html += '<span onclick="toggleMCColumn(\''+c.key+'\')" style="cursor:pointer;display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:16px;background:'+c.bg+';'+active+'transition:all 0.15s;">';
      html += '<span style="font-size:12px;">'+c.icon+'</span>';
      html += '<span style="font-size:11px;color:var(--text-secondary);">'+c.label+'</span>';
      html += '<span style="font-size:12px;font-weight:700;color:'+c.color+';min-width:16px;text-align:center;">'+c.tasks.length+'</span>';
      html += '</span>';
    });
    var total = tasks.length;
    html += '<span style="margin-left:auto;font-size:10px;color:var(--text-muted);">'+total+' tasks</span>';
    bar.innerHTML = html;
    if (_mcExpanded) renderMCExpanded(_mcExpanded);
  } catch(e) {
    var w = document.getElementById('mc-bar-wrapper');
    if (w) w.style.display='none';
  }
}

function toggleMCColumn(key) {
  if (_mcExpanded === key) { _mcExpanded = null; document.getElementById('mc-expanded-section').style.display='none'; }
  else { _mcExpanded = key; renderMCExpanded(key); }
  // Re-render bar to update active pill
  if (_mcData) {
    var bar = document.getElementById('mc-summary-bar');
    var cols = _mcData;
    var html = '<span style="font-size:12px;font-weight:700;color:var(--text-tertiary);margin-right:4px;">🎯 MC</span>';
    cols.forEach(function(c, i) {
      if (i > 0) html += '<span style="color:var(--text-faint);font-size:12px;margin:0 2px;">│</span>';
      var active = _mcExpanded === c.key ? 'outline:2px solid '+c.color+';outline-offset:-2px;' : '';
      html += '<span onclick="toggleMCColumn(\''+c.key+'\')" style="cursor:pointer;display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:16px;background:'+c.bg+';'+active+'transition:all 0.15s;">';
      html += '<span style="font-size:12px;">'+c.icon+'</span>';
      html += '<span style="font-size:11px;color:var(--text-secondary);">'+c.label+'</span>';
      html += '<span style="font-size:12px;font-weight:700;color:'+c.color+';min-width:16px;text-align:center;">'+c.tasks.length+'</span>';
      html += '</span>';
    });
    var total = cols.reduce(function(s,c){return s+c.tasks.length;},0);
    html += '<span style="margin-left:auto;font-size:10px;color:var(--text-muted);">'+total+' tasks</span>';
    bar.innerHTML = html;
  }
}

function renderMCExpanded(key) {
  var sec = document.getElementById('mc-expanded-section');
  if (!_mcData) { sec.style.display='none'; return; }
  var col = _mcData.find(function(c){return c.key===key;});
  if (!col || col.tasks.length === 0) { sec.style.display='block'; sec.innerHTML='<div style="font-size:12px;color:var(--text-muted);padding:4px;">No tasks in '+col.label+'</div>'; return; }
  sec.style.display='block';
  var html = '<div style="font-size:11px;font-weight:700;color:'+col.color+';margin-bottom:8px;">'+col.icon+' '+col.label+' ('+col.tasks.length+')</div>';
  html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:6px;">';
  col.tasks.forEach(function(t) {
    var title = t.title || '—';
    var badge = t.companyId ? '<span style="font-size:9px;background:var(--bg-secondary);padding:1px 5px;border-radius:3px;color:var(--text-muted);margin-left:6px;">'+t.companyId+'</span>' : '';
    html += '<div style="font-size:12px;color:var(--text-secondary);padding:4px 8px;background:var(--bg-secondary);border-radius:6px;border-left:3px solid '+col.color+';white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="'+(t.title||'').replace(/"/g,'&quot;')+'">'+title+badge+'</div>';
  });
  html += '</div>';
  sec.innerHTML = html;
}

// MC auto-refresh every 30s
if (!_mcRefreshTimer) {
  _mcRefreshTimer = setInterval(function() {
    if (document.querySelector('.nav-tab.active')?.textContent?.trim() === 'Overview') loadMCTasks();
  }, 30000);
}

// ===== Health Checks =====
async function loadHealth() {
  try {
    var data = await fetch('/api/health').then(r => r.json());
    data.checks.forEach(function(c) {
      var dotEl = document.getElementById('health-dot-' + c.id);
      var detailEl = document.getElementById('health-detail-' + c.id);
      var itemEl = document.getElementById('health-' + c.id);
      if (dotEl) { dotEl.className = 'health-dot ' + c.color; }
      if (detailEl) { detailEl.textContent = c.detail; }
      if (itemEl) { itemEl.className = 'health-item ' + c.status; }
    });
  } catch(e) {}
}

// Health SSE auto-refresh
var healthStream = null;
function startHealthStream() {
  if (healthStream) healthStream.close();
  healthStream = new EventSource('/api/health-stream');
  healthStream.onmessage = function(e) {
    try {
      var data = JSON.parse(e.data);
      data.checks.forEach(function(c) {
        var dotEl = document.getElementById('health-dot-' + c.id);
        var detailEl = document.getElementById('health-detail-' + c.id);
        var itemEl = document.getElementById('health-' + c.id);
        if (dotEl) { dotEl.className = 'health-dot ' + c.color; }
        if (detailEl) { detailEl.textContent = c.detail; }
        if (itemEl) { itemEl.className = 'health-item ' + c.status; }
      });
    } catch(ex) {}
  };
  healthStream.onerror = function() { setTimeout(startHealthStream, 30000); };
}
startHealthStream();

// ===== Activity Heatmap =====
async function loadHeatmap() {
  try {
    var data = await fetch('/api/heatmap').then(r => r.json());
    var grid = document.getElementById('heatmap-grid');
    var maxVal = Math.max(1, data.max);
    var html = '<div class="heatmap-label"></div>';
    for (var h = 0; h < 24; h++) { html += '<div class="heatmap-hour-label">' + (h < 10 ? '0' : '') + h + '</div>'; }
    data.days.forEach(function(day) {
      html += '<div class="heatmap-label">' + day.label + '</div>';
      day.hours.forEach(function(val, hi) {
        var intensity = val / maxVal;
        var color;
        if (val === 0) color = '#12122a';
        else if (intensity < 0.25) color = '#1a3a2a';
        else if (intensity < 0.5) color = '#2a6a3a';
        else if (intensity < 0.75) color = '#4a9a2a';
        else color = '#6adb3a';
        html += '<div class="heatmap-cell" style="background:' + color + ';" title="' + day.label + ' ' + (hi < 10 ? '0' : '') + hi + ':00 — ' + val + ' events"></div>';
      });
    });
    grid.innerHTML = html;
    var legend = document.getElementById('heatmap-legend');
    legend.innerHTML = 'Less <div class="heatmap-legend-cell" style="background:#12122a"></div><div class="heatmap-legend-cell" style="background:#1a3a2a"></div><div class="heatmap-legend-cell" style="background:#2a6a3a"></div><div class="heatmap-legend-cell" style="background:#4a9a2a"></div><div class="heatmap-legend-cell" style="background:#6adb3a"></div> More';
  } catch(e) {
    document.getElementById('heatmap-grid').innerHTML = '<span style="color:#555">No activity data</span>';
  }
}

// ===== Usage / Token Tracking =====
async function loadUsage() {
  try {
    var data = await fetch('/api/usage').then(r => r.json());
    function fmtTokens(n) { return n >= 1000000 ? (n/1000000).toFixed(1) + 'M' : n >= 1000 ? (n/1000).toFixed(0) + 'K' : String(n); }
    function fmtCost(c) { return c >= 0.01 ? '$' + c.toFixed(2) : c > 0 ? '<$0.01' : '$0.00'; }
    document.getElementById('usage-today').textContent = fmtTokens(data.today);
    document.getElementById('usage-today-cost').textContent = '≈ ' + fmtCost(data.todayCost);
    document.getElementById('usage-week').textContent = fmtTokens(data.week);
    document.getElementById('usage-week-cost').textContent = '≈ ' + fmtCost(data.weekCost);
    document.getElementById('usage-month').textContent = fmtTokens(data.month);
    document.getElementById('usage-month-cost').textContent = '≈ ' + fmtCost(data.monthCost);
    
    // Display cost warnings
    displayCostWarnings(data.warnings || []);
    
    // Display trend analysis
    displayTrendAnalysis(data.trend || {});
    // Bar chart
    var maxTokens = Math.max.apply(null, data.days.map(function(d){return d.tokens;})) || 1;
    var chartHtml = '';
    data.days.forEach(function(d) {
      var pct = Math.max(1, (d.tokens / maxTokens) * 100);
      var label = d.date.substring(5);
      var val = d.tokens >= 1000 ? (d.tokens/1000).toFixed(0) + 'K' : d.tokens;
      chartHtml += '<div class="usage-bar-wrap"><div class="usage-bar" style="height:' + pct + '%"><div class="usage-bar-value">' + (d.tokens > 0 ? val : '') + '</div></div><div class="usage-bar-label">' + label + '</div></div>';
    });
    document.getElementById('usage-chart').innerHTML = chartHtml;
    // Cost table
    var costLabel = data.source === 'otlp' ? 'Cost' : 'Est. Cost';
    var tableHtml = '<thead><tr><th>Period</th><th>Tokens</th><th>' + costLabel + '</th></tr></thead><tbody>';
    tableHtml += '<tr><td>Today</td><td>' + fmtTokens(data.today) + '</td><td>' + fmtCost(data.todayCost) + '</td></tr>';
    tableHtml += '<tr><td>This Week</td><td>' + fmtTokens(data.week) + '</td><td>' + fmtCost(data.weekCost) + '</td></tr>';
    tableHtml += '<tr><td>This Month</td><td>' + fmtTokens(data.month) + '</td><td>' + fmtCost(data.monthCost) + '</td></tr>';
    tableHtml += '</tbody>';
    document.getElementById('usage-cost-table').innerHTML = tableHtml;
    // OTLP-specific sections
    var otelExtra = document.getElementById('otel-extra-sections');
    if (data.source === 'otlp') {
      otelExtra.style.display = '';
      var runEl = document.getElementById('usage-avg-run');
      if (runEl) runEl.textContent = data.avgRunMs > 0 ? (data.avgRunMs > 1000 ? (data.avgRunMs/1000).toFixed(1) + 's' : data.avgRunMs.toFixed(0) + 'ms') : '—';
      var msgEl = document.getElementById('usage-msg-count');
      if (msgEl) msgEl.textContent = data.messageCount || '0';
      // Model breakdown table
      if (data.modelBreakdown && data.modelBreakdown.length > 0) {
        var mHtml = '<thead><tr><th>Model</th><th>Tokens</th></tr></thead><tbody>';
        data.modelBreakdown.forEach(function(m) {
          mHtml += '<tr><td><span class="badge model">' + escHtml(m.model) + '</span></td><td>' + fmtTokens(m.tokens) + '</td></tr>';
        });
        mHtml += '</tbody>';
        document.getElementById('usage-model-table').innerHTML = mHtml;
      }
    } else {
      otelExtra.style.display = 'none';
    }
  } catch(e) {
    document.getElementById('usage-chart').innerHTML = '<span style="color:#555">No usage data available</span>';
  }
}

function displayCostWarnings(warnings) {
  var container = document.getElementById('cost-warnings');
  if (!warnings || warnings.length === 0) {
    container.style.display = 'none';
    return;
  }
  
  var html = '';
  warnings.forEach(function(w) {
    var icon = w.level === 'error' ? '🚨' : '⚠️';
    html += '<div class="cost-warning ' + w.level + '">';
    html += '<div class="cost-warning-icon">' + icon + '</div>';
    html += '<div class="cost-warning-message">' + escHtml(w.message) + '</div>';
    html += '</div>';
  });
  
  container.innerHTML = html;
  container.style.display = 'block';
}

function displayTrendAnalysis(trend) {
  var card = document.getElementById('trend-card');
  if (!trend || trend.trend === 'insufficient_data') {
    card.style.display = 'none';
    return;
  }
  
  var directionEl = document.getElementById('trend-direction');
  var predictionEl = document.getElementById('trend-prediction');
  
  var emoji = trend.trend === 'increasing' ? '📈' : trend.trend === 'decreasing' ? '📉' : '➡️';
  directionEl.textContent = emoji + ' ' + trend.trend.charAt(0).toUpperCase() + trend.trend.slice(1);
  
  if (trend.dailyAvg && trend.monthlyPrediction) {
    var dailyAvg = trend.dailyAvg >= 1000 ? (trend.dailyAvg/1000).toFixed(0) + 'K' : trend.dailyAvg;
    var monthlyPred = trend.monthlyPrediction >= 1000000 ? (trend.monthlyPrediction/1000000).toFixed(1) + 'M' : 
                      trend.monthlyPrediction >= 1000 ? (trend.monthlyPrediction/1000).toFixed(0) + 'K' : trend.monthlyPrediction;
    predictionEl.textContent = dailyAvg + '/day avg, ~' + monthlyPred + '/month projected';
  } else {
    predictionEl.textContent = 'Analyzing usage patterns...';
  }
  
  card.style.display = 'block';
}

function exportUsageData() {
  // Trigger CSV download
  window.open('/api/usage/export', '_blank');
}

// ===== Transcripts =====
async function loadTranscripts() {
  try {
    var data = await fetch('/api/transcripts').then(r => r.json());
    var html = '';
    data.transcripts.forEach(function(t) {
      html += '<div class="transcript-item" onclick="viewTranscript(\'' + escHtml(t.id) + '\')">';
      html += '<div><div class="transcript-name">' + escHtml(t.name) + '</div>';
      html += '<div class="transcript-meta-row">';
      html += '<span>' + t.messages + ' messages</span>';
      html += '<span>' + (t.size > 1024 ? (t.size/1024).toFixed(1) + ' KB' : t.size + ' B') + '</span>';
      html += '<span>' + timeAgo(t.modified) + '</span>';
      html += '</div></div>';
      html += '<span style="color:#444;font-size:18px;">▸</span>';
      html += '</div>';
    });
    document.getElementById('transcript-list').innerHTML = html || '<div style="padding:16px;color:#666;">No transcript files found</div>';
    document.getElementById('transcript-list').style.display = '';
    document.getElementById('transcript-viewer').style.display = 'none';
    document.getElementById('transcript-back-btn').style.display = 'none';
  } catch(e) {
    document.getElementById('transcript-list').innerHTML = '<div style="padding:16px;color:#666;">Failed to load transcripts</div>';
  }
}

function showTranscriptList() {
  document.getElementById('transcript-list').style.display = '';
  document.getElementById('transcript-viewer').style.display = 'none';
  document.getElementById('transcript-back-btn').style.display = 'none';
}

async function viewTranscript(sessionId) {
  document.getElementById('transcript-list').style.display = 'none';
  document.getElementById('transcript-viewer').style.display = '';
  document.getElementById('transcript-back-btn').style.display = '';
  document.getElementById('transcript-messages').innerHTML = '<div style="padding:20px;color:#666;">Loading transcript...</div>';
  try {
    var data = await fetch('/api/transcript/' + encodeURIComponent(sessionId)).then(r => r.json());
    // Metadata
    var metaHtml = '<div class="stat-row"><span class="stat-label">Session</span><span class="stat-val">' + escHtml(data.name) + '</span></div>';
    metaHtml += '<div class="stat-row"><span class="stat-label">Messages</span><span class="stat-val">' + data.messageCount + '</span></div>';
    if (data.model) metaHtml += '<div class="stat-row"><span class="stat-label">Model</span><span class="stat-val"><span class="badge model">' + escHtml(data.model) + '</span></span></div>';
    if (data.totalTokens) metaHtml += '<div class="stat-row"><span class="stat-label">Tokens</span><span class="stat-val"><span class="badge tokens">' + (data.totalTokens/1000).toFixed(0) + 'K</span></span></div>';
    if (data.duration) metaHtml += '<div class="stat-row"><span class="stat-label">Duration</span><span class="stat-val">' + data.duration + '</span></div>';
    document.getElementById('transcript-meta').innerHTML = metaHtml;
    // Messages
    var msgsHtml = '';
    data.messages.forEach(function(m, idx) {
      var role = m.role || 'unknown';
      var cls = role === 'user' ? 'user' : role === 'assistant' ? 'assistant' : role === 'system' ? 'system' : 'tool';
      var content = m.content || '';
      var needsTruncate = content.length > 800;
      var displayContent = needsTruncate ? content.substring(0, 800) : content;
      msgsHtml += '<div class="chat-msg ' + cls + '">';
      msgsHtml += '<div class="chat-role">' + escHtml(role) + '</div>';
      if (needsTruncate) {
        msgsHtml += '<div class="chat-content-truncated" id="msg-' + idx + '-short">' + escHtml(displayContent) + '</div>';
        msgsHtml += '<div id="msg-' + idx + '-full" style="display:none;white-space:pre-wrap;">' + escHtml(content) + '</div>';
        msgsHtml += '<div class="chat-expand" onclick="toggleMsg(' + idx + ')">Show more (' + content.length + ' chars)</div>';
      } else {
        msgsHtml += '<div style="white-space:pre-wrap;">' + escHtml(content) + '</div>';
      }
      if (m.timestamp) msgsHtml += '<div class="chat-ts">' + new Date(m.timestamp).toLocaleString() + '</div>';
      msgsHtml += '</div>';
    });
    document.getElementById('transcript-messages').innerHTML = msgsHtml || '<div style="color:#555;padding:16px;">No messages in this transcript</div>';
  } catch(e) {
    document.getElementById('transcript-messages').innerHTML = '<div style="color:#e74c3c;padding:16px;">Failed to load transcript</div>';
  }
}

function toggleMsg(idx) {
  var short = document.getElementById('msg-' + idx + '-short');
  var full = document.getElementById('msg-' + idx + '-full');
  if (short.style.display === 'none') {
    short.style.display = '';
    full.style.display = 'none';
    short.nextElementSibling.nextElementSibling.textContent = 'Show more';
  } else {
    short.style.display = 'none';
    full.style.display = '';
    event.target.textContent = 'Show less';
  }
}

loadAll();
setInterval(loadAll, 10000);

// Real-time log stream via SSE
var logStream = null;
var streamBuffer = [];
var MAX_STREAM_LINES = 500;

function startLogStream() {
  if (logStream) logStream.close();
  streamBuffer = [];
  logStream = new EventSource('/api/logs-stream');
  logStream.onmessage = function(e) {
    var data = JSON.parse(e.data);
    streamBuffer.push(data.line);
    if (streamBuffer.length > MAX_STREAM_LINES) streamBuffer.shift();
    appendLogLine('ov-logs', data.line);
    appendLogLine('logs-full', data.line);
    processFlowEvent(data.line);
    document.getElementById('refresh-time').textContent = 'Live • ' + new Date().toLocaleTimeString();
  };
  logStream.onerror = function() {
    setTimeout(startLogStream, 5000);
  };
}

function parseLogLine(line) {
  try {
    var obj = JSON.parse(line);
    var ts = '';
    if (obj.time || (obj._meta && obj._meta.date)) {
      var d = new Date(obj.time || obj._meta.date);
      ts = d.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
    }
    var level = (obj.logLevelName || obj.level || 'info').toLowerCase();
    var cls = 'info';
    if (level === 'error' || level === 'fatal') cls = 'err';
    else if (level === 'warn' || level === 'warning') cls = 'warn';
    else if (level === 'debug') cls = 'msg';
    var msg = obj.msg || obj.message || obj.name || '';
    var extras = [];
    if (obj["0"]) extras.push(obj["0"]);
    if (obj["1"]) extras.push(obj["1"]);
    var display;
    if (msg && extras.length) display = msg + ' | ' + extras.join(' ');
    else if (extras.length) display = extras.join(' ');
    else if (!msg) display = line.substring(0, 200);
    else display = msg;
    if (ts) display = '<span class="ts">' + ts + '</span> ' + escHtml(display);
    else display = escHtml(display);
    return {cls: cls, html: display};
  } catch(e) {
    var cls = 'msg';
    if (line.includes('Error') || line.includes('failed')) cls = 'err';
    else if (line.includes('WARN')) cls = 'warn';
    else if (line.includes('run start') || line.includes('inbound')) cls = 'info';
    return {cls: cls, html: escHtml(line.substring(0, 300))};
  }
}

function appendLogLine(elId, line) {
  var el = document.getElementById(elId);
  if (!el) return;
  var parsed = parseLogLine(line);
  var div = document.createElement('div');
  div.className = 'log-line';
  div.innerHTML = '<span class="' + parsed.cls + '">' + parsed.html + '</span>';
  el.appendChild(div);
  while (el.children.length > MAX_STREAM_LINES) el.removeChild(el.firstChild);
  if (el.scrollHeight - el.scrollTop - el.clientHeight < 150) {
    el.scrollTop = el.scrollHeight;
  }
}

startLogStream();

// ===== Flow Visualization Engine =====
var flowStats = { messages: 0, events: 0, activeTools: {}, msgTimestamps: [] };
var flowInitDone = false;

function initFlow() {
  if (flowInitDone) return;
  flowInitDone = true;
  
  // Performance: Reduce update frequency on mobile
  var updateInterval = window.innerWidth < 768 ? 3000 : 2000;
  
  fetch('/api/overview').then(function(r){return r.json();}).then(function(d) {
    var el = document.getElementById('brain-model-text');
    if (el && d.model) el.textContent = d.model;
    var label = document.getElementById('brain-model-label');
    if (label && d.model) {
      var short = d.model.split('/').pop().split('-').slice(0,2).join(' ');
      label.textContent = short.charAt(0).toUpperCase() + short.slice(1);
    }
    var tok = document.getElementById('flow-tokens');
    if (tok) tok.textContent = (d.mainTokens / 1000).toFixed(0) + 'K';
    
    // Add visual hierarchy hints
    setTimeout(function() {
      enhanceArchitectureClarity();
    }, 1000);
  }).catch(function(){});
  
  setInterval(updateFlowStats, updateInterval);
}

// Add subtle animation to help users understand the flow
function enhanceArchitectureClarity() {
  // Gentle pulse on key nodes to show importance hierarchy
  var keyNodes = ['node-human', 'node-gateway', 'node-brain'];
  keyNodes.forEach(function(nodeId, index) {
    setTimeout(function() {
      var node = document.getElementById(nodeId);
      if (node) {
        node.style.animation = 'none';
        setTimeout(function() {
          node.style.animation = '';
        }, 100);
      }
    }, index * 800);
  });
  
  // Highlight the main message flow path briefly
  var paths = ['path-human-tg', 'path-tg-gw', 'path-gw-brain'];
  paths.forEach(function(pathId, index) {
    setTimeout(function() {
      var path = document.getElementById(pathId);
      if (path) {
        path.style.opacity = '0.8';
        path.style.strokeWidth = '3';
        path.style.transition = 'all 0.5s ease';
        setTimeout(function() {
          path.style.opacity = '';
          path.style.strokeWidth = '';
        }, 1500);
      }
    }, index * 200);
  });
}

function updateFlowStats() {
  var now = Date.now();
  flowStats.msgTimestamps = flowStats.msgTimestamps.filter(function(t){return now - t < 60000;});
  var el1 = document.getElementById('flow-msg-rate');
  if (el1) el1.textContent = flowStats.msgTimestamps.length;
  var el2 = document.getElementById('flow-event-count');
  if (el2) el2.textContent = flowStats.events;
  var names = Object.keys(flowStats.activeTools).filter(function(k){return flowStats.activeTools[k];});
  var el3 = document.getElementById('flow-active-tools');
  if (el3) el3.textContent = names.length > 0 ? names.join(', ') : '\u2014';
  if (flowStats.events % 15 === 0) {
    fetch('/api/overview').then(function(r){return r.json();}).then(function(d) {
      var tok = document.getElementById('flow-tokens');
      if (tok) tok.textContent = (d.mainTokens / 1000).toFixed(0) + 'K';
    }).catch(function(){});
  }
}

// Enhanced particle animation with performance optimizations and better mobile support
var particlePool = [];
var trailPool = [];
var maxParticles = window.innerWidth < 768 ? 3 : 8; // Limit particles on mobile
var trailInterval = window.innerWidth < 768 ? 8 : 4; // Fewer trails on mobile

function getPooledParticle(isTrail) {
  var pool = isTrail ? trailPool : particlePool;
  if (pool.length > 0) return pool.pop();
  var elem = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  elem.setAttribute('r', isTrail ? '2' : '5');
  return elem;
}

function returnToPool(elem, isTrail) {
  var pool = isTrail ? trailPool : particlePool;
  elem.style.opacity = '0';
  elem.style.transform = '';
  if (pool.length < 20) pool.push(elem); // Pool size limit
  else if (elem.parentNode) elem.parentNode.removeChild(elem);
}

function animateParticle(pathId, color, duration, reverse) {
  var path = document.getElementById(pathId);
  if (!path) return;
  var svg = document.getElementById('flow-svg');
  if (!svg) return;
  
  // Skip if too many particles (performance)
  var activeParticles = svg.querySelectorAll('circle[data-particle]').length;
  if (activeParticles > maxParticles) return;
  
  var len = path.getTotalLength();
  var particle = getPooledParticle(false);
  particle.setAttribute('data-particle', 'true');
  particle.setAttribute('fill', color);
  particle.style.filter = 'drop-shadow(0 0 8px ' + color + ')';
  particle.style.opacity = '1';
  svg.appendChild(particle);
  
  var glowCls = color === '#60a0ff' ? 'glow-blue' : color === '#f0c040' ? 'glow-yellow' : color === '#50e080' ? 'glow-green' : color === '#40a0b0' ? 'glow-cyan' : color === '#c0a0ff' ? 'glow-purple' : 'glow-red';
  path.classList.add(glowCls);
  
  var startT = performance.now();
  var trailN = 0;
  var trailElements = [];
  
  function step(now) {
    var t = Math.min((now - startT) / duration, 1);
    var dist = reverse ? (1 - t) * len : t * len;
    
    try {
      var pt = path.getPointAtLength(dist);
      particle.setAttribute('cx', pt.x);
      particle.setAttribute('cy', pt.y);
    } catch(e) { 
      cleanup();
      return; 
    }
    
    // Create trail less frequently, and only if not too many already
    if (trailN++ % trailInterval === 0 && trailElements.length < 6) {
      var tr = getPooledParticle(true);
      tr.setAttribute('cx', particle.getAttribute('cx'));
      tr.setAttribute('cy', particle.getAttribute('cy'));
      tr.setAttribute('fill', color);
      tr.style.opacity = '0.6';
      tr.style.filter = 'blur(0.5px)';
      svg.insertBefore(tr, particle);
      trailElements.push(tr);
      
      // Fade trail with CSS transition instead of JS animation
      setTimeout(function() {
        tr.style.transition = 'opacity 400ms ease-out, transform 400ms ease-out';
        tr.style.opacity = '0';
        tr.style.transform = 'scale(0.3)';
        setTimeout(function() { 
          if (tr.parentNode) tr.parentNode.removeChild(tr);
          returnToPool(tr, true);
        }, 450);
      }, 50);
    }
    
    if (t < 1) {
      requestAnimationFrame(step);
    } else {
      cleanup();
    }
  }
  
  function cleanup() {
    if (particle.parentNode) particle.parentNode.removeChild(particle);
    returnToPool(particle, false);
    setTimeout(function() { 
      path.classList.remove(glowCls); 
    }, 400);
  }
  
  requestAnimationFrame(step);
}

function highlightNode(nodeId, dur) {
  var node = document.getElementById(nodeId);
  if (!node) return;
  node.classList.add('active');
  setTimeout(function() { node.classList.remove('active'); }, dur || 2000);
}

function triggerInbound(ch) {
  ch = ch || 'tg';
  var chNodeId = ch === 'tg' ? 'node-telegram' : ch === 'sig' ? 'node-signal' : 'node-whatsapp';
  highlightNode(chNodeId, 3000);
  animateParticle('path-human-' + ch, '#c0a0ff', 550, false);
  highlightNode('node-human', 2200);
  setTimeout(function() {
    animateParticle('path-' + ch + '-gw', '#60a0ff', 800, false);
    highlightNode('node-gateway', 2000);
  }, 400);
  setTimeout(function() {
    animateParticle('path-gw-brain', '#60a0ff', 600, false);
    highlightNode('node-brain', 2500);
  }, 1050);
  setTimeout(function() {
    animateParticle('path-brain-session', '#60a0ff', 400, false);
    highlightNode('node-session', 1500);
  }, 1550);
  setTimeout(function() { triggerInfraNetwork(); }, 300);
}

function triggerToolCall(toolName) {
  var pathId = 'path-brain-' + toolName;
  animateParticle(pathId, '#f0c040', 700, false);
  highlightNode('node-' + toolName, 2500);
  setTimeout(function() {
    animateParticle(pathId, '#f0c040', 700, true);
  }, 900);
  var ind = document.getElementById('ind-' + toolName);
  if (ind) { ind.classList.add('active'); setTimeout(function() { ind.classList.remove('active'); }, 4000); }
  flowStats.activeTools[toolName] = true;
  setTimeout(function() { delete flowStats.activeTools[toolName]; }, 5000);
  if (toolName === 'exec') {
    setTimeout(function() { triggerInfraMachine(); triggerInfraRuntime(); }, 400);
  } else if (toolName === 'browser' || toolName === 'search') {
    setTimeout(function() { triggerInfraNetwork(); }, 400);
  } else if (toolName === 'memory') {
    setTimeout(function() { triggerInfraStorage(); }, 400);
  }
}

function triggerOutbound(ch) {
  ch = ch || 'tg';
  animateParticle('path-gw-brain', '#50e080', 600, true);
  highlightNode('node-gateway', 2000);
  setTimeout(function() {
    animateParticle('path-' + ch + '-gw', '#50e080', 800, true);
  }, 500);
  setTimeout(function() {
    animateParticle('path-human-' + ch, '#50e080', 550, true);
    highlightNode('node-human', 1800);
  }, 1200);
  setTimeout(function() { triggerInfraNetwork(); }, 200);
}

function triggerError() {
  var brain = document.getElementById('node-brain');
  if (!brain) return;
  var r = brain.querySelector('rect');
  if (r) { r.style.stroke = '#e04040'; setTimeout(function() { r.style.stroke = '#f0c040'; }, 2500); }
}

function triggerInfraNetwork() {
  animateParticle('path-gw-network', '#40a0b0', 1200, false);
  highlightNode('node-network', 2500);
}
function triggerInfraRuntime() {
  animateParticle('path-brain-runtime', '#40a0b0', 1000, false);
  highlightNode('node-runtime', 2200);
}
function triggerInfraMachine() {
  animateParticle('path-brain-machine', '#40a0b0', 1000, false);
  highlightNode('node-machine', 2200);
}
function triggerInfraStorage() {
  animateParticle('path-memory-storage', '#40a0b0', 700, false);
  highlightNode('node-storage', 2000);
}

// Live feed for Flow tab — shows recent events in plain English
var _flowFeedItems = [];
var _flowFeedMax = 30;
function addFlowFeedItem(text, color) {
  var now = new Date();
  var time = now.toLocaleTimeString('en-GB', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  _flowFeedItems.push({time: time, text: text, color: color || '#888'});
  if (_flowFeedItems.length > _flowFeedMax) _flowFeedItems.shift();
  var el = document.getElementById('flow-live-feed');
  if (!el) return;
  var html = '';
  for (var i = _flowFeedItems.length - 1; i >= Math.max(0, _flowFeedItems.length - 15); i--) {
    var item = _flowFeedItems[i];
    html += '<div><span style="color:#555;">' + item.time + '</span> <span style="color:' + item.color + ';">' + item.text + '</span></div>';
  }
  el.innerHTML = html;
  var countEl = document.getElementById('flow-feed-count');
  if (countEl) countEl.textContent = flowStats.events + ' events';
}

var flowThrottles = {};
function processFlowEvent(line) {
  flowStats.events++;
  var now = Date.now();
  var msg = '', level = '';
  try {
    var obj = JSON.parse(line);
    msg = ((obj.msg || '') + ' ' + (obj.message || '') + ' ' + (obj.name || '') + ' ' + (obj['0'] || '') + ' ' + (obj['1'] || '')).toLowerCase();
    level = (obj.logLevelName || obj.level || (obj._meta && obj._meta.logLevelName) || '').toLowerCase();
  } catch(e) { msg = line.toLowerCase(); }

  if (level === 'error' || level === 'fatal') { triggerError(); return; }

  if (msg.includes('run start') && msg.includes('messagechannel')) {
    if (now - (flowThrottles['inbound']||0) < 500) return;
    flowThrottles['inbound'] = now;
    var ch = 'tg';
    if (msg.includes('signal')) ch = 'sig';
    else if (msg.includes('whatsapp')) ch = 'wa';
    triggerInbound(ch);
    addFlowFeedItem('📨 New message arrived via ' + (ch === 'tg' ? 'Telegram' : ch === 'wa' ? 'WhatsApp' : 'Signal'), '#c0a0ff');
    flowStats.msgTimestamps.push(now);
    return;
  }
  if (msg.includes('inbound') || msg.includes('dispatching') || msg.includes('message received')) {
    triggerInbound('tg');
    addFlowFeedItem('📨 Incoming message received', '#c0a0ff');
    flowStats.msgTimestamps.push(now);
    return;
  }

  if ((msg.includes('tool start') || msg.includes('tool-call') || msg.includes('tool_use')) && !msg.includes('tool end')) {
    var toolName = '';
    var toolMatch = msg.match(/tool=(\w+)/);
    if (toolMatch) toolName = toolMatch[1].toLowerCase();
    var flowTool = 'exec';
    if (toolName === 'exec' || toolName === 'read' || toolName === 'write' || toolName === 'edit' || toolName === 'process') {
      flowTool = 'exec';
    } else if (toolName.includes('browser') || toolName === 'canvas') {
      flowTool = 'browser';
    } else if (toolName === 'web_search' || toolName === 'web_fetch') {
      flowTool = 'search';
    } else if (toolName === 'cron' || toolName === 'sessions_spawn' || toolName === 'sessions_send') {
      flowTool = 'cron';
    } else if (toolName === 'tts') {
      flowTool = 'tts';
    } else if (toolName === 'memory_search' || toolName === 'memory_get') {
      flowTool = 'memory';
    } else if (toolName === 'message') {
      if (now - (flowThrottles['outbound']||0) < 500) return;
      flowThrottles['outbound'] = now;
      triggerOutbound('tg'); return;
    }
    if (now - (flowThrottles['tool-'+flowTool]||0) < 300) return;
    flowThrottles['tool-'+flowTool] = now;
    var toolNames = {exec:'running a command',browser:'browsing the web',search:'searching the web',cron:'scheduling a task',tts:'generating speech',memory:'accessing memory'};
    addFlowFeedItem('⚡ AI is ' + (toolNames[flowTool] || 'using ' + flowTool), '#f0c040');
    triggerToolCall(flowTool); return;
  }

  var toolMap = {
    'exec': ['exec','shell','command'],
    'browser': ['browser','screenshot','snapshot'],
    'search': ['web_search','web_fetch'],
    'cron': ['cron','schedule'],
    'tts': ['tts','speech','voice'],
    'memory': ['memory_search','memory_get']
  };
  if (msg.includes('tool') || msg.includes('invoke') || msg.includes('calling')) {
    for (var t in toolMap) {
      for (var i = 0; i < toolMap[t].length; i++) {
        if (msg.includes(toolMap[t][i])) { triggerToolCall(t); return; }
      }
    }
  }

  if (msg.includes('response sent') || msg.includes('completion') || msg.includes('reply sent') || msg.includes('deliver') || (msg.includes('lane task done') && msg.includes('main'))) {
    var ch = 'tg';
    if (msg.includes('signal')) ch = 'sig';
    else if (msg.includes('whatsapp')) ch = 'wa';
    addFlowFeedItem('✉️ AI sent a reply via ' + (ch === 'tg' ? 'Telegram' : ch === 'wa' ? 'WhatsApp' : 'Signal'), '#50e080');
    triggerOutbound(ch);
    return;
  }

  // Catch embedded run lifecycle events
  if (msg.includes('embedded run start') && !msg.includes('tool') && !msg.includes('prompt') && !msg.includes('agent')) {
    if (now - (flowThrottles['run-start']||0) < 1000) return;
    flowThrottles['run-start'] = now;
    var ch = 'tg';
    if (msg.includes('messagechannel=signal')) ch = 'sig';
    else if (msg.includes('messagechannel=whatsapp')) ch = 'wa';
    else if (msg.includes('messagechannel=heartbeat')) { addFlowFeedItem('💓 Heartbeat run started', '#4a7090'); return; }
    addFlowFeedItem('🧠 AI run started (' + (ch === 'tg' ? 'Telegram' : ch === 'wa' ? 'WhatsApp' : 'Signal') + ')', '#a080f0');
    triggerInbound(ch);
    flowStats.msgTimestamps.push(now);
    return;
  }
  if (msg.includes('embedded run agent end') || msg.includes('embedded run prompt end')) {
    if (now - (flowThrottles['run-end']||0) < 1000) return;
    flowThrottles['run-end'] = now;
    addFlowFeedItem('✅ AI processing complete', '#50e080');
    return;
  }
  if (msg.includes('session state') && msg.includes('new=processing')) {
    if (now - (flowThrottles['session-active']||0) < 2000) return;
    flowThrottles['session-active'] = now;
    addFlowFeedItem('⚡ Session activated', '#f0c040');
    return;
  }
  if (msg.includes('lane enqueue') && msg.includes('main')) {
    if (now - (flowThrottles['lane']||0) < 2000) return;
    flowThrottles['lane'] = now;
    addFlowFeedItem('📥 Task queued', '#8090b0');
    return;
  }
  if (msg.includes('tool end') || msg.includes('tool_end')) {
    if (now - (flowThrottles['tool-end']||0) < 300) return;
    flowThrottles['tool-end'] = now;
    addFlowFeedItem('✔️ Tool completed', '#50c070');
    return;
  }
}

// === Overview Split-Screen: Clone flow SVG into overview pane ===
function initOverviewFlow() {
  var srcSvg = document.getElementById('flow-svg');
  var container = document.getElementById('overview-flow-container');
  if (!srcSvg || !container) return;
  // Clone the SVG into the overview pane
  var clone = srcSvg.cloneNode(true);
  clone.id = 'overview-flow-svg';
  clone.style.width = '100%';
  clone.style.height = '100%';
  clone.style.minWidth = '0';
  // Rename filter IDs in clone to avoid duplicate-id conflicts with original SVG
  var filters = clone.querySelectorAll('filter[id]');
  filters.forEach(function(f) {
    var oldId = f.id;
    var newId = 'ov-' + oldId;
    f.id = newId;
    clone.querySelectorAll('[filter="url(#' + oldId + ')"]').forEach(function(el) {
      el.setAttribute('filter', 'url(#' + newId + ')');
    });
  });
  container.innerHTML = '';
  container.appendChild(clone);
}

// === Overview Tasks Panel (right side) ===
var _ovTasksTimer = null;
window._ovExpandedSet = {};  // track which detail panels are open across refreshes

function _ovTimeLabel(agent) {
  var ms = agent.runtimeMs || 0;
  var sec = Math.floor(ms / 1000);
  var min = Math.floor(sec / 60);
  var hr = Math.floor(min / 60);
  if (agent.status === 'active') {
    if (min < 1) return 'Running (' + sec + 's)';
    if (min < 60) return 'Running (' + min + ' min)';
    return 'Running (' + hr + 'h ' + (min % 60) + 'm)';
  }
  if (sec < 60) return 'Finished ' + sec + 's ago';
  if (min < 60) return 'Finished ' + min + ' min ago';
  if (hr < 24) return 'Finished ' + hr + 'h ago';
  return 'Finished ' + Math.floor(hr / 24) + 'd ago';
}

function _ovRenderCard(agent, idx) {
  var isRealFailure = agent.status === 'stale' && agent.abortedLastRun && (agent.outputTokens || 0) === 0;
  var sc = agent.status === 'active' ? 'running' : isRealFailure ? 'failed' : 'complete';
  var taskName = cleanTaskName(agent.displayName);
  var badge = detectProjectBadge(agent.displayName);
  var timeLabel = _ovTimeLabel(agent);
  var detailId = 'ovd2-' + idx;
  var isOpen = !!(window._ovExpandedSet || {})[agent.sessionId];
  var tokTotal = (agent.inputTokens || 0) + (agent.outputTokens || 0);
  var cmdsRun = (agent.recentTools || []).length;

  var h = '';
  // Card with left color bar (via border-left on ov-task-card class)
  h += '<div class="ov-task-card ' + sc + '" style="cursor:pointer;" onclick="openTaskModal(\'' + escHtml(agent.sessionId).replace(/'/g,"\\'") + '\',\'' + escHtml(taskName).replace(/'/g,"\\'") + '\',\'' + escHtml(agent.key).replace(/'/g,"\\'") + '\')">';
  // Row 1: status dot + name + status badge
  h += '<div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:6px;">';
  h += '<span class="status-dot ' + sc + '" style="margin-top:5px;"></span>';
  h += '<div style="flex:1;min-width:0;">';
  h += '<div style="font-weight:700;font-size:14px;color:var(--text-primary);line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;">' + escHtml(taskName) + '</div>';
  // Row 2: project pill + time
  h += '<div style="display:flex;align-items:center;gap:8px;margin-top:4px;flex-wrap:wrap;">';
  if (badge) {
    h += '<span style="display:inline-block;padding:1px 8px;border-radius:10px;font-size:10px;font-weight:700;background:' + badge.color + '18;color:' + badge.color + ';border:1px solid ' + badge.color + '33;">' + badge.label + '</span>';
  }
  h += '<span style="font-size:12px;color:var(--text-muted);">' + escHtml(timeLabel) + '</span>';
  h += '</div>';
  h += '</div>';
  // Status badge top-right
  h += '<span class="task-card-badge ' + sc + '" style="flex-shrink:0;">' + (sc === 'running' ? '🔄' : sc === 'failed' ? '❌' : '✅') + '</span>';
  h += '</div>';
  // Row 3: Show details toggle
  h += '<button class="ov-toggle-btn" onclick="event.stopPropagation();var d=document.getElementById(\'' + detailId + '\');var o=d.classList.toggle(\'open\');this.textContent=o?\'▼ Hide details\':\'▶ Show details\';if(o){window._ovExpandedSet=window._ovExpandedSet||{};window._ovExpandedSet[\'' + escHtml(agent.sessionId) + '\']=true;}else{delete window._ovExpandedSet[\'' + escHtml(agent.sessionId) + '\'];}">' + (isOpen ? '▼ Hide details' : '▶ Show details') + '</button>';
  // Collapsible details
  h += '<div class="ov-details' + (isOpen ? ' open' : '') + '" id="' + detailId + '">';
  h += '<div><span style="color:var(--text-muted);">Session:</span> <span style="font-family:monospace;font-size:10px;">' + escHtml(agent.sessionId) + '</span></div>';
  h += '<div><span style="color:var(--text-muted);">Key:</span> <span style="font-family:monospace;font-size:10px;">' + escHtml(agent.key) + '</span></div>';
  h += '<div><span style="color:var(--text-muted);">Model:</span> ' + escHtml(agent.model || 'unknown') + '</div>';
  if (tokTotal > 0) h += '<div><span style="color:var(--text-muted);">Tokens:</span> ' + tokTotal.toLocaleString() + ' (' + (agent.inputTokens||0).toLocaleString() + ' in / ' + (agent.outputTokens||0).toLocaleString() + ' out)</div>';
  if (cmdsRun > 0) h += '<div><span style="color:var(--text-muted);">Commands run:</span> ' + cmdsRun + '</div>';
  if (agent.recentTools && agent.recentTools.length > 0) {
    h += '<div style="margin-top:6px;"><span style="color:var(--text-muted);">Recent tools:</span></div>';
    agent.recentTools.forEach(function(t) {
      h += '<div style="font-size:10px;font-family:monospace;"><span style="color:var(--text-accent);">' + escHtml(t.name) + '</span> ' + escHtml(t.summary) + '</div>';
    });
  }
  h += '<div style="margin-top:6px;"><span style="color:var(--text-muted);">Full prompt:</span></div>';
  h += '<div style="white-space:pre-wrap;word-break:break-word;max-height:120px;overflow-y:auto;padding:6px;background:var(--bg-primary);border-radius:4px;margin-top:2px;font-size:10px;">' + escHtml(agent.displayName) + '</div>';
  h += '</div>';
  h += '</div>';
  return h;
}

async function loadOverviewTasks() {
  try {
    var data = await fetch('/api/subagents').then(function(r){return r.json();});
    var el = document.getElementById('overview-tasks-list');
    var countBadge = document.getElementById('overview-tasks-count-badge');
    if (!el) return;
    var agents = data.subagents || [];

    // Also load into hidden active-tasks-grid for compatibility
    loadActiveTasks();

    if (agents.length === 0) {
      if (countBadge) countBadge.textContent = '';
      el.innerHTML = '<div style="text-align:center;padding:40px 20px;color:var(--text-muted);">'
        + '<div style="font-size:32px;margin-bottom:12px;" class="tasks-empty-icon">😴</div>'
        + '<div style="font-size:14px;font-weight:600;color:var(--text-tertiary);margin-bottom:4px;">No active tasks</div>'
        + '<div style="font-size:12px;">The AI is idle.</div></div>';
      return;
    }

    var running = [], done = [], failed = [];
    agents.forEach(function(a) {
      var isRealFailure = a.status === 'stale' && a.abortedLastRun && (a.outputTokens || 0) === 0;
      if (a.status === 'active') running.push(a);
      else if (isRealFailure) failed.push(a);
      else done.push(a);
    });
    // Filter old completed/failed (2h)
    done = done.filter(function(a) { return a.runtimeMs < 2 * 60 * 60 * 1000; });
    failed = failed.filter(function(a) { return a.runtimeMs < 2 * 60 * 60 * 1000; });

    if (countBadge) countBadge.textContent = running.length > 0 ? '(' + running.length + ' running)' : '(' + (done.length + failed.length) + ' recent)';

    var totalShown = running.length + done.length + failed.length;
    if (totalShown === 0) {
      el.innerHTML = '<div style="text-align:center;padding:40px 20px;color:var(--text-muted);">'
        + '<div style="font-size:32px;margin-bottom:12px;" class="tasks-empty-icon">😴</div>'
        + '<div style="font-size:14px;font-weight:600;color:var(--text-tertiary);margin-bottom:4px;">No active tasks</div>'
        + '<div style="font-size:12px;">The AI is idle.</div></div>';
      return;
    }

    var html = '';
    var cardIdx = 0;
    if (running.length > 0) {
      html += '<div class="task-group-header">🔄 Running (' + running.length + ')</div>';
      running.forEach(function(a) { html += _ovRenderCard(a, cardIdx++); });
    }
    if (done.length > 0) {
      html += '<div class="task-group-header">✅ Recently Completed (' + done.length + ')</div>';
      done.forEach(function(a) { html += _ovRenderCard(a, cardIdx++); });
    }
    if (failed.length > 0) {
      html += '<div class="task-group-header">❌ Failed (' + failed.length + ')</div>';
      failed.forEach(function(a) { html += _ovRenderCard(a, cardIdx++); });
    }

    // Preserve scroll position for smooth update
    var scrollTop = el.scrollTop;
    el.innerHTML = html;
    el.scrollTop = scrollTop;
  } catch(e) {}
}

function startOverviewTasksRefresh() {
  loadOverviewTasks();
  if (_ovTasksTimer) clearInterval(_ovTasksTimer);
  _ovTasksTimer = setInterval(loadOverviewTasks, 10000);
}

// === Task Detail Modal ===
var _modalSessionId = null;
var _modalTab = 'summary';
var _modalAutoRefresh = true;
var _modalRefreshTimer = null;
var _modalEvents = [];

function openTaskModal(sessionId, taskName, sessionKey) {
  _modalSessionId = sessionId;
  document.getElementById('modal-title').textContent = taskName || sessionId;
  document.getElementById('modal-session-key').textContent = sessionKey || sessionId;
  document.getElementById('task-modal-overlay').classList.add('open');
  document.getElementById('modal-content').innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted);">Loading transcript...</div>';
  _modalTab = 'summary';
  document.querySelectorAll('.modal-tab').forEach(function(t,i){t.classList.toggle('active',i===0);});
  loadModalTranscript();
  if (_modalAutoRefresh) {
    _modalRefreshTimer = setInterval(loadModalTranscript, 4000);
  }
  document.addEventListener('keydown', _modalEscHandler);
}

function closeTaskModal() {
  document.getElementById('task-modal-overlay').classList.remove('open');
  _modalSessionId = null;
  if (_modalRefreshTimer) { clearInterval(_modalRefreshTimer); _modalRefreshTimer = null; }
  document.removeEventListener('keydown', _modalEscHandler);
}

function _modalEscHandler(e) { if (e.key === 'Escape') closeTaskModal(); }

function toggleModalAutoRefresh() {
  _modalAutoRefresh = document.getElementById('modal-auto-refresh-cb').checked;
  if (_modalRefreshTimer) { clearInterval(_modalRefreshTimer); _modalRefreshTimer = null; }
  if (_modalAutoRefresh && _modalSessionId) {
    _modalRefreshTimer = setInterval(loadModalTranscript, 4000);
  }
}

function switchModalTab(tab) {
  _modalTab = tab;
  document.querySelectorAll('.modal-tab').forEach(function(t){ t.classList.toggle('active', t.textContent.toLowerCase().indexOf(tab) >= 0 || (tab==='full' && t.textContent==='Full Logs')); });
  renderModalContent();
}

async function loadModalTranscript() {
  if (!_modalSessionId) return;
  try {
    var r = await fetch('/api/transcript-events/' + encodeURIComponent(_modalSessionId));
    var data = await r.json();
    if (data.error) {
      document.getElementById('modal-content').innerHTML = '<div style="padding:20px;color:var(--text-error);">Error: ' + escHtml(data.error) + '</div>';
      return;
    }
    _modalEvents = data.events || [];
    document.getElementById('modal-event-count').textContent = '📊 ' + _modalEvents.length + ' events';
    document.getElementById('modal-msg-count').textContent = '💬 ' + (data.messageCount || 0) + ' messages';
    renderModalContent();
  } catch(e) {
    document.getElementById('modal-content').innerHTML = '<div style="padding:20px;color:var(--text-error);">Failed to load transcript</div>';
  }
}

function renderModalContent() {
  var el = document.getElementById('modal-content');
  if (_modalTab === 'summary') renderModalSummary(el);
  else if (_modalTab === 'narrative') renderModalNarrative(el);
  else renderModalFull(el);
}

function renderModalSummary(el) {
  var events = _modalEvents;
  // Find first user message as task description
  var desc = '';
  var result = '';
  for (var i = 0; i < events.length; i++) {
    if (events[i].type === 'user' && !desc) {
      desc = events[i].text || '';
      if (desc.length > 500) desc = desc.substring(0, 500) + '...';
    }
  }
  // Find last assistant text as result
  for (var i = events.length - 1; i >= 0; i--) {
    if (events[i].type === 'agent' && events[i].text) {
      result = events[i].text;
      if (result.length > 1000) result = result.substring(0, 1000) + '...';
      break;
    }
  }
  var html = '';
  html += '<div class="summary-section"><div class="summary-label">Task Description</div>';
  html += '<div class="summary-text">' + escHtml(desc || 'No description found') + '</div></div>';
  html += '<div class="summary-section"><div class="summary-label">Final Result / Output</div>';
  html += '<div class="summary-text">' + escHtml(result || 'No result yet...') + '</div></div>';
  el.innerHTML = html;
}

function renderModalNarrative(el) {
  var events = _modalEvents;
  var html = '';
  events.forEach(function(evt) {
    var icon = '', text = '';
    if (evt.type === 'user') {
      icon = '👤'; text = 'User sent: <code>' + escHtml((evt.text||'').substring(0, 150)) + '</code>';
    } else if (evt.type === 'agent') {
      icon = '🤖'; text = 'Agent said: <code>' + escHtml((evt.text||'').substring(0, 200)) + '</code>';
    } else if (evt.type === 'thinking') {
      icon = '💭'; text = 'Agent thought about the problem...';
    } else if (evt.type === 'exec') {
      icon = '⚡'; text = 'Ran command: <code>' + escHtml(evt.command||'') + '</code>';
    } else if (evt.type === 'read') {
      icon = '📖'; text = 'Read file: <code>' + escHtml(evt.file||'') + '</code>';
    } else if (evt.type === 'tool') {
      icon = '🔧'; text = 'Called tool: <code>' + escHtml(evt.toolName||'') + '</code>';
    } else if (evt.type === 'result') {
      icon = '🔍'; text = 'Got result (' + (evt.text||'').length + ' chars)';
    } else return;
    html += '<div class="narrative-item"><span class="narr-icon">' + icon + '</span>' + text + '</div>';
  });
  el.innerHTML = html || '<div style="padding:20px;color:var(--text-muted);">No events yet</div>';
}

function renderModalFull(el) {
  var events = _modalEvents;
  var html = '';
  events.forEach(function(evt, idx) {
    var icon = '📝', typeClass = '', summary = '', body = '';
    var ts = evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString() : '';
    if (evt.type === 'agent') {
      icon = '🤖'; typeClass = 'type-agent';
      summary = '<strong>Agent</strong> — ' + escHtml((evt.text||'').substring(0, 120));
      body = evt.text || '';
    } else if (evt.type === 'thinking') {
      icon = '💭'; typeClass = 'type-thinking';
      summary = '<strong>Thinking</strong> — ' + escHtml((evt.text||'').substring(0, 120));
      body = evt.text || '';
    } else if (evt.type === 'user') {
      icon = '👤'; typeClass = 'type-user';
      summary = '<strong>User</strong> — ' + escHtml((evt.text||'').substring(0, 120));
      body = evt.text || '';
    } else if (evt.type === 'exec') {
      icon = '⚡'; typeClass = 'type-exec';
      summary = '<strong>EXEC</strong> — <code>' + escHtml(evt.command||'') + '</code>';
      body = evt.command || '';
    } else if (evt.type === 'read') {
      icon = '📖'; typeClass = 'type-read';
      summary = '<strong>READ</strong> — ' + escHtml(evt.file||'');
      body = evt.file || '';
    } else if (evt.type === 'tool') {
      icon = '🔧'; typeClass = 'type-exec';
      summary = '<strong>' + escHtml(evt.toolName||'tool') + '</strong> — ' + escHtml((evt.args||'').substring(0, 100));
      body = evt.args || '';
    } else if (evt.type === 'result') {
      icon = '🔍'; typeClass = 'type-result';
      summary = '<strong>Result</strong> — ' + escHtml((evt.text||'').substring(0, 120));
      body = evt.text || '';
    } else {
      summary = '<strong>' + escHtml(evt.type) + '</strong>';
      body = JSON.stringify(evt, null, 2);
    }
    var bodyId = 'evt-body-' + idx;
    html += '<div class="evt-item ' + typeClass + '">';
    html += '<div class="evt-header" onclick="var b=document.getElementById(\'' + bodyId + '\');b.classList.toggle(\'open\');">';
    html += '<span class="evt-icon">' + icon + '</span>';
    html += '<span class="evt-summary">' + summary + '</span>';
    html += '<span class="evt-ts">' + escHtml(ts) + '</span>';
    html += '</div>';
    html += '<div class="evt-body" id="' + bodyId + '">' + escHtml(body) + '</div>';
    html += '</div>';
  });
  el.innerHTML = html || '<div style="padding:20px;color:var(--text-muted);">No events yet</div>';
}

// Initialize theme and zoom on page load
document.addEventListener('DOMContentLoaded', function() {
  initTheme();
  initZoom();
  // Overview is the default tab
  initOverviewFlow();
  initFlow();
  loadAll();
  startOverviewTasksRefresh();
});
</script>
</div> <!-- end zoom-wrapper -->

<!-- Task Detail Modal -->
<div class="modal-overlay" id="task-modal-overlay" onclick="if(event.target===this)closeTaskModal()">
  <div class="modal-card">
    <div class="modal-header">
      <div class="modal-header-left">
        <div class="modal-title" id="modal-title">Task Name</div>
        <div class="modal-session-key" id="modal-session-key">session-id</div>
      </div>
      <div class="modal-header-right">
        <label class="modal-auto-refresh"><input type="checkbox" id="modal-auto-refresh-cb" checked onchange="toggleModalAutoRefresh()"> Auto-refresh</label>
        <div class="modal-close" onclick="closeTaskModal()">&times;</div>
      </div>
    </div>
    <div class="modal-tabs">
      <div class="modal-tab active" onclick="switchModalTab('summary')">Summary</div>
      <div class="modal-tab" onclick="switchModalTab('narrative')">Narrative</div>
      <div class="modal-tab" onclick="switchModalTab('full')">Full Logs</div>
    </div>
    <div class="modal-content" id="modal-content">Loading...</div>
    <div class="modal-footer">
      <span id="modal-event-count">—</span>
      <span id="modal-msg-count">—</span>
    </div>
  </div>
</div>
</body>
</html>
"""


# ── API Routes ──────────────────────────────────────────────────────────

@app.route('/')
def index():
    resp = make_response(render_template_string(DASHBOARD_HTML))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


@app.route('/api/mc-tasks')
def api_mc_tasks():
    import requests as _req
    try:
        r = _req.get(f'{MC_URL}/api/tasks', timeout=3)
        data = r.json()
        data['available'] = True
        return jsonify(data)
    except Exception:
        return jsonify({'available': False, 'tasks': []})

@app.route('/api/overview')
def api_overview():
    sessions = _get_sessions()
    main = next((s for s in sessions if s.get('key', '').endswith(':main')), {})

    crons = _get_crons()
    enabled = len([j for j in crons if j.get('enabled')])
    disabled = len(crons) - enabled

    mem_files = _get_memory_files()
    total_size = sum(f['size'] for f in mem_files)

    # System info
    system = []
    try:
        disk = subprocess.run(['df', '-h', '/'], capture_output=True, text=True).stdout.strip().split('\n')[-1].split()
        disk_pct = int(disk[4].replace('%', '')) if len(disk) > 4 else 0
        disk_color = 'green' if disk_pct < 80 else ('yellow' if disk_pct < 90 else 'red')
        system.append(['Disk /', f'{disk[2]} / {disk[1]} ({disk[4]})', disk_color])
    except Exception:
        system.append(['Disk /', '—', ''])

    try:
        mem = subprocess.run(['free', '-h'], capture_output=True, text=True).stdout.strip().split('\n')[1].split()
        system.append(['RAM', f'{mem[2]} / {mem[1]}', ''])
    except Exception:
        system.append(['RAM', '—', ''])

    try:
        load = open('/proc/loadavg').read().split()[:3]
        system.append(['Load', ' '.join(load), ''])
    except Exception:
        system.append(['Load', '—', ''])

    try:
        uptime = subprocess.run(['uptime', '-p'], capture_output=True, text=True).stdout.strip()
        system.append(['Uptime', uptime.replace('up ', ''), ''])
    except Exception:
        system.append(['Uptime', '—', ''])

    gw = subprocess.run(['pgrep', '-f', 'moltbot'], capture_output=True, text=True)
    system.append(['Gateway', 'Running' if gw.returncode == 0 else 'Stopped',
                    'green' if gw.returncode == 0 else 'red'])

    # Infrastructure details for Flow tab
    infra = {
        'userName': USER_NAME,
        'network': get_local_ip(),
    }
    try:
        import platform
        uname = platform.uname()
        infra['machine'] = uname.node
        infra['runtime'] = f'Node.js · {uname.system} {uname.release.split("-")[0]}'
    except Exception:
        infra['machine'] = 'Host'
        infra['runtime'] = 'Runtime'

    try:
        disk_info = subprocess.run(['df', '-h', '/'], capture_output=True, text=True).stdout.strip().split('\n')[-1].split()
        infra['storage'] = f'{disk_info[1]} root'
    except Exception:
        infra['storage'] = 'Disk'

    return jsonify({
        'model': main.get('model', 'claude-opus-4-5') or 'claude-opus-4-5',
        'provider': 'anthropic',
        'sessionCount': len(sessions),
        'mainSessionUpdated': main.get('updatedAt'),
        'mainTokens': main.get('totalTokens', 0),
        'contextWindow': main.get('contextTokens', 200000),
        'cronCount': len(crons),
        'cronEnabled': enabled,
        'cronDisabled': disabled,
        'memoryCount': len(mem_files),
        'memorySize': total_size,
        'system': system,
        'infra': infra,
    })


@app.route('/api/sessions')
def api_sessions():
    return jsonify({'sessions': _get_sessions()})


@app.route('/api/crons')
def api_crons():
    return jsonify({'jobs': _get_crons()})


def _find_log_file(ds):
    """Find log file for a given date string, trying multiple prefixes and dirs."""
    dirs = [LOG_DIR, '/tmp/openclaw', '/tmp/moltbot']
    prefixes = ['openclaw-', 'moltbot-']
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for p in prefixes:
            f = os.path.join(d, f'{p}{ds}.log')
            if os.path.exists(f):
                return f
    return None

@app.route('/api/logs')
def api_logs():
    lines_count = int(request.args.get('lines', 100))
    today = datetime.now().strftime('%Y-%m-%d')
    log_file = _find_log_file(today)
    lines = []
    if log_file:
        result = subprocess.run(['tail', f'-{lines_count}', log_file], capture_output=True, text=True)
        lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
    return jsonify({'lines': lines})


@app.route('/api/logs-stream')
def api_logs_stream():
    """SSE endpoint — streams new log lines in real-time."""
    today = datetime.now().strftime('%Y-%m-%d')
    log_file = _find_log_file(today)

    def generate():
        if not log_file:
            yield 'data: {"line":"No log file found"}\n\n'
            return
        proc = subprocess.Popen(
            ['tail', '-f', '-n', '0', log_file],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            while True:
                line = proc.stdout.readline()
                if line:
                    yield f'data: {json.dumps({"line": line.rstrip()})}\n\n'
        except GeneratorExit:
            proc.kill()

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/memory-files')
def api_memory_files():
    return jsonify(_get_memory_files())


@app.route('/api/file')
def api_view_file():
    """Return the contents of a memory file."""
    path = request.args.get('path', '')
    full = os.path.normpath(os.path.join(WORKSPACE, path))
    if not full.startswith(os.path.normpath(WORKSPACE)):
        return jsonify({'error': 'Access denied'}), 403
    if not os.path.exists(full):
        return jsonify({'error': 'File not found'}), 404
    try:
        with open(full, 'r') as f:
            content = f.read(100_000)
        return jsonify({'path': path, 'content': content})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── OTLP Receiver Endpoints ─────────────────────────────────────────────

@app.route('/v1/metrics', methods=['POST'])
def otlp_metrics():
    """OTLP/HTTP receiver for metrics (protobuf)."""
    if not _HAS_OTEL_PROTO:
        return jsonify({
            'error': 'opentelemetry-proto not installed',
            'message': 'Install OTLP support: pip install openclaw-dashboard[otel]  '
                       'or: pip install opentelemetry-proto protobuf',
        }), 501

    try:
        pb_data = request.get_data()
        _process_otlp_metrics(pb_data)
        return '{}', 200, {'Content-Type': 'application/json'}
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/v1/traces', methods=['POST'])
def otlp_traces():
    """OTLP/HTTP receiver for traces (protobuf)."""
    if not _HAS_OTEL_PROTO:
        return jsonify({
            'error': 'opentelemetry-proto not installed',
            'message': 'Install OTLP support: pip install openclaw-dashboard[otel]  '
                       'or: pip install opentelemetry-proto protobuf',
        }), 501

    try:
        pb_data = request.get_data()
        _process_otlp_traces(pb_data)
        return '{}', 200, {'Content-Type': 'application/json'}
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/otel-status')
def api_otel_status():
    """Return OTLP receiver status."""
    counts = {}
    with _metrics_lock:
        for k in metrics_store:
            counts[k] = len(metrics_store[k])
    return jsonify({
        'available': _HAS_OTEL_PROTO,
        'hasData': _has_otel_data(),
        'lastReceived': _otel_last_received,
        'counts': counts,
    })


# ── Enhanced Cost Tracking Utilities ─────────────────────────────────────

def _get_model_pricing():
    """Model-specific pricing per 1M tokens (input, output)."""
    return {
        'claude-opus': (15.0, 75.0),      # Claude 3 Opus
        'claude-sonnet': (3.0, 15.0),     # Claude 3 Sonnet  
        'claude-haiku': (0.25, 1.25),     # Claude 3 Haiku
        'gpt-4': (10.0, 30.0),            # GPT-4 Turbo
        'gpt-3.5': (1.0, 2.0),            # GPT-3.5 Turbo
        'default': (15.0, 45.0),          # Conservative estimate
    }

def _calculate_enhanced_costs(daily_tokens, today_str, week_start, month_start):
    """Enhanced cost calculation with model-specific pricing."""
    pricing = _get_model_pricing()
    
    # For log parsing fallback, assume 60/40 input/output ratio
    input_ratio, output_ratio = 0.6, 0.4
    
    def calc_cost(tokens, model_key='default'):
        if tokens == 0:
            return 0.0
        in_price, out_price = pricing.get(model_key, pricing['default'])
        input_cost = (tokens * input_ratio) * (in_price / 1_000_000)
        output_cost = (tokens * output_ratio) * (out_price / 1_000_000)
        return input_cost + output_cost
    
    today_tok = daily_tokens.get(today_str, 0)
    week_tok = sum(v for k, v in daily_tokens.items() if k >= week_start)
    month_tok = sum(v for k, v in daily_tokens.items() if k >= month_start)
    
    return (
        round(calc_cost(today_tok), 4),
        round(calc_cost(week_tok), 4), 
        round(calc_cost(month_tok), 4)
    )

def _analyze_usage_trends(daily_tokens):
    """Analyze usage trends for predictions."""
    if len(daily_tokens) < 3:
        return {'prediction': None, 'trend': 'insufficient_data'}
    
    # Get last 7 days of data
    recent_days = sorted(daily_tokens.items())[-7:]
    if len(recent_days) < 3:
        return {'prediction': None, 'trend': 'insufficient_data'}
    
    tokens_series = [v for k, v in recent_days]
    
    # Simple trend analysis
    if len(tokens_series) >= 3:
        recent_avg = sum(tokens_series[-3:]) / 3
        older_avg = sum(tokens_series[:-3]) / max(1, len(tokens_series) - 3) if len(tokens_series) > 3 else recent_avg
        
        if recent_avg > older_avg * 1.2:
            trend = 'increasing'
        elif recent_avg < older_avg * 0.8:
            trend = 'decreasing'
        else:
            trend = 'stable'
        
        # Monthly prediction based on recent average
        daily_avg = sum(tokens_series[-7:]) / len(tokens_series[-7:])
        monthly_prediction = daily_avg * 30
        
        return {
            'trend': trend,
            'dailyAvg': int(daily_avg),
            'monthlyPrediction': int(monthly_prediction),
        }
    
    return {'prediction': None, 'trend': 'stable'}

def _generate_cost_warnings(today_cost, week_cost, month_cost, trend_data):
    """Generate cost warnings based on thresholds."""
    warnings = []
    
    # Daily cost warnings
    if today_cost > 10.0:
        warnings.append({
            'type': 'high_daily_cost',
            'level': 'error',
            'message': f'High daily cost: ${today_cost:.2f} (threshold: $10)',
        })
    elif today_cost > 5.0:
        warnings.append({
            'type': 'elevated_daily_cost', 
            'level': 'warning',
            'message': f'Elevated daily cost: ${today_cost:.2f}',
        })
    
    # Weekly cost warnings  
    if week_cost > 50.0:
        warnings.append({
            'type': 'high_weekly_cost',
            'level': 'error', 
            'message': f'High weekly cost: ${week_cost:.2f} (threshold: $50)',
        })
    elif week_cost > 25.0:
        warnings.append({
            'type': 'elevated_weekly_cost',
            'level': 'warning',
            'message': f'Elevated weekly cost: ${week_cost:.2f}',
        })
    
    # Monthly cost warnings
    if month_cost > 200.0:
        warnings.append({
            'type': 'high_monthly_cost',
            'level': 'error',
            'message': f'High monthly cost: ${month_cost:.2f} (threshold: $200)', 
        })
    elif month_cost > 100.0:
        warnings.append({
            'type': 'elevated_monthly_cost',
            'level': 'warning', 
            'message': f'Elevated monthly cost: ${month_cost:.2f}',
        })
    
    # Trend-based warnings
    if trend_data.get('trend') == 'increasing' and trend_data.get('monthlyPrediction', 0) > 300:
        warnings.append({
            'type': 'trend_warning',
            'level': 'warning',
            'message': f'Usage trending up - projected monthly cost: ${(trend_data["monthlyPrediction"] * 0.00003):.2f}',
        })
    
    return warnings

# ── Usage cache ─────────────────────────────────────────────────────────
_usage_cache = {'data': None, 'ts': 0}
_USAGE_CACHE_TTL = 60  # seconds

# ── New Feature APIs ────────────────────────────────────────────────────

@app.route('/api/usage')
def api_usage():
    """Token/cost tracking from transcript files — Enhanced OTLP workaround."""
    import time as _time
    now = _time.time()
    if _usage_cache['data'] is not None and (now - _usage_cache['ts']) < _USAGE_CACHE_TTL:
        return jsonify(_usage_cache['data'])

    # Prefer OTLP data when available
    if _has_otel_data():
        result = _get_otel_usage_data()
        _usage_cache['data'] = result
        _usage_cache['ts'] = now
        return jsonify(result)

    # NEW: Parse transcript JSONL files for real usage data
    sessions_dir = SESSIONS_DIR or os.path.expanduser('~/.moltbot/agents/main/sessions')
    daily_tokens = {}
    daily_cost = {}
    model_usage = {}
    session_costs = {}
    
    if os.path.isdir(sessions_dir):
        for fname in os.listdir(sessions_dir):
            if not fname.endswith('.jsonl'):
                continue
            fpath = os.path.join(sessions_dir, fname)
            session_cost = 0
            
            try:
                with open(fpath, 'r') as f:
                    for line in f:
                        try:
                            obj = json.loads(line.strip())
                            
                            # Only process message entries with usage data
                            if obj.get('type') != 'message':
                                continue
                                
                            message = obj.get('message', {})
                            usage = message.get('usage')
                            if not usage or not isinstance(usage, dict):
                                continue
                                
                            # Extract the exact usage format from the brief
                            tokens_data = {
                                'input': usage.get('input', 0),
                                'output': usage.get('output', 0),
                                'cacheRead': usage.get('cacheRead', 0),
                                'cacheWrite': usage.get('cacheWrite', 0),
                                'totalTokens': usage.get('totalTokens', 0),
                                'cost': usage.get('cost', {})
                            }
                            
                            cost_data = tokens_data['cost']
                            if isinstance(cost_data, dict) and 'total' in cost_data:
                                total_cost = float(cost_data['total'])
                            else:
                                total_cost = 0.0
                            
                            # Extract model name
                            model = message.get('model', 'unknown') or 'unknown'
                            
                            # Get timestamp and convert to date
                            ts = obj.get('timestamp')
                            if ts:
                                # Handle ISO timestamp strings
                                if isinstance(ts, str):
                                    try:
                                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                                    except:
                                        continue
                                else:
                                    # Handle numeric timestamps
                                    dt = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts)
                                
                                day = dt.strftime('%Y-%m-%d')
                                
                                # Aggregate daily tokens and costs
                                daily_tokens[day] = daily_tokens.get(day, 0) + tokens_data['totalTokens']
                                daily_cost[day] = daily_cost.get(day, 0) + total_cost
                                
                                # Track model usage
                                model_usage[model] = model_usage.get(model, 0) + tokens_data['totalTokens']
                                
                                # Track session costs
                                session_cost += total_cost
                                
                        except (json.JSONDecodeError, ValueError, KeyError):
                            continue
                            
                # Store session cost
                session_costs[fname.replace('.jsonl', '')] = session_cost
                        
            except Exception:
                continue

    # Build response data
    today = datetime.now()
    days = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        ds = d.strftime('%Y-%m-%d')
        days.append({
            'date': ds, 
            'tokens': daily_tokens.get(ds, 0),
            'cost': daily_cost.get(ds, 0)
        })

    # Calculate aggregations
    today_str = today.strftime('%Y-%m-%d')
    week_start = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
    month_start = today.strftime('%Y-%m-01')
    
    today_tok = daily_tokens.get(today_str, 0)
    week_tok = sum(v for k, v in daily_tokens.items() if k >= week_start)
    month_tok = sum(v for k, v in daily_tokens.items() if k >= month_start)
    
    today_cost = daily_cost.get(today_str, 0)
    week_cost = sum(v for k, v in daily_cost.items() if k >= week_start)
    month_cost = sum(v for k, v in daily_cost.items() if k >= month_start)
    
    # Trend analysis & predictions
    trend_data = _analyze_usage_trends(daily_tokens)
    
    # Cost warnings
    warnings = _generate_cost_warnings(today_cost, week_cost, month_cost, trend_data)
    
    # Model breakdown for display
    model_breakdown = [
        {'model': k, 'tokens': v}
        for k, v in sorted(model_usage.items(), key=lambda x: -x[1])
    ]
    
    result = {
        'source': 'transcripts',
        'days': days,
        'today': today_tok,
        'week': week_tok, 
        'month': month_tok,
        'todayCost': round(today_cost, 4),
        'weekCost': round(week_cost, 4),
        'monthCost': round(month_cost, 4),
        'modelBreakdown': model_breakdown,
        'sessionCosts': session_costs,
        'trend': trend_data,
        'warnings': warnings,
    }
    import time as _time
    _usage_cache['data'] = result
    _usage_cache['ts'] = _time.time()
    return jsonify(result)


@app.route('/api/usage/export')
def api_usage_export():
    """Export usage data as CSV."""
    try:
        # Get usage data
        if _has_otel_data():
            data = _get_otel_usage_data()
        else:
            # Call the same logic as /api/usage but get full data
            sessions_dir = SESSIONS_DIR or os.path.expanduser('~/.clawdbot/agents/main/sessions')
            daily_tokens = {}
            
            if os.path.isdir(sessions_dir):
                for fname in os.listdir(sessions_dir):
                    if not fname.endswith('.jsonl'):
                        continue
                    fpath = os.path.join(sessions_dir, fname)
                    try:
                        fmtime = datetime.fromtimestamp(os.path.getmtime(fpath))
                        with open(fpath, 'r') as f:
                            for line in f:
                                try:
                                    obj = json.loads(line.strip())
                                    tokens = 0
                                    usage = obj.get('usage') or obj.get('tokens_used') or {}
                                    if isinstance(usage, dict):
                                        tokens = (usage.get('total_tokens') or usage.get('totalTokens')
                                                  or (usage.get('input_tokens', 0) + usage.get('output_tokens', 0))
                                                  or 0)
                                    elif isinstance(usage, (int, float)):
                                        tokens = int(usage)
                                    if not tokens:
                                        content = obj.get('content', '')
                                        if isinstance(content, str) and len(content) > 0:
                                            tokens = max(1, len(content) // 4)
                                        elif isinstance(content, list):
                                            total_len = sum(len(str(c.get('text', ''))) for c in content if isinstance(c, dict))
                                            tokens = max(1, total_len // 4) if total_len else 0
                                    ts = obj.get('timestamp') or obj.get('time') or obj.get('created_at')
                                    if ts:
                                        if isinstance(ts, (int, float)):
                                            dt = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts)
                                        else:
                                            try:
                                                dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
                                            except Exception:
                                                dt = fmtime
                                    else:
                                        dt = fmtime
                                    day = dt.strftime('%Y-%m-%d')
                                    if tokens > 0:
                                        daily_tokens[day] = daily_tokens.get(day, 0) + tokens
                                except (json.JSONDecodeError, ValueError):
                                    pass
                    except Exception:
                        pass
            
            today = datetime.now()
            today_str = today.strftime('%Y-%m-%d')
            week_start = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
            month_start = today.strftime('%Y-%m-01')
            
            # Build data structure similar to OTLP
            days = []
            for i in range(30, -1, -1):  # Last 30 days for export
                d = today - timedelta(days=i)
                ds = d.strftime('%Y-%m-%d')
                tokens = daily_tokens.get(ds, 0)
                cost = round(tokens * (30.0 / 1_000_000), 4)  # Default pricing
                days.append({'date': ds, 'tokens': tokens, 'cost': cost})
                
            data = {'days': days}
        
        # Generate CSV content
        csv_lines = ['Date,Tokens,Cost']
        for day in data['days']:
            csv_lines.append(f"{day['date']},{day['tokens']},{day.get('cost', 0):.4f}")
        
        csv_content = '\n'.join(csv_lines)
        
        response = make_response(csv_content)
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = f'attachment; filename=openclaw-usage-{datetime.now().strftime("%Y%m%d")}.csv'
        return response
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/transcripts')
def api_transcripts():
    """List available session transcript .jsonl files."""
    sessions_dir = SESSIONS_DIR or os.path.expanduser('~/.clawdbot/agents/main/sessions')
    transcripts = []
    if os.path.isdir(sessions_dir):
        for fname in sorted(os.listdir(sessions_dir), key=lambda f: os.path.getmtime(os.path.join(sessions_dir, f)), reverse=True):
            if not fname.endswith('.jsonl') or 'deleted' in fname:
                continue
            fpath = os.path.join(sessions_dir, fname)
            try:
                msg_count = 0
                with open(fpath) as f:
                    for _ in f:
                        msg_count += 1
                transcripts.append({
                    'id': fname.replace('.jsonl', ''),
                    'name': fname.replace('.jsonl', '')[:40],
                    'messages': msg_count,
                    'size': os.path.getsize(fpath),
                    'modified': int(os.path.getmtime(fpath) * 1000),
                })
            except Exception:
                pass
    return jsonify({'transcripts': transcripts[:50]})


@app.route('/api/transcript/<session_id>')
def api_transcript(session_id):
    """Parse and return a session transcript for the chat viewer."""
    sessions_dir = SESSIONS_DIR or os.path.expanduser('~/.clawdbot/agents/main/sessions')
    fpath = os.path.join(sessions_dir, session_id + '.jsonl')
    # Sanitize path
    fpath = os.path.normpath(fpath)
    if not fpath.startswith(os.path.normpath(sessions_dir)):
        return jsonify({'error': 'Access denied'}), 403
    if not os.path.exists(fpath):
        return jsonify({'error': 'Transcript not found'}), 404

    messages = []
    model = None
    total_tokens = 0
    first_ts = None
    last_ts = None
    try:
        with open(fpath) as f:
            for line in f:
                try:
                    obj = json.loads(line.strip())
                    role = obj.get('role', obj.get('type', 'unknown'))
                    content = obj.get('content', '')
                    if isinstance(content, list):
                        parts = []
                        for part in content:
                            if isinstance(part, dict):
                                parts.append(part.get('text', str(part)))
                            else:
                                parts.append(str(part))
                        content = '\n'.join(parts)
                    elif not isinstance(content, str):
                        content = str(content) if content else ''
                    # Tool use handling
                    if obj.get('tool_calls') or obj.get('tool_use'):
                        tools = obj.get('tool_calls') or obj.get('tool_use') or []
                        if isinstance(tools, list):
                            for tc in tools:
                                tname = tc.get('name', tc.get('function', {}).get('name', 'tool'))
                                messages.append({
                                    'role': 'tool',
                                    'content': f"[Tool Call: {tname}]\n{json.dumps(tc.get('input', tc.get('arguments', {})), indent=2)[:500]}",
                                    'timestamp': obj.get('timestamp') or obj.get('time'),
                                })
                    if role == 'tool_result':
                        role = 'tool'
                    ts = obj.get('timestamp') or obj.get('time') or obj.get('created_at')
                    if ts:
                        if isinstance(ts, (int, float)):
                            ts_ms = int(ts * 1000) if ts < 1e12 else int(ts)
                        else:
                            try:
                                ts_ms = int(datetime.fromisoformat(str(ts).replace('Z', '+00:00')).timestamp() * 1000)
                            except Exception:
                                ts_ms = None
                        if ts_ms:
                            if not first_ts or ts_ms < first_ts:
                                first_ts = ts_ms
                            if not last_ts or ts_ms > last_ts:
                                last_ts = ts_ms
                    else:
                        ts_ms = None
                    if not model:
                        model = obj.get('model')
                    usage = obj.get('usage', {})
                    if isinstance(usage, dict):
                        total_tokens += usage.get('total_tokens', 0) or (
                            usage.get('input_tokens', 0) + usage.get('output_tokens', 0))
                    if content or role in ('user', 'assistant', 'system'):
                        messages.append({
                            'role': role, 'content': content, 'timestamp': ts_ms,
                        })
                except (json.JSONDecodeError, ValueError):
                    pass
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    duration = None
    if first_ts and last_ts and last_ts > first_ts:
        dur_sec = (last_ts - first_ts) / 1000
        if dur_sec < 60:
            duration = f'{dur_sec:.0f}s'
        elif dur_sec < 3600:
            duration = f'{dur_sec / 60:.0f}m'
        else:
            duration = f'{dur_sec / 3600:.1f}h'

    return jsonify({
        'name': session_id[:40],
        'messageCount': len(messages),
        'model': model,
        'totalTokens': total_tokens,
        'duration': duration,
        'messages': messages[:500],  # Cap at 500 messages
    })


@app.route('/api/transcript-events/<session_id>')
def api_transcript_events(session_id):
    """Parse a session transcript JSONL into structured events for the detail modal."""
    sessions_dir = SESSIONS_DIR or os.path.expanduser('~/.clawdbot/agents/main/sessions')
    fpath = os.path.join(sessions_dir, session_id + '.jsonl')
    fpath = os.path.normpath(fpath)
    if not fpath.startswith(os.path.normpath(sessions_dir)):
        return jsonify({'error': 'Access denied'}), 403
    if not os.path.exists(fpath):
        return jsonify({'error': 'Transcript not found'}), 404

    events = []
    msg_count = 0
    try:
        with open(fpath) as f:
            for line in f:
                try:
                    obj = json.loads(line.strip())
                except (json.JSONDecodeError, ValueError):
                    continue

                ts = obj.get('timestamp') or obj.get('time') or obj.get('created_at')
                ts_val = None
                if ts:
                    if isinstance(ts, (int, float)):
                        ts_val = int(ts * 1000) if ts < 1e12 else int(ts)
                    else:
                        try:
                            ts_val = int(datetime.fromisoformat(str(ts).replace('Z', '+00:00')).timestamp() * 1000)
                        except Exception:
                            pass

                obj_type = obj.get('type', '')
                if obj_type == 'message':
                    msg = obj.get('message', {})
                    role = msg.get('role', '')
                    content = msg.get('content', '')
                    msg_count += 1

                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get('type', '')
                            if btype == 'thinking':
                                events.append({'type': 'thinking', 'text': block.get('thinking', '')[:2000], 'timestamp': ts_val})
                            elif btype == 'text':
                                text = block.get('text', '')
                                if role == 'user':
                                    events.append({'type': 'user', 'text': text[:3000], 'timestamp': ts_val})
                                elif role == 'assistant':
                                    events.append({'type': 'agent', 'text': text[:3000], 'timestamp': ts_val})
                            elif btype in ('toolCall', 'tool_use'):
                                name = block.get('name', '?')
                                args = block.get('arguments') or block.get('input') or {}
                                args_str = json.dumps(args, indent=2)[:1000] if isinstance(args, dict) else str(args)[:1000]
                                if name == 'exec':
                                    cmd = args.get('command', '') if isinstance(args, dict) else ''
                                    events.append({'type': 'exec', 'command': cmd, 'toolName': name, 'args': args_str, 'timestamp': ts_val})
                                elif name in ('Read', 'read'):
                                    fp = (args.get('file_path') or args.get('path') or '') if isinstance(args, dict) else ''
                                    events.append({'type': 'read', 'file': fp, 'toolName': name, 'args': args_str, 'timestamp': ts_val})
                                else:
                                    events.append({'type': 'tool', 'toolName': name, 'args': args_str, 'timestamp': ts_val})
                    elif isinstance(content, str) and content:
                        if role == 'user':
                            events.append({'type': 'user', 'text': content[:3000], 'timestamp': ts_val})
                        elif role == 'assistant':
                            events.append({'type': 'agent', 'text': content[:3000], 'timestamp': ts_val})
                        elif role == 'toolResult':
                            events.append({'type': 'result', 'text': content[:2000], 'timestamp': ts_val})

                    if role == 'toolResult' and isinstance(content, list):
                        text_parts = []
                        for block in content:
                            if isinstance(block, dict) and block.get('type') == 'text':
                                text_parts.append(block.get('text', ''))
                        if text_parts:
                            events.append({'type': 'result', 'text': '\n'.join(text_parts)[:2000], 'timestamp': ts_val})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'events': events[-500:], 'messageCount': msg_count, 'totalEvents': len(events)})


@app.route('/api/subagents')
def api_subagents():
    """Get sub-agent sessions from sessions.json index (batch read, no N+1)."""
    sessions_dir = SESSIONS_DIR or os.path.expanduser('~/.openclaw/agents/main/sessions')
    index_path = os.path.join(sessions_dir, 'sessions.json')
    subagents = []
    now = time.time() * 1000

    # Read the authoritative session index
    try:
        with open(index_path, 'r') as f:
            index = json.load(f)
    except Exception:
        return jsonify({'subagents': [], 'counts': {'active': 0, 'idle': 0, 'stale': 0, 'total': 0}, 'totalActive': 0})

    # Collect sub-agent entries and their session IDs for batch transcript reading
    sa_entries = []
    for key, meta in index.items():
        if ':subagent:' not in key:
            continue
        sa_entries.append((key, meta))

    # Batch: read first+last lines of each transcript for task detection
    tasks = {}
    labels = {}
    for key, meta in sa_entries:
        sid = meta.get('sessionId', '')
        if not sid:
            continue
        fpath = os.path.join(sessions_dir, f"{sid}.jsonl")
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, 'rb') as f:
                # Read first few lines for session label
                first_lines = []
                for _ in range(10):
                    line = f.readline()
                    if not line:
                        break
                    first_lines.append(line)

                # Read last ~8KB for recent activity
                try:
                    f.seek(0, 2)
                    fsize = f.tell()
                    tail_start = max(0, fsize - 8192)
                    f.seek(tail_start)
                    tail_data = f.read().decode('utf-8', errors='replace')
                    tail_lines = tail_data.strip().split('\n')
                    if tail_start > 0:
                        tail_lines = tail_lines[1:]  # drop partial first line
                except Exception:
                    tail_lines = []

                # Extract label from session spawn message (first user message)
                label = None
                for raw in first_lines:
                    try:
                        obj = json.loads(raw)
                        if obj.get('type') == 'message' and obj.get('message', {}).get('role') == 'user':
                            content = obj['message'].get('content', [])
                            if isinstance(content, list):
                                for block in content:
                                    if block.get('type') == 'text':
                                        text = block.get('text', '')
                                        # Extract the label from subagent context
                                        if 'Label:' in text:
                                            label = text.split('Label:')[1].split('\n')[0].strip()
                                        elif len(text) > 20:
                                            # Use first line as label
                                            label = text.split('\n')[0][:100]
                                        break
                            break
                    except Exception:
                        continue
                if label:
                    labels[key] = label

                # Extract recent tool calls and activity from tail
                recent_tools = []
                last_text = None
                for raw in reversed(tail_lines[-20:]):
                    try:
                        obj = json.loads(raw)
                        if obj.get('type') != 'message':
                            continue
                        msg = obj.get('message', {})
                        content = msg.get('content', [])
                        if not isinstance(content, list):
                            continue
                        for block in content:
                            btype = block.get('type', '')
                            if btype in ('tool_use', 'toolCall') and len(recent_tools) < 5:
                                tool_name = block.get('name', '?')
                                tool_input = block.get('input') or block.get('arguments') or {}
                                summary = _summarize_tool_input(tool_name, tool_input)
                                recent_tools.append({'name': tool_name, 'summary': summary[:120], 'ts': obj.get('timestamp', '')})
                            elif btype == 'text' and msg.get('role') == 'assistant' and not last_text:
                                t = block.get('text', '').strip()
                                if t and len(t) > 5:
                                    last_text = t[:200]
                    except Exception:
                        continue
                tasks[key] = {
                    'recentTools': list(reversed(recent_tools)),
                    'lastText': last_text,
                }
        except Exception:
            continue

    # Build response
    for key, meta in sa_entries:
        uuid = key.split(':')[-1]
        updated_at = meta.get('updatedAt', 0)
        sid = meta.get('sessionId', '')

        # Status based on recency
        age_ms = now - updated_at if updated_at else float('inf')
        if age_ms < 5 * 60 * 1000:
            status = 'active'
        elif age_ms < 30 * 60 * 1000:
            status = 'idle'
        else:
            status = 'stale'

        # Runtime (from first seen to last update)
        runtime_ms = age_ms if age_ms != float('inf') else 0
        if runtime_ms < 60000:
            runtime = f"{int(runtime_ms / 1000)}s ago"
        elif runtime_ms < 3600000:
            runtime = f"{int(runtime_ms / 60000)}m ago"
        elif runtime_ms < 86400000:
            runtime = f"{int(runtime_ms / 3600000)}h ago"
        else:
            runtime = f"{int(runtime_ms / 86400000)}d ago"

        task_info = tasks.get(key, {})
        # Prefer label from sessions.json metadata, fallback to transcript extraction
        label = meta.get('label') or labels.get(key, f'Worker {uuid[:8]}')

        subagents.append({
            'key': key,
            'uuid': uuid,
            'sessionId': sid,
            'displayName': label,
            'status': status,
            'runtime': runtime,
            'runtimeMs': runtime_ms,
            'updatedAt': updated_at,
            'recentTools': task_info.get('recentTools', []),
            'lastText': task_info.get('lastText', ''),
            'model': meta.get('model', 'unknown'),
            'channel': meta.get('channel', meta.get('lastChannel', 'agent')),
            'spawnedBy': meta.get('spawnedBy', ''),
            'abortedLastRun': meta.get('abortedLastRun', False),
            'totalTokens': meta.get('totalTokens', 0),
            'outputTokens': meta.get('outputTokens', 0),
        })

    subagents.sort(key=lambda x: x['updatedAt'] or 0, reverse=True)

    counts = {
        'active': sum(1 for s in subagents if s['status'] == 'active'),
        'idle': sum(1 for s in subagents if s['status'] == 'idle'),
        'stale': sum(1 for s in subagents if s['status'] == 'stale'),
        'total': len(subagents),
    }

    return jsonify({'subagents': subagents, 'counts': counts, 'totalActive': counts['active']})


@app.route('/api/subagent/<session_id>/activity')
def api_subagent_activity(session_id):
    """Stream recent activity from a sub-agent's transcript. Progressive: reads tail only."""
    sessions_dir = SESSIONS_DIR or os.path.expanduser('~/.openclaw/agents/main/sessions')
    fpath = os.path.join(sessions_dir, f"{session_id}.jsonl")
    if not os.path.exists(fpath):
        return jsonify({'error': 'not found', 'events': []}), 404

    # Read last ~16KB for activity timeline
    tail_size = int(request.args.get('tail', 16384))
    events = []
    try:
        with open(fpath, 'rb') as f:
            f.seek(0, 2)
            fsize = f.tell()
            start = max(0, fsize - tail_size)
            f.seek(start)
            data = f.read().decode('utf-8', errors='replace')
            lines = data.strip().split('\n')
            if start > 0:
                lines = lines[1:]

            for raw in lines:
                try:
                    obj = json.loads(raw)
                    etype = obj.get('type', '')
                    ts = obj.get('timestamp', '')

                    if etype == 'message':
                        msg = obj.get('message', {})
                        role = msg.get('role', '')
                        content = msg.get('content', [])
                        if not isinstance(content, list):
                            continue
                        for block in content:
                            btype = block.get('type', '')
                            if btype in ('tool_use', 'toolCall'):
                                inp = block.get('input') or block.get('arguments') or {}
                                events.append({
                                    'type': 'tool_call',
                                    'ts': ts,
                                    'tool': block.get('name', '?'),
                                    'input': _summarize_tool_input(block.get('name', ''), inp),
                                })
                            elif btype in ('tool_result', 'toolResult'):
                                result_text = ''
                                sub = block.get('content', '')
                                if isinstance(sub, list):
                                    for sb in sub[:1]:
                                        result_text = sb.get('text', '')[:300]
                                elif isinstance(sub, str):
                                    result_text = sub[:300]
                                events.append({
                                    'type': 'tool_result',
                                    'ts': ts,
                                    'preview': result_text,
                                    'isError': block.get('is_error', False),
                                })
                            elif btype == 'text' and role == 'assistant':
                                text = block.get('text', '').strip()
                                if text:
                                    events.append({
                                        'type': 'thinking',
                                        'ts': ts,
                                        'text': text[:500],
                                    })
                            elif btype == 'thinking':
                                text = block.get('thinking', '').strip()
                                if text:
                                    events.append({
                                        'type': 'internal_thought',
                                        'ts': ts,
                                        'text': text[:300],
                                    })
                    elif etype == 'model_change':
                        events.append({
                            'type': 'model_change',
                            'ts': ts,
                            'model': obj.get('modelId', '?'),
                        })
                except Exception:
                    continue
    except Exception as e:
        return jsonify({'error': str(e), 'events': []}), 500

    return jsonify({'events': events, 'fileSize': fsize if 'fsize' in dir() else 0})


def _summarize_tool_input(name, inp):
    """Create a human-readable one-line summary of a tool call."""
    if name == 'exec':
        return (inp.get('command') or str(inp))[:150]
    elif name in ('Read', 'read'):
        return f"📖 {inp.get('file_path') or inp.get('path') or '?'}"
    elif name in ('Write', 'write'):
        return f"✏️ {inp.get('file_path') or inp.get('path') or '?'}"
    elif name in ('Edit', 'edit'):
        return f"🔧 {inp.get('file_path') or inp.get('path') or '?'}"
    elif name == 'web_search':
        return f"🔍 {inp.get('query', '?')}"
    elif name == 'web_fetch':
        return f"🌐 {inp.get('url', '?')[:80]}"
    elif name == 'browser':
        return f"🖥️ {inp.get('action', '?')}"
    elif name == 'message':
        return f"💬 {inp.get('action', '?')} → {inp.get('message', '')[:60]}"
    elif name == 'tts':
        return f"🔊 {inp.get('text', '')[:60]}"
    else:
        return str(inp)[:120]


@app.route('/api/heatmap')
def api_heatmap():
    """Activity heatmap — events per hour for the last 7 days."""
    now = datetime.now()
    # Initialize 7 days × 24 hours grid
    grid = {}
    day_labels = []
    for i in range(6, -1, -1):
        d = now - timedelta(days=i)
        ds = d.strftime('%Y-%m-%d')
        grid[ds] = [0] * 24
        day_labels.append({'date': ds, 'label': d.strftime('%a %d')})

    # Parse log files for the last 7 days
    for i in range(7):
        d = now - timedelta(days=i)
        ds = d.strftime('%Y-%m-%d')
        log_file = _find_log_file(ds)
        if not log_file:
            continue
        try:
            with open(log_file) as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                        ts = obj.get('time') or (obj.get('_meta', {}).get('date') if isinstance(obj.get('_meta'), dict) else None)
                        if ts:
                            if isinstance(ts, (int, float)):
                                dt = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts)
                            else:
                                dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00').replace('+00:00', ''))
                            hour = dt.hour
                            day_key = dt.strftime('%Y-%m-%d')
                            if day_key in grid:
                                grid[day_key][hour] += 1
                    except Exception:
                        # Count non-JSON lines too
                        if ds in grid:
                            grid[ds][12] += 1  # default to noon
        except Exception:
            pass

    max_val = max(max(hours) for hours in grid.values()) if grid else 0
    days = []
    for dl in day_labels:
        days.append({'label': dl['label'], 'hours': grid.get(dl['date'], [0] * 24)})

    return jsonify({'days': days, 'max': max_val})


@app.route('/api/health')
def api_health():
    """System health checks."""
    checks = []
    # 1. Gateway — check if port 18789 is responding
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        result = s.connect_ex(('127.0.0.1', 18789))
        s.close()
        if result == 0:
            checks.append({'id': 'gateway', 'status': 'healthy', 'color': 'green', 'detail': 'Port 18789 responding'})
        else:
            # Fallback: check process
            gw = subprocess.run(['pgrep', '-f', 'moltbot'], capture_output=True, text=True)
            if gw.returncode == 0:
                checks.append({'id': 'gateway', 'status': 'warning', 'color': 'yellow', 'detail': 'Process running, port not responding'})
            else:
                checks.append({'id': 'gateway', 'status': 'critical', 'color': 'red', 'detail': 'Not running'})
    except Exception:
        checks.append({'id': 'gateway', 'status': 'critical', 'color': 'red', 'detail': 'Check failed'})

    # 2. Disk space — warn if < 5GB free
    try:
        st = os.statvfs('/')
        free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
        total_gb = (st.f_blocks * st.f_frsize) / (1024 ** 3)
        pct_used = ((total_gb - free_gb) / total_gb) * 100
        if free_gb < 2:
            checks.append({'id': 'disk', 'status': 'critical', 'color': 'red', 'detail': f'{free_gb:.1f} GB free ({pct_used:.0f}% used)'})
        elif free_gb < 5:
            checks.append({'id': 'disk', 'status': 'warning', 'color': 'yellow', 'detail': f'{free_gb:.1f} GB free ({pct_used:.0f}% used)'})
        else:
            checks.append({'id': 'disk', 'status': 'healthy', 'color': 'green', 'detail': f'{free_gb:.1f} GB free ({pct_used:.0f}% used)'})
    except Exception:
        checks.append({'id': 'disk', 'status': 'warning', 'color': 'yellow', 'detail': 'Check failed'})

    # 3. Memory usage (RSS of this process + overall)
    try:
        import resource
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KB -> MB on Linux
        mem = subprocess.run(['free', '-m'], capture_output=True, text=True)
        mem_parts = mem.stdout.strip().split('\n')[1].split()
        used_mb = int(mem_parts[2])
        total_mb = int(mem_parts[1])
        pct = (used_mb / total_mb) * 100
        if pct > 90:
            checks.append({'id': 'memory', 'status': 'critical', 'color': 'red', 'detail': f'{used_mb}MB / {total_mb}MB ({pct:.0f}%)'})
        elif pct > 75:
            checks.append({'id': 'memory', 'status': 'warning', 'color': 'yellow', 'detail': f'{used_mb}MB / {total_mb}MB ({pct:.0f}%)'})
        else:
            checks.append({'id': 'memory', 'status': 'healthy', 'color': 'green', 'detail': f'{used_mb}MB / {total_mb}MB ({pct:.0f}%)'})
    except Exception:
        checks.append({'id': 'memory', 'status': 'warning', 'color': 'yellow', 'detail': 'Check failed'})

    # 4. Uptime
    try:
        uptime = subprocess.run(['uptime', '-p'], capture_output=True, text=True).stdout.strip().replace('up ', '')
        checks.append({'id': 'uptime', 'status': 'healthy', 'color': 'green', 'detail': uptime})
    except Exception:
        checks.append({'id': 'uptime', 'status': 'warning', 'color': 'yellow', 'detail': 'Unknown'})

    # 5. OTLP Metrics
    if _has_otel_data():
        ago = time.time() - _otel_last_received
        if ago < 300:  # <5min
            total = sum(len(metrics_store[k]) for k in metrics_store)
            checks.append({'id': 'otel', 'status': 'healthy', 'color': 'green',
                           'detail': f'Connected — {total} data points, last {int(ago)}s ago'})
        elif ago < 3600:
            checks.append({'id': 'otel', 'status': 'warning', 'color': 'yellow',
                           'detail': f'Stale — last data {int(ago/60)}m ago'})
        else:
            checks.append({'id': 'otel', 'status': 'warning', 'color': 'yellow',
                           'detail': f'Stale — last data {int(ago/3600)}h ago'})
    elif _HAS_OTEL_PROTO:
        checks.append({'id': 'otel', 'status': 'warning', 'color': 'yellow',
                       'detail': 'OTLP ready — no data received yet'})
    else:
        checks.append({'id': 'otel', 'status': 'warning', 'color': 'yellow',
                       'detail': 'Not installed — pip install openclaw-dashboard[otel]'})

    return jsonify({'checks': checks})


@app.route('/api/health-stream')
def api_health_stream():
    """SSE endpoint — auto-refresh health checks every 30 seconds."""
    def generate():
        while True:
            try:
                with app.test_request_context():
                    resp = api_health()
                    data = resp.get_json()
                    yield f'data: {json.dumps(data)}\n\n'
            except Exception:
                yield f'data: {json.dumps({"checks": []})}\n\n'
            time.sleep(30)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── Data Helpers ────────────────────────────────────────────────────────

def _get_sessions():
    """Read active sessions from the session directory."""
    sessions = []
    try:
        base = SESSIONS_DIR or os.path.expanduser('~/.clawdbot/agents/main/sessions')
        if not os.path.isdir(base):
            return sessions
        idx_files = sorted(
            [f for f in os.listdir(base) if f.endswith('.jsonl') and 'deleted' not in f],
            key=lambda f: os.path.getmtime(os.path.join(base, f)),
            reverse=True
        )
        for fname in idx_files[:30]:
            fpath = os.path.join(base, fname)
            try:
                mtime = os.path.getmtime(fpath)
                size = os.path.getsize(fpath)
                with open(fpath) as f:
                    first = json.loads(f.readline())
                sid = fname.replace('.jsonl', '')
                sessions.append({
                    'sessionId': sid,
                    'key': sid[:12] + '...',
                    'displayName': sid[:20],
                    'updatedAt': int(mtime * 1000),
                    'model': 'claude-opus-4-5',
                    'channel': 'unknown',
                    'totalTokens': size,
                    'contextTokens': 200000,
                })
            except Exception:
                pass
    except Exception:
        pass
    return sessions


def _get_crons():
    """Read crons from moltbot state."""
    try:
        crons_file = os.path.expanduser('~/.clawdbot/cron/jobs.json')
        if os.path.exists(crons_file):
            with open(crons_file) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get('jobs', list(data.values()))
    except Exception:
        pass
    return []


def _get_memory_files():
    """List workspace memory files."""
    result = []
    for name in ['MEMORY.md', 'SOUL.md', 'IDENTITY.md', 'USER.md', 'AGENTS.md', 'TOOLS.md', 'HEARTBEAT.md']:
        path = os.path.join(WORKSPACE, name)
        if os.path.exists(path):
            result.append({'path': name, 'size': os.path.getsize(path)})
    if os.path.isdir(MEMORY_DIR):
        pattern = os.path.join(MEMORY_DIR, '*.md')
        for f in sorted(glob.glob(pattern), reverse=True):
            name = 'memory/' + os.path.basename(f)
            result.append({'path': name, 'size': os.path.getsize(f)})
    return result


# ── CLI Entry Point ─────────────────────────────────────────────────────

BANNER = r"""
   ___                    ____ _
  / _ \ _ __   ___ _ __  / ___| | __ ___      __
 | | | | '_ \ / _ \ '_ \| |   | |/ _` \ \ /\ / /
 | |_| | |_) |  __/ | | | |___| | (_| |\ V  V /
  \___/| .__/ \___|_| |_|\____|_|\__,_| \_/\_/
       |_|          Dashboard v{version}

  🦞  See your agent think

  Tabs: Overview · 📊 Usage · Sessions · Crons · Logs
        Memory · 📜 Transcripts · Flow
  New:  📡 OTLP receiver · Real-time metrics · Model breakdown
"""


def main():
    parser = argparse.ArgumentParser(
        description="OpenClaw Dashboard — Real-time observability for your AI agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Environment variables:\n"
               "  OPENCLAW_HOME         Agent workspace directory\n"
               "  OPENCLAW_LOG_DIR      Log directory (default: auto-detected)\n"
               "  OPENCLAW_METRICS_FILE Path to metrics persistence JSON file\n"
               "  OPENCLAW_USER         Your name in the Flow visualization\n"
    )
    parser.add_argument('--port', '-p', type=int, default=8900, help='Port (default: 8900)')
    parser.add_argument('--host', '-H', type=str, default='0.0.0.0', help='Host (default: 0.0.0.0)')
    parser.add_argument('--workspace', '-w', type=str, help='Agent workspace directory')
    parser.add_argument('--log-dir', '-l', type=str, help='Log directory')
    parser.add_argument('--sessions-dir', '-s', type=str, help='Sessions directory (transcript .jsonl files)')
    parser.add_argument('--metrics-file', '-m', type=str, help='Path to metrics persistence JSON file')
    parser.add_argument('--name', '-n', type=str, help='Your name (shown in Flow tab)')
    parser.add_argument('--version', '-v', action='version', version=f'openclaw-dashboard {__version__}')

    args = parser.parse_args()
    detect_config(args)

    # Metrics file config
    global METRICS_FILE
    if args.metrics_file:
        METRICS_FILE = os.path.expanduser(args.metrics_file)
    elif os.environ.get('OPENCLAW_METRICS_FILE'):
        METRICS_FILE = os.path.expanduser(os.environ['OPENCLAW_METRICS_FILE'])

    # Load persisted metrics and start flush thread
    _load_metrics_from_disk()
    _start_metrics_flush_thread()

    # Print banner
    print(BANNER.format(version=__version__))
    print(f"  Workspace:  {WORKSPACE}")
    print(f"  Sessions:   {SESSIONS_DIR}")
    print(f"  Logs:       {LOG_DIR}")
    print(f"  Metrics:    {_metrics_file_path()}")
    print(f"  OTLP:       {'✅ Ready (opentelemetry-proto installed)' if _HAS_OTEL_PROTO else '❌ Not available (pip install openclaw-dashboard[otel])'}")
    print(f"  User:       {USER_NAME}")
    print()

    local_ip = get_local_ip()
    print(f"  → http://localhost:{args.port}")
    if local_ip != '127.0.0.1':
        print(f"  → http://{local_ip}:{args.port}")
    if _HAS_OTEL_PROTO:
        print(f"  → OTLP endpoint: http://{local_ip}:{args.port}/v1/metrics")
    print()

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
