"""
Web3 Signals Dashboard — Production UI.

Serves a full single-page dashboard at /dashboard that visualizes
all signal data, agent health, and portfolio insights.
"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Web3 Signals Intelligence</title>
<style>
  :root {
    --bg: #0a0b0f;
    --surface: #12131a;
    --surface2: #1a1b25;
    --border: #2a2b35;
    --text: #e4e4e7;
    --text-dim: #8b8b95;
    --green: #22c55e;
    --green-bg: rgba(34,197,94,0.1);
    --red: #ef4444;
    --red-bg: rgba(239,68,68,0.1);
    --yellow: #eab308;
    --yellow-bg: rgba(234,179,8,0.1);
    --blue: #3b82f6;
    --blue-bg: rgba(59,130,246,0.1);
    --purple: #a855f7;
    --purple-bg: rgba(168,85,247,0.1);
    --cyan: #06b6d4;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* Header */
  .header {
    padding: 20px 32px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--surface);
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .header-left { display: flex; align-items: center; gap: 16px; }
  .header h1 { font-size: 20px; font-weight: 600; letter-spacing: -0.5px; }
  .header h1 span { color: var(--cyan); }
  .header-right { display: flex; align-items: center; gap: 16px; }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--green); display: inline-block;
    animation: pulse 2s infinite;
  }
  .status-dot.offline { background: var(--red); animation: none; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  .last-update { color: var(--text-dim); font-size: 13px; }
  .refresh-btn {
    background: var(--surface2); border: 1px solid var(--border);
    color: var(--text); padding: 6px 14px; border-radius: 6px;
    cursor: pointer; font-size: 13px; transition: all 0.2s;
  }
  .refresh-btn:hover { border-color: var(--cyan); color: var(--cyan); }

  /* Layout */
  .main { padding: 24px 32px; max-width: 1600px; margin: 0 auto; }

  /* Portfolio Summary Bar */
  .portfolio-bar {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }
  .portfolio-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
  }
  .portfolio-card .label { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim); margin-bottom: 6px; }
  .portfolio-card .value { font-size: 22px; font-weight: 700; }
  .portfolio-card .value.fear { color: var(--red); }
  .portfolio-card .value.greed { color: var(--green); }
  .portfolio-card .value.neutral { color: var(--yellow); }
  .portfolio-card .sub { font-size: 12px; color: var(--text-dim); margin-top: 4px; }

  /* Agent Status Strip */
  .agents-strip {
    display: flex; gap: 10px; margin-bottom: 24px;
    overflow-x: auto; padding-bottom: 4px;
  }
  .agent-chip {
    display: flex; align-items: center; gap: 8px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 14px; white-space: nowrap;
    font-size: 13px; flex-shrink: 0;
  }
  .agent-chip .dot {
    width: 6px; height: 6px; border-radius: 50%;
  }
  .agent-chip .dot.ok { background: var(--green); }
  .agent-chip .dot.err { background: var(--red); }
  .agent-chip .dot.warn { background: var(--yellow); }
  .agent-chip .name { font-weight: 500; }
  .agent-chip .meta { color: var(--text-dim); font-size: 11px; }

  /* LLM Insight Banner */
  .insight-banner {
    background: linear-gradient(135deg, rgba(6,182,212,0.08), rgba(168,85,247,0.08));
    border: 1px solid rgba(6,182,212,0.2);
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 24px;
    line-height: 1.6;
    font-size: 14px;
  }
  .insight-banner .insight-label {
    font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
    color: var(--cyan); margin-bottom: 8px; font-weight: 600;
  }
  .insight-banner strong { color: var(--text); }

  /* Tabs */
  .tabs {
    display: flex; gap: 4px; margin-bottom: 20px;
    border-bottom: 1px solid var(--border); padding-bottom: 0;
  }
  .tab {
    padding: 10px 20px; cursor: pointer; border: none;
    background: none; color: var(--text-dim); font-size: 14px;
    font-weight: 500; border-bottom: 2px solid transparent;
    transition: all 0.2s;
  }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--cyan); border-bottom-color: var(--cyan); }

  /* History view */
  .history-controls {
    display: flex; gap: 12px; align-items: center; margin-bottom: 16px; flex-wrap: wrap;
  }
  .history-controls select, .history-controls button {
    background: var(--surface2); border: 1px solid var(--border);
    color: var(--text); padding: 8px 14px; border-radius: 6px;
    font-size: 13px; cursor: pointer;
  }
  .history-controls select:hover, .history-controls button:hover {
    border-color: var(--cyan);
  }
  .history-controls .page-info {
    color: var(--text-dim); font-size: 13px;
  }

  .history-table {
    width: 100%; border-collapse: collapse;
    background: var(--surface); border-radius: 10px; overflow: hidden;
    border: 1px solid var(--border);
  }
  .history-table th {
    text-align: left; padding: 12px 16px; font-size: 11px;
    text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim);
    background: var(--surface2); border-bottom: 1px solid var(--border);
  }
  .history-table td {
    padding: 10px 16px; font-size: 13px; border-bottom: 1px solid var(--border);
    vertical-align: top;
  }
  .history-table tr:last-child td { border-bottom: none; }
  .history-table tr:hover { background: var(--surface2); }
  .history-table tr { cursor: pointer; transition: background 0.15s; }

  .run-status { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .run-status.ok { background: var(--green-bg); color: var(--green); }
  .run-status.partial { background: var(--yellow-bg); color: var(--yellow); }
  .run-status.error { background: var(--red-bg); color: var(--red); }

  .expand-row { display: none; }
  .expand-row.open { display: table-row; }
  .expand-row td {
    background: var(--surface2); padding: 16px 20px;
  }
  .expand-content {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 10px;
  }
  .expand-asset {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 14px;
  }
  .expand-asset .ea-name { font-weight: 700; font-size: 14px; margin-bottom: 4px; }
  .expand-asset .ea-score { font-size: 20px; font-weight: 800; }
  .expand-asset .ea-label { font-size: 11px; color: var(--text-dim); }
  .expand-asset .ea-dims { font-size: 11px; color: var(--text-dim); margin-top: 6px; line-height: 1.6; }

  /* Signal Grid */
  .signal-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 12px;
    margin-bottom: 32px;
  }

  .signal-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
    cursor: pointer;
    transition: all 0.2s;
    position: relative;
    overflow: hidden;
  }
  .signal-card:hover {
    border-color: var(--cyan);
    transform: translateY(-1px);
    box-shadow: 0 4px 20px rgba(6,182,212,0.1);
  }
  .signal-card .score-stripe {
    position: absolute; top: 0; left: 0; width: 4px; height: 100%;
  }
  .signal-card .score-stripe.buy { background: var(--green); }
  .signal-card .score-stripe.sell { background: var(--red); }
  .signal-card .score-stripe.neutral { background: var(--yellow); }

  .card-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; padding-left: 8px; }
  .card-top .asset { font-size: 18px; font-weight: 700; }
  .card-top .score {
    font-size: 28px; font-weight: 800; line-height: 1;
  }
  .card-top .score.buy { color: var(--green); }
  .card-top .score.sell { color: var(--red); }
  .card-top .score.neutral { color: var(--yellow); }

  .card-label {
    display: inline-block; padding: 3px 10px; border-radius: 4px;
    font-size: 11px; font-weight: 600; letter-spacing: 0.5px;
    margin-bottom: 10px; margin-left: 8px;
  }
  .card-label.strong-buy { background: var(--green-bg); color: var(--green); }
  .card-label.moderate-buy { background: var(--green-bg); color: var(--green); }
  .card-label.neutral { background: var(--yellow-bg); color: var(--yellow); }
  .card-label.moderate-sell { background: var(--red-bg); color: var(--red); }
  .card-label.strong-sell { background: var(--red-bg); color: var(--red); }

  /* Dimension bars */
  .dimensions { padding-left: 8px; }
  .dim-row {
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 6px; font-size: 12px;
  }
  .dim-row .dim-name { width: 75px; color: var(--text-dim); text-transform: capitalize; }
  .dim-row .dim-bar-bg {
    flex: 1; height: 6px; background: var(--surface2);
    border-radius: 3px; overflow: hidden;
  }
  .dim-row .dim-bar {
    height: 100%; border-radius: 3px;
    transition: width 0.6s ease;
  }
  .dim-row .dim-bar.high { background: var(--green); }
  .dim-row .dim-bar.mid { background: var(--yellow); }
  .dim-row .dim-bar.low { background: var(--red); }
  .dim-row .dim-score { width: 30px; text-align: right; font-weight: 600; font-size: 11px; }

  /* Detail modal */
  .modal-overlay {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.7); z-index: 200;
    justify-content: center; align-items: center; padding: 24px;
  }
  .modal-overlay.active { display: flex; }
  .modal {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 14px; max-width: 640px; width: 100%;
    max-height: 80vh; overflow-y: auto; padding: 28px;
  }
  .modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
  .modal-header h2 { font-size: 24px; font-weight: 700; }
  .modal-close {
    background: none; border: none; color: var(--text-dim);
    font-size: 24px; cursor: pointer; padding: 4px 8px;
  }
  .modal-close:hover { color: var(--text); }
  .modal-score { font-size: 48px; font-weight: 800; margin-bottom: 4px; }
  .modal-dim-detail { margin: 16px 0; }
  .modal-dim-item {
    display: flex; justify-content: space-between; align-items: center;
    padding: 12px 0; border-bottom: 1px solid var(--border);
  }
  .modal-dim-item:last-child { border-bottom: none; }
  .modal-dim-item .left { display: flex; flex-direction: column; gap: 2px; }
  .modal-dim-item .dim-title { font-weight: 600; text-transform: capitalize; }
  .modal-dim-item .dim-detail { font-size: 12px; color: var(--text-dim); }
  .modal-dim-item .dim-badge {
    padding: 4px 12px; border-radius: 6px;
    font-size: 12px; font-weight: 600;
  }
  .modal-insight {
    background: var(--surface2); border-radius: 10px;
    padding: 16px 20px; margin-top: 16px; font-size: 13px;
    line-height: 1.7; color: var(--text-dim);
  }
  .modal-insight strong { color: var(--text); }

  /* Table view */
  .signal-table {
    width: 100%; border-collapse: collapse;
    background: var(--surface); border-radius: 10px; overflow: hidden;
    border: 1px solid var(--border);
  }
  .signal-table th {
    text-align: left; padding: 12px 16px; font-size: 11px;
    text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim);
    background: var(--surface2); border-bottom: 1px solid var(--border);
    cursor: pointer; user-select: none;
  }
  .signal-table th:hover { color: var(--text); }
  .signal-table th.sorted { color: var(--cyan); }
  .signal-table td { padding: 12px 16px; font-size: 13px; border-bottom: 1px solid var(--border); }
  .signal-table tr:last-child td { border-bottom: none; }
  .signal-table tr:hover { background: var(--surface2); }
  .signal-table tr { cursor: pointer; transition: background 0.15s; }

  .table-score { font-weight: 700; font-size: 15px; }

  /* Loading */
  .loading {
    display: flex; justify-content: center; align-items: center;
    min-height: 400px; flex-direction: column; gap: 16px;
  }
  .spinner {
    width: 40px; height: 40px; border: 3px solid var(--border);
    border-top-color: var(--cyan); border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Responsive */
  @media (max-width: 768px) {
    .header { padding: 16px; }
    .main { padding: 16px; }
    .signal-grid { grid-template-columns: 1fr; }
    .portfolio-bar { grid-template-columns: repeat(2, 1fr); }
    .modal { padding: 20px; }
  }

  /* Performance view */
  .perf-header {
    display: grid;
    grid-template-columns: 280px 1fr;
    gap: 24px;
    margin-bottom: 28px;
  }
  .perf-score-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 28px;
    text-align: center;
  }
  .perf-score-card .perf-big {
    font-size: 64px; font-weight: 800; line-height: 1;
  }
  .perf-score-card .perf-label {
    font-size: 13px; color: var(--text-dim); margin-top: 8px;
  }
  .perf-score-card .perf-sub {
    font-size: 12px; color: var(--text-dim); margin-top: 12px; line-height: 1.6;
  }
  .perf-timeframes {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    align-content: center;
  }
  .perf-tf-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px;
    text-align: center;
  }
  .perf-tf-card .tf-label {
    font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
    color: var(--text-dim); margin-bottom: 8px;
  }
  .perf-tf-card .tf-value {
    font-size: 32px; font-weight: 700;
  }
  .perf-tf-card .tf-detail {
    font-size: 11px; color: var(--text-dim); margin-top: 6px;
  }

  .perf-section-title {
    font-size: 14px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 1px; color: var(--text-dim); margin-bottom: 14px;
    margin-top: 8px;
  }

  .perf-asset-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 10px;
    margin-bottom: 28px;
  }
  .perf-asset-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .perf-asset-card .pa-name { font-weight: 700; font-size: 14px; }
  .perf-asset-card .pa-acc { font-size: 20px; font-weight: 700; }

  .perf-signals-table {
    width: 100%; border-collapse: collapse;
    background: var(--surface); border-radius: 10px; overflow: hidden;
    border: 1px solid var(--border);
  }
  .perf-signals-table th {
    text-align: left; padding: 12px 16px; font-size: 11px;
    text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim);
    background: var(--surface2); border-bottom: 1px solid var(--border);
  }
  .perf-signals-table td {
    padding: 10px 16px; font-size: 13px; border-bottom: 1px solid var(--border);
    vertical-align: top;
  }
  .perf-signals-table tr:last-child td { border-bottom: none; }

  .dir-badge {
    display: inline-block; padding: 3px 10px; border-radius: 4px;
    font-size: 11px; font-weight: 600; letter-spacing: 0.5px;
  }
  .dir-badge.bullish { background: var(--green-bg); color: var(--green); }
  .dir-badge.bearish { background: var(--red-bg); color: var(--red); }
  .dir-badge.neutral { background: var(--yellow-bg); color: var(--yellow); }

  .perf-collecting {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 48px;
    text-align: center;
  }
  .perf-collecting .pc-icon { font-size: 48px; margin-bottom: 16px; }
  .perf-collecting .pc-title { font-size: 20px; font-weight: 700; margin-bottom: 8px; }
  .perf-collecting .pc-sub { color: var(--text-dim); font-size: 14px; line-height: 1.6; }
  .perf-collecting .pc-count { font-size: 32px; font-weight: 800; color: var(--cyan); margin: 16px 0 4px; }
  .perf-collecting .pc-count-label { font-size: 12px; color: var(--text-dim); }

  .perf-methodology {
    background: var(--surface2);
    border-radius: 10px;
    padding: 16px 20px;
    font-size: 12px;
    color: var(--text-dim);
    line-height: 1.8;
    margin-top: 8px;
  }
  .perf-methodology strong { color: var(--text); }

  /* Analytics view */
  .analytics-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }
  .analytics-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px;
    text-align: center;
  }
  .analytics-card .ac-label {
    font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
    color: var(--text-dim); margin-bottom: 6px;
  }
  .analytics-card .ac-value {
    font-size: 32px; font-weight: 700;
  }
  .analytics-card .ac-sub {
    font-size: 12px; color: var(--text-dim); margin-top: 4px;
  }

  .ua-breakdown {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 10px;
    margin-bottom: 24px;
  }
  .ua-chip {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .ua-chip .ua-name { font-weight: 600; font-size: 14px; text-transform: capitalize; }
  .ua-chip .ua-count { font-size: 20px; font-weight: 700; color: var(--cyan); }
  .ua-chip.ai-agent { border-color: var(--green); }
  .ua-chip.ai-agent .ua-count { color: var(--green); }

  .daily-chart {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px;
    margin-bottom: 24px;
  }
  .daily-bars {
    display: flex; align-items: flex-end; gap: 4px;
    height: 120px; padding-top: 10px;
  }
  .daily-bar-wrap {
    flex: 1; display: flex; flex-direction: column;
    align-items: center; height: 100%;
    justify-content: flex-end;
  }
  .daily-bar {
    width: 100%; max-width: 40px; background: var(--cyan);
    border-radius: 4px 4px 0 0; min-height: 2px;
    transition: height 0.3s;
  }
  .daily-bar-label {
    font-size: 10px; color: var(--text-dim); margin-top: 6px;
    writing-mode: vertical-rl; text-orientation: mixed;
  }
  .daily-bar-count {
    font-size: 10px; color: var(--text-dim); margin-bottom: 4px;
  }

  .endpoint-table {
    width: 100%; border-collapse: collapse;
    background: var(--surface); border-radius: 10px; overflow: hidden;
    border: 1px solid var(--border); margin-bottom: 24px;
  }
  .endpoint-table th {
    text-align: left; padding: 12px 16px; font-size: 11px;
    text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim);
    background: var(--surface2); border-bottom: 1px solid var(--border);
  }
  .endpoint-table td {
    padding: 10px 16px; font-size: 13px; border-bottom: 1px solid var(--border);
  }
  .endpoint-table tr:last-child td { border-bottom: none; }

  .ep-bar-bg {
    height: 8px; background: var(--surface2); border-radius: 4px;
    overflow: hidden; margin-top: 4px;
  }
  .ep-bar { height: 100%; background: var(--cyan); border-radius: 4px; }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  @media (max-width: 768px) {
    .perf-header { grid-template-columns: 1fr; }
    .perf-timeframes { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <h1><span>W3</span> Signal Intelligence</h1>
  </div>
  <div class="header-right">
    <span class="status-dot" id="statusDot"></span>
    <span class="last-update" id="lastUpdate">Loading...</span>
    <button class="refresh-btn" onclick="fetchAll()">Refresh</button>
  </div>
</div>

<div class="main">
  <!-- Portfolio Summary -->
  <div class="portfolio-bar" id="portfolioBar"></div>

  <!-- Agent Status -->
  <div class="agents-strip" id="agentsStrip"></div>

  <!-- LLM Insight -->
  <div class="insight-banner" id="insightBanner" style="display:none;"></div>

  <!-- Tabs -->
  <div class="tabs">
    <button class="tab active" data-view="grid" onclick="switchView('grid', this)">Cards</button>
    <button class="tab" data-view="table" onclick="switchView('table', this)">Table</button>
    <button class="tab" data-view="performance" onclick="switchView('performance', this)">Performance</button>
    <button class="tab" data-view="analytics" onclick="switchView('analytics', this)">Analytics</button>
    <button class="tab" data-view="history" onclick="switchView('history', this)">History</button>
  </div>

  <!-- Content -->
  <div id="content">
    <div class="loading"><div class="spinner"></div><span style="color:var(--text-dim)">Loading signals...</span></div>
  </div>
</div>

<!-- Detail Modal -->
<div class="modal-overlay" id="modalOverlay" onclick="closeModal(event)">
  <div class="modal" id="modal" onclick="event.stopPropagation()"></div>
</div>

<script>
let signalData = null;
let healthData = null;
let perfData = null;
let analyticsData = null;
let currentView = 'grid';
let sortField = 'score';
let sortDir = -1; // descending

const API_BASE = '';

async function fetchAll() {
  const content = document.getElementById('content');

  // Show loading state with timeout feedback
  let loadTimer = setTimeout(() => {
    content.innerHTML = '<div class="loading"><div class="spinner"></div><span style="color:var(--text-dim)">Agents are computing signals... this can take up to 60s on first load.</span></div>';
  }, 5000);

  try {
    // Fetch health first (always fast), then signal (may be slow on first load)
    const healthRes = await fetch(API_BASE + '/health');
    healthData = await healthRes.json();
    renderAgents();

    const sigRes = await fetch(API_BASE + '/api/signal');
    clearTimeout(loadTimer);

    if (!sigRes.ok) {
      throw new Error(`Signal API returned ${sigRes.status}`);
    }

    signalData = await sigRes.json();

    document.getElementById('statusDot').className = 'status-dot';
    document.getElementById('lastUpdate').textContent =
      'Updated: ' + new Date(signalData.timestamp || Date.now()).toLocaleTimeString();

    // Fetch performance data (non-blocking)
    try {
      const perfRes = await fetch(API_BASE + '/api/performance/reputation');
      perfData = await perfRes.json();
    } catch(e) { perfData = null; }

    // Fetch analytics data (non-blocking)
    try {
      const analyticsRes = await fetch(API_BASE + '/analytics?days=7');
      analyticsData = await analyticsRes.json();
    } catch(e) { analyticsData = null; }

    renderPortfolio();
    renderInsight();
    if (currentView === 'history') {
      loadHistory();
    } else if (currentView === 'performance') {
      renderPerformance();
    } else if (currentView === 'analytics') {
      renderAnalytics();
    } else {
      renderSignals();
    }
  } catch (e) {
    clearTimeout(loadTimer);
    document.getElementById('statusDot').className = 'status-dot offline';
    document.getElementById('lastUpdate').textContent = 'Connection error';
    content.innerHTML = `<div class="loading">
      <span style="color:var(--red);font-size:16px;">Failed to load signals</span>
      <span style="color:var(--text-dim);font-size:13px;">${e.message || 'Network error'}</span>
      <button class="refresh-btn" onclick="fetchAll()" style="margin-top:12px;">Try Again</button>
    </div>`;
    console.error('Fetch failed:', e);
  }
}

function renderPortfolio() {
  const ps = signalData?.data?.portfolio_summary;
  if (!ps) return;

  const regime = ps.market_regime || 'unknown';
  const regimeClass = regime.includes('fear') ? 'fear' : regime.includes('greed') ? 'greed' : 'neutral';
  const risk = ps.risk_level || 'unknown';
  const momentum = ps.signal_momentum || 'unknown';
  const topBuy = ps.top_buys?.[0];
  const topSell = ps.top_sells?.[0];
  const improving = ps.assets_improving || 0;
  const degrading = ps.assets_degrading || 0;
  const agents = signalData?.data?.meta?.agents_available?.length || 0;

  document.getElementById('portfolioBar').innerHTML = `
    <div class="portfolio-card">
      <div class="label">Market Regime</div>
      <div class="value ${regimeClass}">${regime.replace(/_/g, ' ').toUpperCase()}</div>
      <div class="sub">Risk: ${risk}</div>
    </div>
    <div class="portfolio-card">
      <div class="label">Top Buy Signal</div>
      <div class="value" style="color:var(--green)">${topBuy ? topBuy.asset : '—'}</div>
      <div class="sub">${topBuy ? topBuy.score + ' — ' + topBuy.label : 'No data'}</div>
    </div>
    <div class="portfolio-card">
      <div class="label">Top Sell Signal</div>
      <div class="value" style="color:var(--red)">${topSell ? topSell.asset : '—'}</div>
      <div class="sub">${topSell ? topSell.score + ' — ' + topSell.label : 'No data'}</div>
    </div>
    <div class="portfolio-card">
      <div class="label">Signal Momentum</div>
      <div class="value neutral">${momentum.toUpperCase()}</div>
      <div class="sub">${improving} improving / ${degrading} degrading</div>
    </div>
    <div class="portfolio-card">
      <div class="label">Active Agents</div>
      <div class="value">${agents}<span style="color:var(--text-dim);font-size:16px">/5</span></div>
      <div class="sub">${5 - agents} agents offline</div>
    </div>
    ${renderReputationCard()}
  `;
}

function renderAgents() {
  const agents = healthData?.agents;
  if (!agents) return;

  const order = ['whale_agent', 'technical_agent', 'derivatives_agent', 'narrative_agent', 'market_agent'];
  const names = { whale_agent: 'Whale', technical_agent: 'Technical', derivatives_agent: 'Derivatives', narrative_agent: 'Narrative', market_agent: 'Market' };
  const weights = { whale_agent: '30%', technical_agent: '25%', derivatives_agent: '20%', narrative_agent: '15%', market_agent: '10%' };

  document.getElementById('agentsStrip').innerHTML = order.map(key => {
    const a = agents[key] || {};
    const status = a.status || 'no_data';
    const dotClass = status === 'ok' || status === 'partial' ? 'ok' : status === 'no_data' ? 'warn' : 'err';
    const lastRun = a.last_run ? new Date(a.last_run).toLocaleTimeString() : 'never';
    const dur = a.duration_ms ? (a.duration_ms / 1000).toFixed(1) + 's' : '';
    return `
      <div class="agent-chip">
        <span class="dot ${dotClass}"></span>
        <span class="name">${names[key]}</span>
        <span class="meta">${weights[key]}</span>
        <span class="meta">${dur || status}</span>
      </div>`;
  }).join('');
}

function renderInsight() {
  const insight = signalData?.data?.portfolio_summary?.llm_insight;
  const banner = document.getElementById('insightBanner');
  if (!insight) { banner.style.display = 'none'; return; }
  banner.style.display = 'block';
  banner.innerHTML = `
    <div class="insight-label">AI Portfolio Insight</div>
    <div>${formatMarkdown(insight)}</div>
  `;
}

function formatMarkdown(text) {
  return text
    .replace(/\\*\\*(.*?)\\*\\*/g, '<strong>$1</strong>')
    .replace(/\\n/g, '<br>');
}

function getSignalList() {
  const signals = signalData?.data?.signals;
  if (!signals) return [];
  return Object.entries(signals).map(([asset, s]) => ({ asset, ...s }));
}

function renderSignals() {
  const list = getSignalList();
  if (!list.length) {
    document.getElementById('content').innerHTML = '<div class="loading"><span style="color:var(--text-dim)">No signal data</span></div>';
    return;
  }

  // Sort
  list.sort((a, b) => {
    let va, vb;
    if (sortField === 'asset') { va = a.asset; vb = b.asset; return sortDir * va.localeCompare(vb); }
    if (sortField === 'score') { va = a.composite_score || 0; vb = b.composite_score || 0; }
    else if (sortField === 'whale') { va = a.dimensions?.whale?.score || 0; vb = b.dimensions?.whale?.score || 0; }
    else if (sortField === 'technical') { va = a.dimensions?.technical?.score || 0; vb = b.dimensions?.technical?.score || 0; }
    else if (sortField === 'derivatives') { va = a.dimensions?.derivatives?.score || 0; vb = b.dimensions?.derivatives?.score || 0; }
    else if (sortField === 'narrative') { va = a.dimensions?.narrative?.score || 0; vb = b.dimensions?.narrative?.score || 0; }
    else if (sortField === 'market') { va = a.dimensions?.market?.score || 0; vb = b.dimensions?.market?.score || 0; }
    else { va = a.composite_score || 0; vb = b.composite_score || 0; }
    return sortDir * (va - vb);
  });

  if (currentView === 'grid') renderGrid(list);
  else renderTable(list);
}

function renderGrid(list) {
  document.getElementById('content').innerHTML = `<div class="signal-grid">${
    list.map(s => {
      const dir = s.direction || 'neutral';
      const labelClass = (s.label || '').toLowerCase().replace(/ /g, '-');
      const dims = s.dimensions || {};
      return `
        <div class="signal-card" onclick="openModal('${s.asset}')">
          <div class="score-stripe ${dir}"></div>
          <div class="card-top">
            <span class="asset">${s.asset}</span>
            <span class="score ${dir}">${(s.composite_score || 0).toFixed(1)}</span>
          </div>
          <span class="card-label ${labelClass}">${s.label || 'N/A'}</span>
          <div class="dimensions">
            ${renderDimBar('whale', dims.whale)}
            ${renderDimBar('technical', dims.technical)}
            ${renderDimBar('derivatives', dims.derivatives)}
            ${renderDimBar('narrative', dims.narrative)}
            ${renderDimBar('market', dims.market)}
          </div>
        </div>`;
    }).join('')
  }</div>`;
}

function renderDimBar(name, dim) {
  const score = dim?.score || 0;
  const cls = score >= 60 ? 'high' : score >= 45 ? 'mid' : 'low';
  return `
    <div class="dim-row">
      <span class="dim-name">${name}</span>
      <div class="dim-bar-bg"><div class="dim-bar ${cls}" style="width:${score}%"></div></div>
      <span class="dim-score">${score}</span>
    </div>`;
}

function renderTable(list) {
  const sortIcon = (field) => sortField === field ? (sortDir > 0 ? ' ▲' : ' ▼') : '';
  const sortCls = (field) => sortField === field ? 'sorted' : '';

  document.getElementById('content').innerHTML = `
    <table class="signal-table">
      <thead><tr>
        <th class="${sortCls('asset')}" onclick="setSort('asset')">Asset${sortIcon('asset')}</th>
        <th class="${sortCls('score')}" onclick="setSort('score')">Score${sortIcon('score')}</th>
        <th>Label</th>
        <th class="${sortCls('whale')}" onclick="setSort('whale')">Whale${sortIcon('whale')}</th>
        <th class="${sortCls('technical')}" onclick="setSort('technical')">Technical${sortIcon('technical')}</th>
        <th class="${sortCls('derivatives')}" onclick="setSort('derivatives')">Derivatives${sortIcon('derivatives')}</th>
        <th class="${sortCls('narrative')}" onclick="setSort('narrative')">Narrative${sortIcon('narrative')}</th>
        <th class="${sortCls('market')}" onclick="setSort('market')">Market${sortIcon('market')}</th>
        <th>Momentum</th>
      </tr></thead>
      <tbody>
        ${list.map(s => {
          const dir = s.direction || 'neutral';
          const color = dir === 'buy' ? 'var(--green)' : dir === 'sell' ? 'var(--red)' : 'var(--yellow)';
          const dims = s.dimensions || {};
          return `<tr onclick="openModal('${s.asset}')">
            <td><strong>${s.asset}</strong></td>
            <td class="table-score" style="color:${color}">${(s.composite_score||0).toFixed(1)}</td>
            <td>${s.label || 'N/A'}</td>
            <td style="color:${dimColor(dims.whale?.score)}">${dims.whale?.score ?? '—'}</td>
            <td style="color:${dimColor(dims.technical?.score)}">${dims.technical?.score ?? '—'}</td>
            <td style="color:${dimColor(dims.derivatives?.score)}">${dims.derivatives?.score ?? '—'}</td>
            <td style="color:${dimColor(dims.narrative?.score)}">${dims.narrative?.score ?? '—'}</td>
            <td style="color:${dimColor(dims.market?.score)}">${dims.market?.score ?? '—'}</td>
            <td>${s.momentum || 'new'}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`;
}

function dimColor(score) {
  if (score == null) return 'var(--text-dim)';
  if (score >= 60) return 'var(--green)';
  if (score >= 45) return 'var(--yellow)';
  return 'var(--red)';
}

function setSort(field) {
  if (sortField === field) sortDir *= -1;
  else { sortField = field; sortDir = -1; }
  renderSignals();
}

function switchView(view, btn) {
  currentView = view;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  if (btn) btn.classList.add('active');
  if (view === 'history') {
    loadHistory();
  } else if (view === 'performance') {
    renderPerformance();
  } else if (view === 'analytics') {
    renderAnalytics();
  } else {
    renderSignals();
  }
}

// ===== HISTORY VIEW =====
let historyAgent = 'signal_fusion';
let historyOffset = 0;
const historyLimit = 20;
let historyTotal = 0;
let expandedRows = new Set();

async function loadHistory() {
  document.getElementById('content').innerHTML = '<div class="loading"><div class="spinner"></div><span style="color:var(--text-dim)">Loading history...</span></div>';

  try {
    const res = await fetch(`${API_BASE}/api/history?agent=${historyAgent}&limit=${historyLimit}&offset=${historyOffset}`);
    const data = await res.json();
    historyTotal = data.total_rows;
    expandedRows.clear();
    renderHistory(data);
  } catch(e) {
    document.getElementById('content').innerHTML = '<div class="loading"><span style="color:var(--red)">Failed to load history</span></div>';
  }
}

function renderHistory(data) {
  const rows = data.rows || [];
  const totalPages = Math.ceil(historyTotal / historyLimit);
  const currentPage = Math.floor(historyOffset / historyLimit) + 1;

  let html = `
    <div class="history-controls">
      <select onchange="historyAgent=this.value; historyOffset=0; loadHistory();">
        <option value="signal_fusion" ${historyAgent==='signal_fusion'?'selected':''}>Signal Fusion (All Signals)</option>
        <option value="technical_agent" ${historyAgent==='technical_agent'?'selected':''}>Technical Agent</option>
        <option value="derivatives_agent" ${historyAgent==='derivatives_agent'?'selected':''}>Derivatives Agent</option>
        <option value="market_agent" ${historyAgent==='market_agent'?'selected':''}>Market Agent</option>
        <option value="whale_agent" ${historyAgent==='whale_agent'?'selected':''}>Whale Agent</option>
        <option value="narrative_agent" ${historyAgent==='narrative_agent'?'selected':''}>Narrative Agent</option>
      </select>
      <span class="page-info">${historyTotal} total runs &middot; Page ${currentPage} of ${totalPages || 1}</span>
      <button onclick="historyOffset=Math.max(0,historyOffset-historyLimit); loadHistory();" ${historyOffset===0?'disabled':''}>&#9664; Prev</button>
      <button onclick="historyOffset=Math.min(historyTotal-1,historyOffset+historyLimit); loadHistory();" ${historyOffset+historyLimit>=historyTotal?'disabled':''}>Next &#9654;</button>
    </div>`;

  if (historyAgent === 'signal_fusion') {
    html += renderFusionHistory(rows);
  } else {
    html += renderAgentHistory(rows);
  }

  document.getElementById('content').innerHTML = html;
}

function renderFusionHistory(rows) {
  if (!rows.length) return '<p style="color:var(--text-dim);padding:20px;">No fusion history yet. Data appears after the first 15-minute cycle.</p>';

  let html = `<table class="history-table">
    <thead><tr>
      <th style="width:30px;"></th>
      <th>Run #</th>
      <th>Timestamp</th>
      <th>Status</th>
      <th>Top Buy</th>
      <th>Top Sell</th>
      <th>Regime</th>
      <th>Agents</th>
      <th>Duration</th>
    </tr></thead><tbody>`;

  rows.forEach((row, idx) => {
    const d = row.data || {};
    const ps = d.data?.portfolio_summary || {};
    const meta = d.data?.meta || {};
    const status = d.status || 'unknown';
    const statusClass = status === 'ok' ? 'ok' : status === 'partial' ? 'partial' : 'error';
    const topBuy = ps.top_buys?.[0];
    const topSell = ps.top_sells?.[0];
    const regime = (ps.market_regime || '—').replace(/_/g, ' ');
    const agents = (meta.agents_available || []).length;
    const dur = meta.duration_ms ? (meta.duration_ms/1000).toFixed(1)+'s' : '—';
    const ts = row.timestamp ? new Date(row.timestamp).toLocaleString() : '—';
    const rowId = 'hrow_' + idx;

    html += `
      <tr onclick="toggleExpand('${rowId}')">
        <td style="color:var(--text-dim)">${expandedRows.has(rowId) ? '▼' : '▶'}</td>
        <td><strong>#${row.id}</strong></td>
        <td>${ts}</td>
        <td><span class="run-status ${statusClass}">${status}</span></td>
        <td style="color:var(--green)">${topBuy ? topBuy.asset + ' ' + topBuy.score : '—'}</td>
        <td style="color:var(--red)">${topSell ? topSell.asset + ' ' + topSell.score : '—'}</td>
        <td>${regime}</td>
        <td>${agents}/5</td>
        <td>${dur}</td>
      </tr>
      <tr class="expand-row ${expandedRows.has(rowId)?'open':''}" id="${rowId}">
        <td colspan="9">${renderFusionExpand(d)}</td>
      </tr>`;
  });

  html += '</tbody></table>';
  return html;
}

function renderFusionExpand(d) {
  const signals = d.data?.signals || {};
  const entries = Object.entries(signals);
  if (!entries.length) return '<span style="color:var(--text-dim)">No signal data in this run</span>';

  let html = '<div class="expand-content">';
  entries.sort((a,b) => (b[1].composite_score||0) - (a[1].composite_score||0));

  entries.forEach(([asset, s]) => {
    const score = s.composite_score || 0;
    const dir = s.direction || 'neutral';
    const color = dir === 'buy' ? 'var(--green)' : dir === 'sell' ? 'var(--red)' : 'var(--yellow)';
    const dims = s.dimensions || {};
    html += `
      <div class="expand-asset">
        <div class="ea-name">${asset}</div>
        <div class="ea-score" style="color:${color}">${score.toFixed(1)}</div>
        <div class="ea-label">${s.label || 'N/A'}</div>
        <div class="ea-dims">
          W:${dims.whale?.score??'—'} T:${dims.technical?.score??'—'} D:${dims.derivatives?.score??'—'} N:${dims.narrative?.score??'—'} M:${dims.market?.score??'—'}
        </div>
      </div>`;
  });

  html += '</div>';
  return html;
}

function renderAgentHistory(rows) {
  if (!rows.length) return '<p style="color:var(--text-dim);padding:20px;">No data yet for this agent.</p>';

  let html = `<table class="history-table">
    <thead><tr>
      <th style="width:30px;"></th>
      <th>Run #</th>
      <th>Timestamp</th>
      <th>Status</th>
      <th>Duration</th>
      <th>Errors</th>
      <th>Assets Covered</th>
    </tr></thead><tbody>`;

  rows.forEach((row, idx) => {
    const d = row.data || {};
    const meta = d.meta || {};
    const status = d.status || 'unknown';
    const statusClass = status === 'ok' ? 'ok' : status === 'partial' ? 'partial' : 'error';
    const dur = meta.duration_ms ? (meta.duration_ms/1000).toFixed(1)+'s' : '—';
    const errors = (meta.errors || []).length;
    const assets = Object.keys(d.data?.per_asset || d.data || {}).length;
    const ts = row.timestamp ? new Date(row.timestamp).toLocaleString() : '—';
    const rowId = 'arow_' + idx;

    html += `
      <tr onclick="toggleExpand('${rowId}')">
        <td style="color:var(--text-dim)">${expandedRows.has(rowId) ? '▼' : '▶'}</td>
        <td><strong>#${row.id}</strong></td>
        <td>${ts}</td>
        <td><span class="run-status ${statusClass}">${status}</span></td>
        <td>${dur}</td>
        <td>${errors > 0 ? '<span style="color:var(--red)">'+errors+'</span>' : '0'}</td>
        <td>${assets}</td>
      </tr>
      <tr class="expand-row ${expandedRows.has(rowId)?'open':''}" id="${rowId}">
        <td colspan="7">
          <pre style="font-size:12px;color:var(--text-dim);white-space:pre-wrap;max-height:400px;overflow-y:auto;">${JSON.stringify(d.data || d, null, 2).substring(0, 5000)}</pre>
        </td>
      </tr>`;
  });

  html += '</tbody></table>';
  return html;
}

function toggleExpand(rowId) {
  const el = document.getElementById(rowId);
  if (!el) return;
  if (expandedRows.has(rowId)) {
    expandedRows.delete(rowId);
    el.classList.remove('open');
  } else {
    expandedRows.add(rowId);
    el.classList.add('open');
  }
  // Update the arrow in the previous row
  const prevTd = el.previousElementSibling?.querySelector('td');
  if (prevTd) prevTd.textContent = expandedRows.has(rowId) ? '▼' : '▶';
}

function openModal(asset) {
  const signals = signalData?.data?.signals;
  const s = signals?.[asset];
  if (!s) return;

  const dir = s.direction || 'neutral';
  const color = dir === 'buy' ? 'var(--green)' : dir === 'sell' ? 'var(--red)' : 'var(--yellow)';
  const dims = s.dimensions || {};
  const dimOrder = ['whale', 'technical', 'derivatives', 'narrative', 'market'];

  const insightHTML = s.llm_insight
    ? `<div class="modal-insight">${formatMarkdown(s.llm_insight)}</div>`
    : '';

  document.getElementById('modal').innerHTML = `
    <div class="modal-header">
      <h2>${asset}</h2>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-score" style="color:${color}">${(s.composite_score||0).toFixed(1)}</div>
    <span class="card-label ${(s.label||'').toLowerCase().replace(/ /g, '-')}">${s.label || 'N/A'}</span>
    <div style="color:var(--text-dim);font-size:13px;margin-top:4px;">
      ${s.momentum ? 'Momentum: ' + s.momentum : ''}
      ${s.prev_score != null ? ' | Prev: ' + s.prev_score : ''}
    </div>
    <div class="modal-dim-detail">
      ${dimOrder.map(d => {
        const dim = dims[d] || {};
        const sc = dim.score ?? 0;
        const bgColor = sc >= 60 ? 'var(--green-bg)' : sc >= 45 ? 'var(--yellow-bg)' : 'var(--red-bg)';
        const fgColor = sc >= 60 ? 'var(--green)' : sc >= 45 ? 'var(--yellow)' : 'var(--red)';
        return `
          <div class="modal-dim-item">
            <div class="left">
              <span class="dim-title">${d}</span>
              <span class="dim-detail">${dim.detail || 'no data'}</span>
            </div>
            <span class="dim-badge" style="background:${bgColor};color:${fgColor}">
              ${sc} — ${dim.label || 'N/A'}
            </span>
          </div>`;
      }).join('')}
    </div>
    ${insightHTML}
  `;

  document.getElementById('modalOverlay').classList.add('active');
}

function closeModal(e) {
  if (e && e.target !== document.getElementById('modalOverlay')) return;
  document.getElementById('modalOverlay').classList.remove('active');
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeModal();
});

// ===== PERFORMANCE VIEW =====
function renderReputationCard() {
  if (!perfData) return '';
  if (perfData.status === 'collecting_data') {
    const snaps = perfData.snapshots_collected || 0;
    return `
      <div class="portfolio-card" style="cursor:pointer" onclick="switchView('performance', document.querySelector('[data-view=performance]'))">
        <div class="label">Reputation</div>
        <div class="value" style="color:var(--cyan)">${snaps}</div>
        <div class="sub">snapshots collected</div>
      </div>`;
  }
  const rep = perfData.reputation_score || 0;
  const repColor = rep >= 65 ? 'var(--green)' : rep >= 50 ? 'var(--yellow)' : 'var(--red)';
  return `
    <div class="portfolio-card" style="cursor:pointer" onclick="switchView('performance', document.querySelector('[data-view=performance]'))">
      <div class="label">Reputation Score</div>
      <div class="value" style="color:${repColor}">${rep}</div>
      <div class="sub">${perfData.accuracy_30d || 0}% accuracy (30d)</div>
    </div>`;
}

function repColor(val) {
  if (val >= 65) return 'var(--green)';
  if (val >= 50) return 'var(--yellow)';
  return 'var(--red)';
}

function renderPerformance() {
  const content = document.getElementById('content');

  if (!perfData) {
    content.innerHTML = '<div class="loading"><div class="spinner"></div><span style="color:var(--text-dim)">Loading performance data...</span></div>';
    return;
  }

  // Collecting data state (no accuracy yet)
  if (perfData.status === 'collecting_data') {
    const snaps = perfData.snapshots_collected || 0;
    // Show current signals with directions + reasoning
    let signalRows = '';
    if (signalData) {
      const signals = signalData.data?.signals || {};
      const sorted = Object.entries(signals).sort((a,b) => (b[1].composite_score||0) - (a[1].composite_score||0));
      signalRows = sorted.map(([asset, s]) => {
        const score = s.composite_score || 0;
        let dir = 'neutral';
        if (score > 60) dir = 'bullish';
        else if (score < 40) dir = 'bearish';
        const dims = s.dimensions || {};
        // Build reasoning from dimension details
        const reasons = [];
        for (const [dName, dData] of Object.entries(dims)) {
          const detail = dData?.detail;
          if (detail && detail !== 'no data' && detail !== 'no scorer') {
            reasons.push('<strong>' + dName.charAt(0).toUpperCase() + dName.slice(1) + ':</strong> ' + detail);
          }
        }
        return `<tr>
          <td><strong>${asset}</strong></td>
          <td style="font-weight:700;color:${score > 60 ? 'var(--green)' : score < 40 ? 'var(--red)' : 'var(--yellow)'}">${score.toFixed(1)}</td>
          <td><span class="dir-badge ${dir}">${dir.toUpperCase()}</span></td>
          <td style="font-size:12px;line-height:1.6;color:var(--text-dim)">${reasons.join('<br>') || 'No details'}</td>
        </tr>`;
      }).join('');
    }

    content.innerHTML = `
      <div class="perf-collecting">
        <div class="pc-icon">&#128202;</div>
        <div class="pc-title">Performance Tracking Active</div>
        <div class="pc-sub">Collecting signal snapshots every 15 minutes.<br>Accuracy scores will appear after 24 hours when we can compare predictions vs actual price moves.</div>
        <div class="pc-count">${snaps}</div>
        <div class="pc-count-label">snapshots collected</div>
      </div>
      ${signalRows ? `
        <div class="perf-section-title" style="margin-top:28px">Current Signal Directions & Reasoning</div>
        <table class="perf-signals-table">
          <thead><tr>
            <th>Asset</th>
            <th>Score</th>
            <th>Direction</th>
            <th>Reasoning (Why This Score)</th>
          </tr></thead>
          <tbody>${signalRows}</tbody>
        </table>
      ` : ''}
      <div class="perf-methodology" style="margin-top:20px">
        <strong>Methodology:</strong> Direction is extracted from composite score (&gt;60 = bullish, &lt;40 = bearish, 40-60 = neutral).
        After 24h/48h/7d, we check if price moved in the predicted direction. Neutral signals are correct if price moved &le;2%.
        Price source: CoinGecko. Scoring: binary hit/miss. Window: 30-day rolling.
      </div>
    `;
    return;
  }

  // Active state — show full reputation data
  const rep = perfData.reputation_score || 0;
  const acc = perfData.accuracy_30d || 0;
  const evaluated = perfData.signals_evaluated || 0;
  const correct = perfData.signals_correct || 0;
  const wrong = perfData.signals_wrong || 0;
  const byTf = perfData.by_timeframe || {};
  const byAsset = perfData.by_asset || {};
  const snaps = perfData.snapshots_collected_30d || 0;

  // Timeframe cards
  const tfOrder = ['24h', '48h', '7d'];
  const tfCards = tfOrder.map(tf => {
    const d = byTf[tf];
    if (!d) return `
      <div class="perf-tf-card">
        <div class="tf-label">${tf} Accuracy</div>
        <div class="tf-value" style="color:var(--text-dim)">—</div>
        <div class="tf-detail">No data yet</div>
      </div>`;
    return `
      <div class="perf-tf-card">
        <div class="tf-label">${tf} Accuracy</div>
        <div class="tf-value" style="color:${repColor(d.accuracy)}">${d.accuracy}%</div>
        <div class="tf-detail">${d.hits}/${d.total} correct</div>
      </div>`;
  }).join('');

  // Per-asset cards
  const assetEntries = Object.entries(byAsset).sort((a,b) => b[1] - a[1]);
  const assetCards = assetEntries.map(([asset, acc]) => `
    <div class="perf-asset-card">
      <span class="pa-name">${asset}</span>
      <span class="pa-acc" style="color:${repColor(acc)}">${acc}%</span>
    </div>
  `).join('');

  // Current signals table with reasoning
  let signalRows = '';
  if (signalData) {
    const signals = signalData.data?.signals || {};
    const sorted = Object.entries(signals).sort((a,b) => (b[1].composite_score||0) - (a[1].composite_score||0));
    signalRows = sorted.map(([asset, s]) => {
      const score = s.composite_score || 0;
      let dir = 'neutral';
      if (score > 60) dir = 'bullish';
      else if (score < 40) dir = 'bearish';
      const dims = s.dimensions || {};
      const assetAcc = byAsset[asset];
      const accBadge = assetAcc != null
        ? `<span style="font-size:11px;color:${repColor(assetAcc)};font-weight:600">${assetAcc}%</span>`
        : '<span style="font-size:11px;color:var(--text-dim)">—</span>';
      const reasons = [];
      for (const [dName, dData] of Object.entries(dims)) {
        const detail = dData?.detail;
        if (detail && detail !== 'no data' && detail !== 'no scorer') {
          reasons.push('<strong>' + dName.charAt(0).toUpperCase() + dName.slice(1) + ':</strong> ' + detail);
        }
      }
      return `<tr>
        <td><strong>${asset}</strong></td>
        <td style="font-weight:700;color:${score > 60 ? 'var(--green)' : score < 40 ? 'var(--red)' : 'var(--yellow)'}">${score.toFixed(1)}</td>
        <td><span class="dir-badge ${dir}">${dir.toUpperCase()}</span></td>
        <td>${accBadge}</td>
        <td style="font-size:12px;line-height:1.6;color:var(--text-dim)">${reasons.join('<br>') || 'No details'}</td>
      </tr>`;
    }).join('');
  }

  content.innerHTML = `
    <div class="perf-header">
      <div class="perf-score-card">
        <div class="perf-big" style="color:${repColor(rep)}">${rep}</div>
        <div class="perf-label">Reputation Score</div>
        <div class="perf-sub">
          ${acc}% accuracy over 30 days<br>
          ${correct} correct / ${wrong} wrong<br>
          ${evaluated} signals evaluated<br>
          ${snaps} snapshots collected
        </div>
      </div>
      <div class="perf-timeframes">
        ${tfCards}
      </div>
    </div>

    ${assetCards ? `
      <div class="perf-section-title">Accuracy by Asset</div>
      <div class="perf-asset-grid">${assetCards}</div>
    ` : ''}

    <div class="perf-section-title">Current Signals & Reasoning</div>
    <table class="perf-signals-table">
      <thead><tr>
        <th>Asset</th>
        <th>Score</th>
        <th>Direction</th>
        <th>30d Acc</th>
        <th>Reasoning (Why This Score)</th>
      </tr></thead>
      <tbody>${signalRows}</tbody>
    </table>

    <div class="perf-methodology" style="margin-top:20px">
      <strong>Methodology:</strong> Direction is extracted from composite score (&gt;60 = bullish, &lt;40 = bearish, 40-60 = neutral).
      After 24h/48h/7d, we compare predicted direction vs actual price movement. Neutral signals are correct if price moved &le;2%.
      <strong>Price source:</strong> CoinGecko. <strong>Scoring:</strong> Binary hit/miss. <strong>Window:</strong> 30-day rolling.
    </div>
  `;
}

// ===== ANALYTICS VIEW =====
function renderAnalytics() {
  const content = document.getElementById('content');

  if (!analyticsData || analyticsData.total_requests === 0) {
    content.innerHTML = `
      <div class="perf-collecting">
        <div class="pc-icon">&#128200;</div>
        <div class="pc-title">Usage Tracking Active</div>
        <div class="pc-sub">
          Request logging is now running. Every API call is tracked with user-agent classification.<br>
          Analytics data will appear here as requests come in.
        </div>
        <div class="pc-count">${analyticsData ? analyticsData.total_requests : 0}</div>
        <div class="pc-count-label">requests tracked</div>
      </div>
      <div class="perf-methodology" style="margin-top:20px">
        <strong>What's tracked:</strong> Endpoint, HTTP method, user-agent (classified as AI agent / browser / bot / SDK),
        response time, client IP (for unique client count). <strong>No PII is stored.</strong>
        <br><strong>Discovery endpoints:</strong>
        <code>/.well-known/agent.json</code> (A2A) &middot;
        <code>/.well-known/agents.md</code> (AAIF) &middot;
        <code>/mcp/sse</code> (MCP SSE) &middot;
        <code>/docs</code> (OpenAPI)
      </div>
    `;
    return;
  }

  const d = analyticsData;
  const totalReqs = d.total_requests || 0;
  const uniqueClients = d.unique_clients || 0;
  const avgMs = d.avg_response_ms || 0;
  const byType = d.by_client_type || {};
  const byEndpoint = d.by_endpoint || {};
  const perDay = d.requests_per_day || {};
  const topUAs = d.top_user_agents || [];

  // Count AI agent requests
  const aiTypes = ['claude', 'openai', 'gemini', 'langchain', 'crewai', 'mcp_client', 'autogpt'];
  let aiReqs = 0;
  aiTypes.forEach(t => { aiReqs += (byType[t] || 0); });

  // Summary cards
  const summaryCards = `
    <div class="analytics-grid">
      <div class="analytics-card">
        <div class="ac-label">Total Requests (7d)</div>
        <div class="ac-value" style="color:var(--cyan)">${totalReqs.toLocaleString()}</div>
      </div>
      <div class="analytics-card">
        <div class="ac-label">AI Agent Requests</div>
        <div class="ac-value" style="color:var(--green)">${aiReqs.toLocaleString()}</div>
        <div class="ac-sub">${totalReqs > 0 ? ((aiReqs/totalReqs)*100).toFixed(1) : 0}% of total</div>
      </div>
      <div class="analytics-card">
        <div class="ac-label">Unique Clients</div>
        <div class="ac-value">${uniqueClients}</div>
      </div>
      <div class="analytics-card">
        <div class="ac-label">Avg Response</div>
        <div class="ac-value">${avgMs < 1000 ? Math.round(avgMs) + '<span style="font-size:16px;color:var(--text-dim)">ms</span>' : (avgMs/1000).toFixed(1) + '<span style="font-size:16px;color:var(--text-dim)">s</span>'}</div>
      </div>
    </div>
  `;

  // Client type breakdown
  const typeEntries = Object.entries(byType).sort((a,b) => b[1] - a[1]);
  const typeCards = typeEntries.map(([type, count]) => {
    const isAI = aiTypes.includes(type);
    return `
      <div class="ua-chip ${isAI ? 'ai-agent' : ''}">
        <span class="ua-name">${type.replace(/_/g, ' ')}</span>
        <span class="ua-count">${count}</span>
      </div>`;
  }).join('');

  // Daily chart
  const dayEntries = Object.entries(perDay).sort((a,b) => a[0].localeCompare(b[0]));
  const maxDay = Math.max(...dayEntries.map(e => e[1]), 1);
  const dailyBars = dayEntries.map(([day, count]) => {
    const pct = (count / maxDay) * 100;
    const shortDay = day.slice(5); // MM-DD
    return `
      <div class="daily-bar-wrap">
        <div class="daily-bar-count">${count}</div>
        <div class="daily-bar" style="height:${Math.max(pct, 2)}%"></div>
        <div class="daily-bar-label">${shortDay}</div>
      </div>`;
  }).join('');

  // Endpoint breakdown table
  const epEntries = Object.entries(byEndpoint).sort((a,b) => b[1] - a[1]);
  const maxEp = Math.max(...epEntries.map(e => e[1]), 1);
  const epRows = epEntries.map(([ep, count]) => {
    const pct = (count / maxEp) * 100;
    return `<tr>
      <td><code>${ep}</code></td>
      <td style="font-weight:700">${count}</td>
      <td style="width:40%">
        <div class="ep-bar-bg"><div class="ep-bar" style="width:${pct}%"></div></div>
      </td>
    </tr>`;
  }).join('');

  // Top user agents table
  const uaRows = topUAs.slice(0, 15).map(ua => {
    const isAI = aiTypes.includes(ua.type);
    return `<tr>
      <td style="font-size:12px;word-break:break-all;max-width:400px">${ua.user_agent || 'unknown'}</td>
      <td><span class="dir-badge ${isAI ? 'bullish' : 'neutral'}">${ua.type.replace(/_/g, ' ').toUpperCase()}</span></td>
      <td style="font-weight:700">${ua.requests}</td>
    </tr>`;
  }).join('');

  content.innerHTML = `
    ${summaryCards}

    <div class="perf-section-title">Client Type Breakdown</div>
    <div class="ua-breakdown">${typeCards}</div>

    <div class="perf-section-title">Requests Per Day</div>
    <div class="daily-chart">
      <div class="daily-bars">${dailyBars || '<span style="color:var(--text-dim);padding:20px">No daily data yet</span>'}</div>
    </div>

    <div class="perf-section-title">Endpoint Popularity</div>
    <table class="endpoint-table">
      <thead><tr>
        <th>Endpoint</th>
        <th>Requests</th>
        <th>Volume</th>
      </tr></thead>
      <tbody>${epRows}</tbody>
    </table>

    ${uaRows ? `
      <div class="perf-section-title">Top User Agents</div>
      <table class="endpoint-table">
        <thead><tr>
          <th>User Agent</th>
          <th>Type</th>
          <th>Requests</th>
        </tr></thead>
        <tbody>${uaRows}</tbody>
      </table>
    ` : ''}

    <div class="perf-methodology">
      <strong>Discovery Protocols Active:</strong><br>
      &#x2705; <strong>A2A</strong> — <code>/.well-known/agent.json</code> (Google Agent-to-Agent Protocol)<br>
      &#x2705; <strong>AGENTS.md</strong> — <code>/.well-known/agents.md</code> (Agentic AI Foundation standard)<br>
      &#x2705; <strong>MCP SSE</strong> — <code>/mcp/sse</code> (Model Context Protocol, remote access)<br>
      &#x2705; <strong>OpenAPI</strong> — <code>/docs</code> (auto-generated, works with LangChain/CrewAI/OpenAI)<br>
      <br>
      <strong>Client Classification:</strong> User-agents are classified as AI agent (Claude, OpenAI, Gemini, LangChain, CrewAI, MCP),
      SDK (Python, Node.js, curl), browser, or bot. AI agent requests are highlighted in green.
    </div>
  `;
}

// Initial load
fetchAll();

// Auto-refresh every 60s
setInterval(fetchAll, 60000);
</script>
</body>
</html>"""
