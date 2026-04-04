# api/dashboard.py
"""Dashboard — inline HTML/JS single-page app."""

DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Web3 Signals Dashboard</title>
    <style>
        body { font-family: monospace; background: #0a0b0f; color: #e0e0e0; padding: 2rem; }
        h1 { color: #00ff88; }
        .card { background: #12131a; border-radius: 8px; padding: 1rem; margin: 0.5rem 0; }
        .score { font-size: 2rem; font-weight: bold; }
        .bullish { color: #00ff88; }
        .bearish { color: #ff4444; }
        .neutral { color: #888; }
        #signals { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; }
        .loading { color: #888; }
    </style>
</head>
<body>
    <h1>Web3 Signals</h1>
    <p id="status" class="loading">Loading signals...</p>
    <div id="signals"></div>
    <script>
        async function loadSignals() {
            try {
                const resp = await fetch('/api/signal');
                const data = await resp.json();
                const container = document.getElementById('signals');
                const status = document.getElementById('status');
                status.textContent = 'Regime: ' + data.regime + ' | Updated: ' + new Date(data.timestamp).toLocaleString();
                status.className = '';
                container.innerHTML = '';
                for (const [asset, sig] of Object.entries(data.signals || {})) {
                    const cls = sig.direction === 'bullish' ? 'bullish' : sig.direction === 'bearish' ? 'bearish' : 'neutral';
                    container.innerHTML += '<div class="card"><h3>' + asset + '</h3><div class="score ' + cls + '">' + sig.composite + '</div><div>' + sig.label + '</div><div>Direction: ' + sig.direction + '</div></div>';
                }
            } catch(e) {
                document.getElementById('status').textContent = 'Error loading signals: ' + e.message;
            }
        }
        loadSignals();
        setInterval(loadSignals, 300000);
    </script>
</body>
</html>"""
