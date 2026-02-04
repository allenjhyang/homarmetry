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
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template_string, request, jsonify, Response

__version__ = "0.1.0"

app = Flask(__name__)

# ── Configuration (auto-detected, overridable via CLI/env) ──────────────
WORKSPACE = None
MEMORY_DIR = None
LOG_DIR = None
USER_NAME = None
CET = timezone(timedelta(hours=1))


def detect_config(args=None):
    """Auto-detect OpenClaw/Moltbot paths, with CLI and env overrides."""
    global WORKSPACE, MEMORY_DIR, LOG_DIR, USER_NAME

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
        LOG_DIR = next((d for d in candidates if os.path.isdir(d)), "/tmp/moltbot")

    # 3. User name (shown in Flow visualization)
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
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a14; color: #e0e0e0; min-height: 100vh; }

  .nav { background: #12122a; border-bottom: 1px solid #2a2a4a; padding: 12px 20px; display: flex; align-items: center; gap: 16px; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .nav h1 { font-size: 20px; color: #fff; white-space: nowrap; }
  .nav h1 span { color: #f0c040; }
  .nav-tabs { display: flex; gap: 4px; margin-left: auto; }
  .nav-tab { padding: 8px 16px; border-radius: 8px; background: transparent; border: 1px solid #2a2a4a; color: #888; cursor: pointer; font-size: 13px; font-weight: 600; white-space: nowrap; transition: all 0.15s; }
  .nav-tab:hover { background: #1a1a35; color: #ccc; }
  .nav-tab.active { background: #f0c040; color: #000; border-color: #f0c040; }

  .page { display: none; padding: 16px; max-width: 1200px; margin: 0 auto; }
  .page.active { display: block; }

  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin-bottom: 16px; }
  .card { background: #141428; border: 1px solid #2a2a4a; border-radius: 12px; padding: 16px; }
  .card-title { font-size: 12px; text-transform: uppercase; color: #666; letter-spacing: 1px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
  .card-title .icon { font-size: 16px; }
  .card-value { font-size: 28px; font-weight: 700; color: #fff; }
  .card-sub { font-size: 12px; color: #555; margin-top: 4px; }

  .stat-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #1a1a30; }
  .stat-row:last-child { border-bottom: none; }
  .stat-label { color: #888; font-size: 13px; }
  .stat-val { color: #fff; font-size: 13px; font-weight: 600; }
  .stat-val.green { color: #27ae60; }
  .stat-val.yellow { color: #f0c040; }
  .stat-val.red { color: #e74c3c; }

  .session-item { padding: 12px; border-bottom: 1px solid #1a1a30; }
  .session-item:last-child { border-bottom: none; }
  .session-name { font-weight: 600; font-size: 14px; color: #fff; }
  .session-meta { font-size: 12px; color: #666; margin-top: 4px; display: flex; gap: 12px; flex-wrap: wrap; }
  .session-meta span { display: flex; align-items: center; gap: 4px; }

  .cron-item { padding: 12px; border-bottom: 1px solid #1a1a30; }
  .cron-item:last-child { border-bottom: none; }
  .cron-name { font-weight: 600; font-size: 14px; color: #fff; }
  .cron-schedule { font-size: 12px; color: #f0c040; margin-top: 2px; font-family: monospace; }
  .cron-meta { font-size: 12px; color: #666; margin-top: 4px; }
  .cron-status { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .cron-status.ok { background: #1a3a2a; color: #27ae60; }
  .cron-status.error { background: #3a1a1a; color: #e74c3c; }
  .cron-status.pending { background: #2a2a1a; color: #f0c040; }

  .log-viewer { background: #0a0a14; border: 1px solid #2a2a4a; border-radius: 8px; font-family: 'JetBrains Mono', monospace; font-size: 12px; line-height: 1.6; padding: 12px; max-height: 500px; overflow-y: auto; -webkit-overflow-scrolling: touch; white-space: pre-wrap; word-break: break-all; }
  .log-line { padding: 1px 0; }
  .log-line .ts { color: #666; }
  .log-line .info { color: #60a0ff; }
  .log-line .warn { color: #f0c040; }
  .log-line .err { color: #e74c3c; }
  .log-line .msg { color: #ccc; }

  .memory-item { padding: 10px 12px; border-bottom: 1px solid #1a1a30; display: flex; justify-content: space-between; align-items: center; cursor: pointer; transition: background 0.15s; }
  .memory-item:hover { background: #1a1a35; }
  .memory-item:last-child { border-bottom: none; }
  .file-viewer { background: #0d0d1a; border: 1px solid #2a2a4a; border-radius: 12px; padding: 16px; margin-top: 16px; display: none; }
  .file-viewer-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .file-viewer-title { font-size: 14px; font-weight: 600; color: #f0c040; }
  .file-viewer-close { background: #2a2a4a; border: none; color: #ccc; padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .file-viewer-close:hover { background: #3a3a5a; }
  .file-viewer-content { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px; color: #ccc; white-space: pre-wrap; word-break: break-word; max-height: 60vh; overflow-y: auto; line-height: 1.5; }
  .memory-name { font-weight: 600; font-size: 14px; color: #60a0ff; cursor: pointer; }
  .memory-name:hover { text-decoration: underline; }
  .memory-size { font-size: 12px; color: #555; }

  .refresh-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  .refresh-btn { padding: 8px 16px; background: #2a2a4a; border: none; border-radius: 6px; color: #e0e0e0; cursor: pointer; font-size: 13px; font-weight: 600; }
  .refresh-btn:hover { background: #3a3a5a; }
  .refresh-time { font-size: 12px; color: #555; }
  .pulse { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #27ae60; animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; box-shadow: 0 0 4px #27ae60; } 50% { opacity: 0.3; box-shadow: none; } }
  .live-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; background: #1a3a2a; color: #27ae60; font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; animation: pulse 1.5s infinite; }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .badge.model { background: #1a2a3a; color: #60a0ff; }
  .badge.channel { background: #2a1a3a; color: #a060ff; }
  .badge.tokens { background: #1a3a2a; color: #60ff80; }

  .full-width { grid-column: 1 / -1; }
  .section-title { font-size: 16px; font-weight: 700; color: #fff; margin: 20px 0 12px; display: flex; align-items: center; gap: 8px; }

  /* === Flow Visualization === */
  .flow-container { width: 100%; overflow-x: auto; overflow-y: hidden; position: relative; }
  .flow-stats { display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
  .flow-stat { background: #141428; border: 1px solid #2a2a4a; border-radius: 8px; padding: 8px 14px; flex: 1; min-width: 100px; }
  .flow-stat-label { font-size: 10px; text-transform: uppercase; color: #555; letter-spacing: 1px; display: block; }
  .flow-stat-value { font-size: 20px; font-weight: 700; color: #fff; display: block; margin-top: 2px; }
  #flow-svg { width: 100%; min-width: 800px; height: auto; display: block; }
  #flow-svg text { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 13px; font-weight: 600; fill: #d0d0d0; text-anchor: middle; dominant-baseline: central; pointer-events: none; }
  .flow-node rect { rx: 12; ry: 12; stroke-width: 1.5; transition: all 0.3s ease; }
  .flow-node-channel rect { fill: #161630; stroke: #6a40bf; }
  .flow-node-gateway rect { fill: #141830; stroke: #4080e0; }
  .flow-node-session rect { fill: #142818; stroke: #40c060; }
  .flow-node-brain rect { fill: #221c08; stroke: #f0c040; stroke-width: 2.5; }
  .flow-node-tool rect { fill: #1e1414; stroke: #c05030; }
  .flow-node-channel.active rect { filter: drop-shadow(0 0 10px rgba(106,64,191,0.7)); stroke-width: 2.5; }
  .flow-node-gateway.active rect { filter: drop-shadow(0 0 10px rgba(64,128,224,0.7)); stroke-width: 2.5; }
  .flow-node-session.active rect { filter: drop-shadow(0 0 10px rgba(64,192,96,0.7)); stroke-width: 2.5; }
  .flow-node-tool.active rect { filter: drop-shadow(0 0 10px rgba(224,96,64,0.8)); stroke: #ff8050; stroke-width: 2.5; }
  .flow-path { fill: none; stroke: #1a1a36; stroke-width: 2; stroke-linecap: round; transition: stroke 0.4s, opacity 0.4s; }
  .flow-path.glow-blue { stroke: #4080e0; filter: drop-shadow(0 0 6px rgba(64,128,224,0.6)); }
  .flow-path.glow-yellow { stroke: #f0c040; filter: drop-shadow(0 0 6px rgba(240,192,64,0.6)); }
  .flow-path.glow-green { stroke: #50e080; filter: drop-shadow(0 0 6px rgba(80,224,128,0.6)); }
  .flow-path.glow-red { stroke: #e04040; filter: drop-shadow(0 0 6px rgba(224,64,64,0.6)); }
  @keyframes brainPulse { 0%,100% { filter: drop-shadow(0 0 6px rgba(240,192,64,0.25)); } 50% { filter: drop-shadow(0 0 22px rgba(240,192,64,0.7)); } }
  .brain-group { animation: brainPulse 2.2s ease-in-out infinite; }
  .tool-indicator { opacity: 0.2; transition: opacity 0.3s ease; }
  .tool-indicator.active { opacity: 1; }
  .flow-label { font-size: 9px !important; fill: #333 !important; font-weight: 400 !important; }
  .flow-node-human circle { transition: all 0.3s ease; }
  .flow-node-human.active circle { filter: drop-shadow(0 0 12px rgba(176,128,255,0.7)); }
  @keyframes humanGlow { 0%,100% { filter: drop-shadow(0 0 3px rgba(160,112,224,0.15)); } 50% { filter: drop-shadow(0 0 10px rgba(160,112,224,0.45)); } }
  .flow-node-human { animation: humanGlow 3.5s ease-in-out infinite; }
  .flow-ground { stroke: #20203a; stroke-width: 1; stroke-dasharray: 8 4; }
  .flow-ground-label { font-size: 10px !important; fill: #1e1e38 !important; font-weight: 600 !important; letter-spacing: 4px; }
  .flow-node-infra rect { rx: 6; ry: 6; stroke-width: 2; stroke-dasharray: 5 2; transition: all 0.3s ease; }
  .flow-node-infra text { font-size: 12px !important; }
  .flow-node-infra .infra-sub { font-size: 9px !important; fill: #444 !important; font-weight: 400 !important; }
  .flow-node-runtime rect { fill: #10182a; stroke: #4a7090; }
  .flow-node-machine rect { fill: #141420; stroke: #606880; }
  .flow-node-storage rect { fill: #1a1810; stroke: #806a30; }
  .flow-node-network rect { fill: #0e1c20; stroke: #308080; }
  .flow-node-runtime.active rect { filter: drop-shadow(0 0 10px rgba(74,112,144,0.7)); stroke-dasharray: none; stroke-width: 2.5; }
  .flow-node-machine.active rect { filter: drop-shadow(0 0 10px rgba(96,104,128,0.7)); stroke-dasharray: none; stroke-width: 2.5; }
  .flow-node-storage.active rect { filter: drop-shadow(0 0 10px rgba(128,106,48,0.7)); stroke-dasharray: none; stroke-width: 2.5; }
  .flow-node-network.active rect { filter: drop-shadow(0 0 10px rgba(48,128,128,0.7)); stroke-dasharray: none; stroke-width: 2.5; }
  .flow-path-infra { stroke-dasharray: 6 3; opacity: 0.3; }
  .flow-path.glow-cyan { stroke: #40a0b0; filter: drop-shadow(0 0 6px rgba(64,160,176,0.6)); stroke-dasharray: none; opacity: 1; }
  .flow-path.glow-purple { stroke: #b080ff; filter: drop-shadow(0 0 6px rgba(176,128,255,0.6)); }

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
  }
</style>
</head>
<body>
<div class="nav">
  <h1><span>🦞</span> OpenClaw</h1>
  <div class="nav-tabs">
    <div class="nav-tab active" onclick="switchTab('overview')">Overview</div>
    <div class="nav-tab" onclick="switchTab('sessions')">Sessions</div>
    <div class="nav-tab" onclick="switchTab('crons')">Crons</div>
    <div class="nav-tab" onclick="switchTab('logs')">Logs</div>
    <div class="nav-tab" onclick="switchTab('memory')">Memory</div>
    <div class="nav-tab" onclick="switchTab('flow')">Flow</div>
  </div>
</div>

<!-- OVERVIEW -->
<div class="page active" id="page-overview">
  <div class="refresh-bar">
    <button class="refresh-btn" onclick="loadAll()">↻ Refresh</button>
    <span class="pulse"></span>
    <span class="live-badge">LIVE</span>
    <span class="refresh-time" id="refresh-time">Loading...</span>
  </div>
  <div class="grid">
    <div class="card">
      <div class="card-title"><span class="icon">🧠</span> Model</div>
      <div class="card-value" id="ov-model">—</div>
      <div class="card-sub" id="ov-model-sub"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="icon">💬</span> Active Sessions</div>
      <div class="card-value" id="ov-sessions">—</div>
      <div class="card-sub" id="ov-sessions-sub"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="icon">⏰</span> Cron Jobs</div>
      <div class="card-value" id="ov-crons">—</div>
      <div class="card-sub" id="ov-crons-sub"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="icon">📊</span> Context Tokens</div>
      <div class="card-value" id="ov-tokens">—</div>
      <div class="card-sub" id="ov-tokens-sub"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="icon">💾</span> Memory Files</div>
      <div class="card-value" id="ov-memory">—</div>
      <div class="card-sub" id="ov-memory-sub"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="icon">💻</span> System</div>
      <div id="ov-system"></div>
    </div>
  </div>
  <div class="section-title">📋 Recent Logs</div>
  <div class="log-viewer" id="ov-logs" style="max-height:300px;">Loading...</div>
</div>

<!-- SESSIONS -->
<div class="page" id="page-sessions">
  <div class="refresh-bar"><button class="refresh-btn" onclick="loadSessions()">↻ Refresh</button></div>
  <div class="card" id="sessions-list">Loading...</div>
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
    <select id="log-lines" onchange="loadLogs()" style="background:#1a1a35;color:#e0e0e0;border:1px solid #2a2a4a;padding:6px;border-radius:6px;font-size:13px;">
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

<!-- FLOW -->
<div class="page" id="page-flow">
  <div class="flow-stats">
    <div class="flow-stat"><span class="flow-stat-label">Msgs / min</span><span class="flow-stat-value" id="flow-msg-rate">0</span></div>
    <div class="flow-stat"><span class="flow-stat-label">Events</span><span class="flow-stat-value" id="flow-event-count">0</span></div>
    <div class="flow-stat"><span class="flow-stat-label">Active Tools</span><span class="flow-stat-value" id="flow-active-tools">&mdash;</span></div>
    <div class="flow-stat"><span class="flow-stat-label">Tokens</span><span class="flow-stat-value" id="flow-tokens">&mdash;</span></div>
  </div>
  <div class="flow-container">
    <svg id="flow-svg" viewBox="0 0 1200 950" preserveAspectRatio="xMidYMid meet">
      <defs>
        <pattern id="flow-grid" width="40" height="40" patternUnits="userSpaceOnUse">
          <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#111128" stroke-width="0.5"/>
        </pattern>
      </defs>
      <rect width="1200" height="950" fill="url(#flow-grid)"/>

      <!-- Human → Channel paths -->
      <path class="flow-path" id="path-human-tg"       d="M 100 76 C 116 115, 116 152, 100 178"/>
      <path class="flow-path" id="path-human-sig"      d="M 100 76 C 98 155, 98 275, 100 328"/>
      <path class="flow-path" id="path-human-wa"       d="M 100 76 C 82 190, 82 410, 100 478"/>

      <!-- Connection Paths -->
      <path class="flow-path" id="path-tg-gw"          d="M 165 200 C 210 200, 220 310, 260 335"/>
      <path class="flow-path" id="path-sig-gw"         d="M 165 350 L 260 350"/>
      <path class="flow-path" id="path-wa-gw"          d="M 165 500 C 210 500, 220 390, 260 365"/>
      <path class="flow-path" id="path-gw-brain"       d="M 380 350 C 425 350, 440 365, 480 365"/>
      <path class="flow-path" id="path-brain-session"   d="M 570 310 L 570 185"/>
      <path class="flow-path" id="path-brain-exec"      d="M 660 335 C 720 310, 770 160, 810 150"/>
      <path class="flow-path" id="path-brain-browser"   d="M 660 350 C 760 340, 880 260, 920 255"/>
      <path class="flow-path" id="path-brain-search"    d="M 660 370 C 790 370, 920 380, 960 380"/>
      <path class="flow-path" id="path-brain-cron"      d="M 660 385 C 760 400, 880 500, 920 510"/>
      <path class="flow-path" id="path-brain-tts"       d="M 660 400 C 720 450, 770 570, 810 585"/>
      <path class="flow-path" id="path-brain-memory"    d="M 610 420 C 630 520, 660 600, 670 620"/>

      <!-- Infrastructure paths -->
      <path class="flow-path flow-path-infra" id="path-gw-network"      d="M 320 377 C 320 570, 720 710, 960 785"/>
      <path class="flow-path flow-path-infra" id="path-brain-runtime"   d="M 540 420 C 520 570, 310 710, 260 785"/>
      <path class="flow-path flow-path-infra" id="path-brain-machine"   d="M 570 420 C 570 570, 510 710, 500 785"/>
      <path class="flow-path flow-path-infra" id="path-memory-storage"  d="M 725 639 C 730 695, 738 750, 740 785"/>

      <!-- Human Origin -->
      <g class="flow-node flow-node-human" id="node-human">
        <circle cx="100" cy="48" r="28" fill="#0e0c22" stroke="#b080ff" stroke-width="2"/>
        <circle cx="100" cy="40" r="7" fill="#9070d0" opacity="0.45"/>
        <path d="M 86 56 Q 86 65 100 65 Q 114 65 114 56" fill="#9070d0" opacity="0.3"/>
        <text x="100" y="92" style="font-size:13px;fill:#c0a8f0;font-weight:700;" id="flow-human-name">You</text>
        <text x="100" y="106" style="font-size:9px;fill:#3a3a5a;">origin</text>
      </g>

      <!-- Channel Nodes -->
      <g class="flow-node flow-node-channel" id="node-telegram">
        <rect x="35" y="178" width="130" height="44"/>
        <text x="100" y="203">&#x1F4F1; Telegram</text>
      </g>
      <g class="flow-node flow-node-channel" id="node-signal">
        <rect x="35" y="328" width="130" height="44"/>
        <text x="100" y="353">&#x1F4E1; Signal</text>
      </g>
      <g class="flow-node flow-node-channel" id="node-whatsapp">
        <rect x="35" y="478" width="130" height="44"/>
        <text x="100" y="503">&#x1F4AC; WhatsApp</text>
      </g>

      <!-- Gateway -->
      <g class="flow-node flow-node-gateway" id="node-gateway">
        <rect x="260" y="323" width="120" height="54"/>
        <text x="320" y="354">&#x1F500; Gateway</text>
      </g>

      <!-- Session / Context -->
      <g class="flow-node flow-node-session" id="node-session">
        <rect x="495" y="132" width="150" height="50"/>
        <text x="570" y="160">&#x1F4BE; Session</text>
      </g>

      <!-- Brain -->
      <g class="flow-node flow-node-brain brain-group" id="node-brain">
        <rect x="480" y="310" width="180" height="110"/>
        <text x="570" y="345" style="font-size:24px;">&#x1F9E0;</text>
        <text x="570" y="374" style="font-size:14px;font-weight:700;fill:#f0c040;" id="brain-model-label">Claude</text>
        <text x="570" y="394" style="font-size:10px;fill:#777;" id="brain-model-text">AI Model</text>
        <circle cx="570" cy="410" r="4" fill="#e04040">
          <animate attributeName="r" values="3;5;3" dur="1.1s" repeatCount="indefinite"/>
          <animate attributeName="opacity" values="0.5;1;0.5" dur="1.1s" repeatCount="indefinite"/>
        </circle>
      </g>

      <!-- Tool Nodes -->
      <g class="flow-node flow-node-tool" id="node-exec">
        <rect x="810" y="131" width="100" height="38"/>
        <text x="860" y="153">&#x26A1; exec</text>
        <circle class="tool-indicator" id="ind-exec" cx="905" cy="137" r="4" fill="#e06040"/>
      </g>
      <g class="flow-node flow-node-tool" id="node-browser">
        <rect x="920" y="236" width="110" height="38"/>
        <text x="975" y="258">&#x1F310; browser</text>
        <circle class="tool-indicator" id="ind-browser" cx="1025" cy="242" r="4" fill="#e06040"/>
      </g>
      <g class="flow-node flow-node-tool" id="node-search">
        <rect x="960" y="361" width="130" height="38"/>
        <text x="1025" y="383">&#x1F50D; web_search</text>
        <circle class="tool-indicator" id="ind-search" cx="1085" cy="367" r="4" fill="#e06040"/>
      </g>
      <g class="flow-node flow-node-tool" id="node-cron">
        <rect x="920" y="491" width="100" height="38"/>
        <text x="970" y="513">&#x23F0; cron</text>
        <circle class="tool-indicator" id="ind-cron" cx="1015" cy="497" r="4" fill="#e06040"/>
      </g>
      <g class="flow-node flow-node-tool" id="node-tts">
        <rect x="810" y="566" width="100" height="38"/>
        <text x="860" y="588">&#x1F50A; tts</text>
        <circle class="tool-indicator" id="ind-tts" cx="905" cy="572" r="4" fill="#e06040"/>
      </g>
      <g class="flow-node flow-node-tool" id="node-memory">
        <rect x="670" y="601" width="110" height="38"/>
        <text x="725" y="623">&#x1F4DD; memory</text>
        <circle class="tool-indicator" id="ind-memory" cx="775" cy="607" r="4" fill="#e06040"/>
      </g>

      <!-- Flow direction labels -->
      <text class="flow-label" x="195" y="255">inbound</text>
      <text class="flow-label" x="420" y="342">dispatch</text>
      <text class="flow-label" x="548" y="250">context</text>
      <text class="flow-label" x="750" y="320">tools</text>

      <!-- Infrastructure Layer -->
      <line class="flow-ground" x1="80" y1="755" x2="1120" y2="755"/>
      <text class="flow-ground-label" x="600" y="772" style="text-anchor:middle;">I N F R A S T R U C T U R E</text>

      <g class="flow-node flow-node-infra flow-node-runtime" id="node-runtime">
        <rect x="165" y="785" width="190" height="55"/>
        <text x="260" y="808" style="font-size:13px !important;">&#x2699;&#xFE0F; Runtime</text>
        <text class="infra-sub" x="260" y="826" id="infra-runtime-text">Node.js · Linux</text>
      </g>
      <g class="flow-node flow-node-infra flow-node-machine" id="node-machine">
        <rect x="405" y="785" width="190" height="55"/>
        <text x="500" y="808" style="font-size:13px !important;">&#x1F5A5;&#xFE0F; Machine</text>
        <text class="infra-sub" x="500" y="826" id="infra-machine-text">Host</text>
      </g>
      <g class="flow-node flow-node-infra flow-node-storage" id="node-storage">
        <rect x="645" y="785" width="190" height="55"/>
        <text x="740" y="808" style="font-size:13px !important;">&#x1F4BF; Storage</text>
        <text class="infra-sub" x="740" y="826" id="infra-storage-text">Disk</text>
      </g>
      <g class="flow-node flow-node-infra flow-node-network" id="node-network">
        <rect x="885" y="785" width="190" height="55"/>
        <text x="980" y="808" style="font-size:13px !important;">&#x1F310; Network</text>
        <text class="infra-sub" x="980" y="826" id="infra-network-text">LAN</text>
      </g>

      <!-- Infra labels -->
      <text class="flow-label" x="440" y="680">runtime</text>
      <text class="flow-label" x="570" y="650">host</text>
      <text class="flow-label" x="720" y="710">disk I/O</text>
      <text class="flow-label" x="870" y="660">network</text>
    </svg>
  </div>
</div>

<script>
function switchTab(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'sessions') loadSessions();
  if (name === 'crons') loadCrons();
  if (name === 'logs') loadLogs();
  if (name === 'memory') loadMemory();
  if (name === 'flow') initFlow();
}

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
  var [overview, logs] = await Promise.all([
    fetch('/api/overview').then(r => r.json()),
    fetch('/api/logs?lines=30').then(r => r.json())
  ]);

  document.getElementById('ov-model').textContent = overview.model || '—';
  document.getElementById('ov-model-sub').textContent = 'Provider: ' + (overview.provider || 'anthropic');
  document.getElementById('ov-sessions').textContent = overview.sessionCount;
  document.getElementById('ov-sessions-sub').textContent = 'Main: ' + timeAgo(overview.mainSessionUpdated);
  document.getElementById('ov-crons').textContent = overview.cronCount;
  document.getElementById('ov-crons-sub').textContent = overview.cronEnabled + ' enabled, ' + overview.cronDisabled + ' disabled';
  document.getElementById('ov-tokens').textContent = (overview.mainTokens / 1000).toFixed(0) + 'K';
  document.getElementById('ov-tokens-sub').textContent = 'of ' + (overview.contextWindow / 1000) + 'K context window (' + ((overview.mainTokens/overview.contextWindow)*100).toFixed(0) + '% used)';
  document.getElementById('ov-memory').textContent = overview.memoryCount;
  document.getElementById('ov-memory-sub').textContent = (overview.memorySize / 1024).toFixed(1) + ' KB total';

  var sysHtml = '';
  overview.system.forEach(function(s) {
    sysHtml += '<div class="stat-row"><span class="stat-label">' + s[0] + '</span><span class="stat-val ' + (s[2]||'') + '">' + s[1] + '</span></div>';
  });
  document.getElementById('ov-system').innerHTML = sysHtml;

  renderLogs('ov-logs', logs.lines);
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
      if (obj["0"]) extras.push(obj["0"]);
      if (obj["1"]) extras.push(obj["1"]);
      if (msg && extras.length) display = msg + ' | ' + extras.join(' ');
      else if (extras.length) display = extras.join(' ');
      else if (!msg) display = l.substring(0, 200);
      else display = msg;
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
  var data = await fetch('/api/sessions').then(r => r.json());
  var html = '';
  data.sessions.forEach(function(s) {
    html += '<div class="session-item">';
    html += '<div class="session-name">' + escHtml(s.displayName || s.key) + '</div>';
    html += '<div class="session-meta">';
    html += '<span><span class="badge model">' + (s.model||'default') + '</span></span>';
    if (s.channel !== 'unknown') html += '<span><span class="badge channel">' + s.channel + '</span></span>';
    html += '<span><span class="badge tokens">' + (s.totalTokens/1000).toFixed(0) + 'K tokens</span></span>';
    html += '<span>Updated ' + timeAgo(s.updatedAt) + '</span>';
    html += '</div></div>';
  });
  document.getElementById('sessions-list').innerHTML = html || 'No sessions';
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
  }).catch(function(){});
  setInterval(updateFlowStats, 2000);
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

function animateParticle(pathId, color, duration, reverse) {
  var path = document.getElementById(pathId);
  if (!path) return;
  var svg = document.getElementById('flow-svg');
  if (!svg) return;
  var len = path.getTotalLength();
  var particle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  particle.setAttribute('r', '5');
  particle.setAttribute('fill', color);
  particle.style.filter = 'drop-shadow(0 0 8px ' + color + ')';
  svg.appendChild(particle);
  var glowCls = color === '#60a0ff' ? 'glow-blue' : color === '#f0c040' ? 'glow-yellow' : color === '#50e080' ? 'glow-green' : color === '#40a0b0' ? 'glow-cyan' : color === '#c0a0ff' ? 'glow-purple' : 'glow-red';
  path.classList.add(glowCls);
  var startT = performance.now();
  var trailN = 0;
  function step(now) {
    var t = Math.min((now - startT) / duration, 1);
    var dist = reverse ? (1 - t) * len : t * len;
    try {
      var pt = path.getPointAtLength(dist);
      particle.setAttribute('cx', pt.x);
      particle.setAttribute('cy', pt.y);
    } catch(e) { particle.remove(); path.classList.remove(glowCls); return; }
    if (trailN++ % 4 === 0) {
      var tr = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      tr.setAttribute('cx', particle.getAttribute('cx'));
      tr.setAttribute('cy', particle.getAttribute('cy'));
      tr.setAttribute('r', '3');
      tr.setAttribute('fill', color);
      tr.setAttribute('opacity', '0.5');
      svg.insertBefore(tr, particle);
      var trS = now;
      (function(el, s) {
        function fade(n) {
          var a = (n - s) / 400;
          if (a >= 1) { el.remove(); return; }
          el.setAttribute('opacity', String(0.5 * (1 - a)));
          el.setAttribute('r', String(3 * (1 - a * 0.5)));
          requestAnimationFrame(fade);
        }
        requestAnimationFrame(fade);
      })(tr, trS);
    }
    if (t < 1) requestAnimationFrame(step);
    else {
      particle.remove();
      setTimeout(function() { path.classList.remove(glowCls); }, 400);
    }
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

var flowThrottles = {};
function processFlowEvent(line) {
  flowStats.events++;
  var now = Date.now();
  var msg = '', level = '';
  try {
    var obj = JSON.parse(line);
    msg = ((obj.msg || '') + ' ' + (obj.message || '') + ' ' + (obj.name || '') + ' ' + (obj['0'] || '') + ' ' + (obj['1'] || '')).toLowerCase();
    level = (obj.logLevelName || obj.level || '').toLowerCase();
  } catch(e) { msg = line.toLowerCase(); }

  if (level === 'error' || level === 'fatal') { triggerError(); return; }

  if (msg.includes('run start') && msg.includes('messagechannel')) {
    if (now - (flowThrottles['inbound']||0) < 500) return;
    flowThrottles['inbound'] = now;
    var ch = 'tg';
    if (msg.includes('signal')) ch = 'sig';
    else if (msg.includes('whatsapp')) ch = 'wa';
    triggerInbound(ch);
    flowStats.msgTimestamps.push(now);
    return;
  }
  if (msg.includes('inbound') || msg.includes('dispatching') || msg.includes('message received')) {
    triggerInbound('tg');
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
    triggerOutbound(ch);
    return;
  }
}
</script>
</body>
</html>
"""


# ── API Routes ──────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(DASHBOARD_HTML)


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


@app.route('/api/logs')
def api_logs():
    lines_count = int(request.args.get('lines', 100))
    today = datetime.now().strftime('%Y-%m-%d')
    log_file = os.path.join(LOG_DIR, f'moltbot-{today}.log')
    lines = []
    if os.path.exists(log_file):
        result = subprocess.run(['tail', f'-{lines_count}', log_file], capture_output=True, text=True)
        lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
    return jsonify({'lines': lines})


@app.route('/api/logs-stream')
def api_logs_stream():
    """SSE endpoint — streams new log lines in real-time."""
    today = datetime.now().strftime('%Y-%m-%d')
    log_file = os.path.join(LOG_DIR, f'moltbot-{today}.log')

    def generate():
        if not os.path.exists(log_file):
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


# ── Data Helpers ────────────────────────────────────────────────────────

def _get_sessions():
    """Read active sessions from the session directory."""
    sessions = []
    try:
        base = os.path.expanduser('~/.clawdbot/agents/main/sessions')
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
"""


def main():
    parser = argparse.ArgumentParser(
        description="OpenClaw Dashboard — Real-time observability for your AI agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Environment variables:\n"
               "  OPENCLAW_HOME       Agent workspace directory\n"
               "  OPENCLAW_LOG_DIR    Log directory (default: /tmp/moltbot)\n"
               "  OPENCLAW_USER       Your name in the Flow visualization\n"
    )
    parser.add_argument('--port', '-p', type=int, default=8900, help='Port (default: 8900)')
    parser.add_argument('--host', '-H', type=str, default='0.0.0.0', help='Host (default: 0.0.0.0)')
    parser.add_argument('--workspace', '-w', type=str, help='Agent workspace directory')
    parser.add_argument('--log-dir', '-l', type=str, help='Log directory')
    parser.add_argument('--name', '-n', type=str, help='Your name (shown in Flow tab)')
    parser.add_argument('--version', '-v', action='version', version=f'openclaw-dashboard {__version__}')

    args = parser.parse_args()
    detect_config(args)

    # Print banner
    print(BANNER.format(version=__version__))
    print(f"  Workspace:  {WORKSPACE}")
    print(f"  Logs:       {LOG_DIR}")
    print(f"  User:       {USER_NAME}")
    print()

    local_ip = get_local_ip()
    print(f"  → http://localhost:{args.port}")
    if local_ip != '127.0.0.1':
        print(f"  → http://{local_ip}:{args.port}")
    print()

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == '__main__':
    main()
