/* =========================================================
   BTC Scalp Dashboard — Frontend Application
   ========================================================= */

(function () {
  "use strict";

  // ── State ──
  let ws = null;
  let reconnectTimer = null;
  let audioEnabled = false;
  let currentTf = "5m";
  let priceChart = null;
  let allSignals = [];
  let lastData = null;

  // ── Audio Context for alerts ──
  let audioCtx = null;
  function playAlert() {
    if (!audioEnabled) return;
    try {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.type = "sine";
      osc.frequency.setValueAtTime(880, audioCtx.currentTime);
      osc.frequency.setValueAtTime(1100, audioCtx.currentTime + 0.1);
      osc.frequency.setValueAtTime(880, audioCtx.currentTime + 0.2);
      gain.gain.setValueAtTime(0.3, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + 0.5);
      osc.start(audioCtx.currentTime);
      osc.stop(audioCtx.currentTime + 0.5);
    } catch (e) {
      // silent fail
    }
  }

  // ── Formatters ──
  function fmt(n, d) {
    if (n == null || isNaN(n)) return "--";
    return Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function fmtUsd(n) {
    if (n == null || isNaN(n)) return "$--";
    return "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtCompact(n) {
    if (n == null || isNaN(n)) return "--";
    if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return n.toFixed(2);
  }
  function fmtTime(iso) {
    if (!iso) return "--";
    const d = new Date(iso);
    return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }

  function scoreColor(score) {
    if (score >= 75) return "var(--accent-green)";
    if (score >= 60) return "var(--accent-yellow)";
    if (score >= 45) return "var(--text-muted)";
    return "var(--accent-red)";
  }

  // ── Chart Setup ──
  function initChart() {
    const ctx = document.getElementById("priceChart").getContext("2d");
    priceChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {
            label: "Price",
            data: [],
            borderColor: "#06b6d4",
            borderWidth: 1.5,
            pointRadius: 0,
            pointHoverRadius: 3,
            fill: false,
            tension: 0.1,
            order: 1,
          },
          {
            label: "EMA 9",
            data: [],
            borderColor: "#22c55e",
            borderWidth: 1,
            pointRadius: 0,
            borderDash: [],
            fill: false,
            order: 2,
          },
          {
            label: "EMA 21",
            data: [],
            borderColor: "#f59e0b",
            borderWidth: 1,
            pointRadius: 0,
            fill: false,
            order: 3,
          },
          {
            label: "EMA 55",
            data: [],
            borderColor: "#a78bfa",
            borderWidth: 1,
            pointRadius: 0,
            fill: false,
            order: 4,
          },
          {
            label: "EMA 200",
            data: [],
            borderColor: "#ef4444",
            borderWidth: 1,
            pointRadius: 0,
            borderDash: [4, 4],
            fill: false,
            order: 5,
          },
          {
            label: "BB Upper",
            data: [],
            borderColor: "rgba(100,116,139,0.4)",
            borderWidth: 1,
            pointRadius: 0,
            borderDash: [2, 2],
            fill: false,
            order: 6,
          },
          {
            label: "BB Lower",
            data: [],
            borderColor: "rgba(100,116,139,0.4)",
            borderWidth: 1,
            pointRadius: 0,
            borderDash: [2, 2],
            fill: "-1",
            backgroundColor: "rgba(100,116,139,0.04)",
            order: 7,
          },
          {
            label: "VWAP",
            data: [],
            borderColor: "rgba(249,115,22,0.6)",
            borderWidth: 1,
            pointRadius: 0,
            borderDash: [6, 3],
            fill: false,
            order: 8,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 0 },
        interaction: { mode: "index", intersect: false },
        scales: {
          x: {
            type: "time",
            time: { unit: "minute", displayFormats: { minute: "HH:mm" } },
            grid: { color: "rgba(30,41,59,0.5)", drawBorder: false },
            ticks: { color: "#64748b", font: { size: 10, family: "'JetBrains Mono'" }, maxTicksLimit: 15 },
          },
          y: {
            position: "right",
            grid: { color: "rgba(30,41,59,0.5)", drawBorder: false },
            ticks: {
              color: "#64748b",
              font: { size: 10, family: "'JetBrains Mono'" },
              callback: function (v) { return "$" + v.toLocaleString(); },
            },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#1a2035",
            titleColor: "#e2e8f0",
            bodyColor: "#94a3b8",
            borderColor: "#2a3a52",
            borderWidth: 1,
            titleFont: { family: "'JetBrains Mono'", size: 11 },
            bodyFont: { family: "'JetBrains Mono'", size: 11 },
            callbacks: {
              label: function (ctx) {
                return ctx.dataset.label + ": $" + Number(ctx.parsed.y).toLocaleString(undefined, { minimumFractionDigits: 2 });
              },
            },
          },
          annotation: { annotations: {} },
        },
      },
    });
  }

  function updateChart(data) {
    if (!priceChart || !data) return;
    const candles = currentTf === "1m" ? data.candles_1m : data.candles_5m;
    if (!candles || candles.length === 0) return;

    const labels = candles.map(function (c) { return new Date(c.t); });
    const closes = candles.map(function (c) { return c.c; });

    priceChart.data.labels = labels;
    priceChart.data.datasets[0].data = closes;

    // EMAs — align to end of closes array
    var ta = data.ta || {};
    var emaValues = ta.emas || {};
    var emaPeriods = [9, 21, 55, 200];
    for (var ei = 0; ei < emaPeriods.length; ei++) {
      var period = emaPeriods[ei];
      var emaVal = emaValues[period];
      if (emaVal != null && closes.length > 0) {
        // Show as flat line at current value (simplified — full array would need recalc)
        var arr = new Array(closes.length).fill(null);
        // Fill from period onwards with interpolated value
        var startIdx = Math.max(0, closes.length - 60);
        for (var j = startIdx; j < closes.length; j++) {
          arr[j] = emaVal;
        }
        priceChart.data.datasets[ei + 1].data = arr;
      }
    }

    // Recalculate EMAs client-side for proper visualization
    for (var ei2 = 0; ei2 < emaPeriods.length; ei2++) {
      var p2 = emaPeriods[ei2];
      if (closes.length >= p2) {
        var emaArr = calcEmaArray(closes, p2);
        priceChart.data.datasets[ei2 + 1].data = emaArr;
      }
    }

    // BB
    if (ta.bb) {
      var bbU = new Array(closes.length).fill(null);
      var bbL = new Array(closes.length).fill(null);
      for (var bi = Math.max(0, closes.length - 50); bi < closes.length; bi++) {
        bbU[bi] = ta.bb.upper;
        bbL[bi] = ta.bb.lower;
      }
      priceChart.data.datasets[5].data = bbU;
      priceChart.data.datasets[6].data = bbL;
    }

    // VWAP
    if (ta.vwap) {
      var vwapArr = new Array(closes.length).fill(null);
      for (var vi = Math.max(0, closes.length - 50); vi < closes.length; vi++) {
        vwapArr[vi] = ta.vwap;
      }
      priceChart.data.datasets[7].data = vwapArr;
    }

    // Fibonacci annotations
    var annotations = {};
    if (ta.fib_levels) {
      var fibColors = { "0.0": "#64748b", "0.236": "#06b6d4", "0.382": "#22c55e", "0.5": "#f59e0b", "0.618": "#ef4444", "0.786": "#a78bfa", "1.0": "#64748b" };
      var fibKeys = Object.keys(ta.fib_levels);
      for (var fi = 0; fi < fibKeys.length; fi++) {
        var fk = fibKeys[fi];
        annotations["fib_" + fk] = {
          type: "line",
          yMin: ta.fib_levels[fk],
          yMax: ta.fib_levels[fk],
          borderColor: (fibColors[fk] || "#64748b") + "66",
          borderWidth: 1,
          borderDash: [3, 3],
          label: {
            display: true,
            content: "Fib " + fk,
            position: "start",
            color: "#64748b",
            font: { size: 9, family: "'JetBrains Mono'" },
            backgroundColor: "transparent",
          },
        };
      }
    }
    priceChart.options.plugins.annotation.annotations = annotations;
    priceChart.update("none");
  }

  function calcEmaArray(data, period) {
    if (data.length < period) return new Array(data.length).fill(null);
    var result = new Array(period - 1).fill(null);
    var sum = 0;
    for (var i = 0; i < period; i++) sum += data[i];
    var ema = sum / period;
    result.push(ema);
    var k = 2 / (period + 1);
    for (var j = period; j < data.length; j++) {
      ema = data[j] * k + ema * (1 - k);
      result.push(ema);
    }
    return result;
  }

  // ── UI Update Functions ──
  function updateHeader(data) {
    var market = data.market || {};
    var el = document.getElementById("price-value");
    el.textContent = fmtUsd(market.price);

    var chgEl = document.getElementById("price-change");
    var pct = market.change_24h_pct || 0;
    chgEl.textContent = (pct >= 0 ? "+" : "") + fmt(pct, 2) + "%";
    chgEl.className = "price-change " + (pct >= 0 ? "positive" : "negative");

    // Confluence
    var conf = data.confluence || {};
    var scoreEl = document.getElementById("confluence-score");
    scoreEl.textContent = fmt(conf.score, 0);
    scoreEl.style.color = scoreColor(conf.score || 0);

    var dirEl = document.getElementById("confluence-dir");
    var dir = conf.direction || "neutral";
    dirEl.textContent = dir.toUpperCase();
    dirEl.className = "gauge-direction " + dir;
  }

  function updateBanner(data) {
    var ta = data.ta || {};
    var cond = ta.market_condition || "Ranging";
    var banner = document.getElementById("market-banner");
    var condEl = document.getElementById("market-condition");
    var extraEl = document.getElementById("banner-extra");
    var iconEl = document.getElementById("banner-icon");

    condEl.textContent = cond;
    banner.className = "market-banner " + cond.toLowerCase();

    if (cond === "Choppy") {
      iconEl.textContent = "⚠";
      extraEl.textContent = " — All signals suppressed. ADX: " + fmt(ta.adx, 1) + ", BB Width: " + fmt(ta.bb ? ta.bb.width : 0, 2);
    } else if (cond === "Trending") {
      iconEl.textContent = "▲";
      extraEl.textContent = " — ADX: " + fmt(ta.adx, 1);
    } else {
      iconEl.textContent = "◆";
      extraEl.textContent = " — ADX: " + fmt(ta.adx, 1);
    }
  }

  function updateToolbar(data) {
    var ta = data.ta || {};
    document.getElementById("toolbar-rsi").textContent = fmt(ta.rsi, 1);
    document.getElementById("toolbar-macd").textContent = ta.macd ? fmt(ta.macd.histogram, 2) : "--";
    document.getElementById("toolbar-adx").textContent = fmt(ta.adx, 1);
    // Color RSI
    var rsiEl = document.getElementById("toolbar-rsi");
    var rsi = ta.rsi || 50;
    if (rsi > 70) rsiEl.style.color = "var(--accent-red)";
    else if (rsi < 30) rsiEl.style.color = "var(--accent-green)";
    else rsiEl.style.color = "var(--text-primary)";
  }

  function updateSignalFeed(data) {
    var signal = data.signal;
    if (signal && signal.id) {
      // Check if already in list
      var exists = allSignals.some(function (s) { return s.id === signal.id; });
      if (!exists) {
        allSignals.unshift(signal);
        if (allSignals.length > 50) allSignals.pop();
        if (signal.confidence === "High") {
          playAlert();
        }
      }
    }

    var feed = document.getElementById("signal-feed");
    if (allSignals.length === 0) {
      feed.innerHTML = '<div class="no-signals">No signals yet. Waiting for confluence...</div>';
      document.getElementById("signal-count").textContent = "";
      return;
    }

    document.getElementById("signal-count").textContent = allSignals.length + " total";

    var html = "";
    var displaySignals = allSignals.slice(0, 15);
    for (var i = 0; i < displaySignals.length; i++) {
      var s = displaySignals[i];
      html += renderSignalCard(s);
    }
    feed.innerHTML = html;
  }

  function renderSignalCard(s) {
    var tierClass = s.confidence === "High" ? "high" : "medium";
    var h = '<div class="signal-card ' + tierClass + '">';
    h += '<div class="signal-header">';
    h += '<span class="signal-dir ' + s.direction + '">' + s.direction + '</span>';
    h += '<span class="signal-confidence">' + s.confidence + ' Confidence</span>';
    h += '</div>';
    h += '<div class="signal-score-mini" style="color:' + scoreColor(s.score) + '">' + fmt(s.score, 0) + '</div>';

    // Entry levels
    h += '<div class="signal-levels">';
    h += '<div class="signal-level"><div class="signal-level-label">Entry</div><div class="signal-level-value entry">' + fmtUsd(s.entry) + '</div></div>';
    h += '<div class="signal-level"><div class="signal-level-label">Stop</div><div class="signal-level-value sl">' + fmtUsd(s.stop_loss) + '</div></div>';
    h += '<div class="signal-level"><div class="signal-level-label">TP1 (' + fmt(s.risk_reward, 1) + 'R)</div><div class="signal-level-value tp">' + fmtUsd(s.tp1) + '</div></div>';
    h += '</div>';

    // Breakdown bars
    if (s.breakdown) {
      h += '<div class="signal-breakdown">';
      var cats = ["technical", "orderflow", "onchain", "sentiment", "macro"];
      var catLabels = { technical: "Technical", orderflow: "Flow", onchain: "On-Chain", sentiment: "Sentiment", macro: "Macro" };
      for (var ci = 0; ci < cats.length; ci++) {
        var cat = cats[ci];
        var bd = s.breakdown[cat] || {};
        var sc = bd.score || 50;
        h += '<div class="breakdown-row">';
        h += '<span class="breakdown-label">' + catLabels[cat] + '</span>';
        h += '<div class="breakdown-bar"><div class="breakdown-fill" style="width:' + sc + '%;background:' + scoreColor(sc) + '"></div></div>';
        h += '<span class="breakdown-val">' + fmt(sc, 0) + '</span>';
        h += '</div>';
      }
      h += '</div>';
    }

    // Reasons
    if (s.reasons && s.reasons.length > 0) {
      h += '<div class="signal-reasons">';
      var maxReasons = Math.min(s.reasons.length, 5);
      for (var ri = 0; ri < maxReasons; ri++) {
        h += '<div class="reason-item">' + escHtml(s.reasons[ri]) + '</div>';
      }
      if (s.reasons.length > 5) {
        h += '<div class="reason-item" style="color:var(--text-faint)">+' + (s.reasons.length - 5) + ' more</div>';
      }
      h += '</div>';
    }

    h += '<div class="signal-time">' + fmtTime(s.timestamp) + ' UTC | ' + s.market_condition + '</div>';
    h += '</div>';
    return h;
  }

  function updateFearGreed(data) {
    var fg = (data.sentiment || {}).fear_greed || {};
    var val = fg.value || 50;
    var cls = fg.classification || "Neutral";
    var prev = fg.prev_value || val;

    document.getElementById("fg-value").textContent = val;
    document.getElementById("fg-label").textContent = cls;

    var trend = val > prev ? "↑ Rising" : val < prev ? "↓ Falling" : "→ Stable";
    document.getElementById("fg-trend").textContent = "24h trend: " + trend;

    // SVG arc
    var arc = document.getElementById("fg-arc");
    var circumference = 2 * Math.PI * 15.91;
    var offset = circumference - (val / 100) * circumference;
    arc.style.strokeDasharray = circumference + " " + circumference;
    arc.style.strokeDashoffset = offset;

    // Color
    var color;
    if (val <= 25) color = "#ef4444";
    else if (val <= 45) color = "#f97316";
    else if (val <= 55) color = "#f59e0b";
    else if (val <= 75) color = "#22c55e";
    else color = "#06b6d4";
    arc.style.stroke = color;
    document.getElementById("fg-value").style.color = color;
    document.getElementById("fg-label").style.color = color;
  }

  function updateOnchain(data) {
    var oc = data.onchain || {};
    document.getElementById("m-funding").textContent = fmt((oc.funding_rate || 0) * 100, 4) + "%";
    document.getElementById("m-oi").textContent = fmtCompact(oc.open_interest || 0) + " BTC";
    var oiChg = oc.oi_change_pct || 0;
    var oiEl = document.getElementById("m-oi-chg");
    oiEl.textContent = (oiChg >= 0 ? "+" : "") + fmt(oiChg, 2) + "%";
    oiEl.style.color = oiChg >= 0 ? "var(--accent-green)" : "var(--accent-red)";

    var liqs = oc.liquidations || {};
    document.getElementById("m-liq-long").textContent = "$" + fmtCompact(liqs.long || 0);
    document.getElementById("m-liq-short").textContent = "$" + fmtCompact(liqs.short || 0);
  }

  function updateOrderbook(data) {
    var ob = data.orderbook || {};
    var bidVol = ob.bid_volume || 0;
    var askVol = ob.ask_volume || 0;
    var total = bidVol + askVol;
    var bidPct = total > 0 ? (bidVol / total * 100) : 50;
    var askPct = 100 - bidPct;

    document.getElementById("ob-bid-bar").style.width = bidPct + "%";
    document.getElementById("ob-bid-bar").textContent = "Bids " + fmt(bidPct, 0) + "%";
    document.getElementById("ob-ask-bar").style.width = askPct + "%";
    document.getElementById("ob-ask-bar").textContent = "Asks " + fmt(askPct, 0) + "%";

    var delta = ob.delta || 0;
    var deltaEl = document.getElementById("ob-delta-val");
    deltaEl.textContent = fmt(delta, 4);
    deltaEl.style.color = delta > 0 ? "var(--accent-green)" : delta < 0 ? "var(--accent-red)" : "var(--text-muted)";

    // Top levels — compact table
    var levelsHtml = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:0 8px;font-size:10px;font-family:var(--font-mono)">';
    var bids = (ob.bids || []).slice(0, 4);
    var asks = (ob.asks || []).slice(0, 4);

    levelsHtml += '<div style="color:var(--text-muted);font-weight:600;padding:2px 0">Ask</div>';
    levelsHtml += '<div style="color:var(--text-muted);font-weight:600;padding:2px 0;text-align:right">Size</div>';
    for (var ai = asks.length - 1; ai >= 0; ai--) {
      levelsHtml += '<div style="color:var(--accent-red)">' + fmtUsd(asks[ai][0]) + '</div>';
      levelsHtml += '<div style="text-align:right;color:var(--text-secondary)">' + fmt(asks[ai][1], 4) + '</div>';
    }
    levelsHtml += '<div style="color:var(--text-muted);font-weight:600;padding:2px 0;border-top:1px solid var(--border)">Bid</div>';
    levelsHtml += '<div style="color:var(--text-muted);font-weight:600;padding:2px 0;text-align:right;border-top:1px solid var(--border)">Size</div>';
    for (var bi = 0; bi < bids.length; bi++) {
      levelsHtml += '<div style="color:var(--accent-green)">' + fmtUsd(bids[bi][0]) + '</div>';
      levelsHtml += '<div style="text-align:right;color:var(--text-secondary)">' + fmt(bids[bi][1], 4) + '</div>';
    }
    levelsHtml += '</div>';
    document.getElementById("ob-levels").innerHTML = levelsHtml;
  }

  function updateMacro(data) {
    var m = data.macro || {};
    document.getElementById("macro-dxy").textContent = fmt(m.dxy, 2);
    setChangeEl("macro-dxy-chg", m.dxy_change);
    document.getElementById("macro-sp").textContent = fmtCompact(m.sp500);
    setChangeEl("macro-sp-chg", m.sp500_change);
    document.getElementById("macro-10y").textContent = fmt(m.us10y, 3) + "%";
    setChangeEl("macro-10y-chg", m.us10y_change, 3);
  }

  function setChangeEl(id, val, decimals) {
    var el = document.getElementById(id);
    if (!el) return;
    var d = decimals || 2;
    var v = val || 0;
    el.textContent = (v >= 0 ? "+" : "") + fmt(v, d) + "%";
    el.style.color = v > 0 ? "var(--accent-green)" : v < 0 ? "var(--accent-red)" : "var(--text-muted)";
  }

  function escHtml(s) {
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(s));
    return div.innerHTML;
  }

  // ── WebSocket Connection ──
  function connect() {
    var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    var url = proto + "//" + window.location.host + "/ws";
    ws = new WebSocket(url);

    ws.onopen = function () {
      document.getElementById("conn-dot").className = "conn-dot connected";
      document.getElementById("conn-text").textContent = "Live";
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    };

    ws.onmessage = function (evt) {
      try {
        var data = JSON.parse(evt.data);
        if (data.type === "update") {
          lastData = data;
          updateHeader(data);
          updateBanner(data);
          updateToolbar(data);
          updateChart(data);
          updateSignalFeed(data);
          updateFearGreed(data);
          updateOnchain(data);
          updateOrderbook(data);
          updateMacro(data);
        } else if (data.type === "settings_updated") {
          populateSettings(data.settings);
        }
      } catch (e) {
        console.error("WS parse error:", e);
      }
    };

    ws.onclose = function () {
      document.getElementById("conn-dot").className = "conn-dot disconnected";
      document.getElementById("conn-text").textContent = "Reconnecting...";
      reconnectTimer = setTimeout(connect, 3000);
    };

    ws.onerror = function () {
      ws.close();
    };
  }

  // ── Settings Modal ──
  function populateSettings(s) {
    if (!s) return;
    var w = s.confluence_weights || {};
    document.getElementById("s-w-tech").value = w.technical || 0.4;
    document.getElementById("s-w-flow").value = w.orderflow || 0.2;
    document.getElementById("s-w-chain").value = w.onchain || 0.15;
    document.getElementById("s-w-sent").value = w.sentiment || 0.15;
    document.getElementById("s-w-macro").value = w.macro || 0.1;
    document.getElementById("s-rsi-period").value = s.rsi_period || 14;
    document.getElementById("s-rsi-ob").value = s.rsi_overbought || 70;
    document.getElementById("s-rsi-os").value = s.rsi_oversold || 30;
    var emas = s.ema_periods || [9, 21, 55, 200];
    document.getElementById("s-ema-1").value = emas[0] || 9;
    document.getElementById("s-ema-2").value = emas[1] || 21;
    document.getElementById("s-ema-3").value = emas[2] || 55;
    document.getElementById("s-ema-4").value = emas[3] || 200;
    document.getElementById("s-conf-high").value = s.min_confluence_high || 75;
    document.getElementById("s-conf-med").value = s.min_confluence_medium || 60;
    document.getElementById("s-conf-exit").value = s.emergency_exit || 40;
    document.getElementById("s-rr1").value = s.rr_primary || 2;
    document.getElementById("s-rr2").value = s.rr_secondary || 3;
  }

  function saveSettings() {
    var newSettings = {
      confluence_weights: {
        technical: parseFloat(document.getElementById("s-w-tech").value),
        orderflow: parseFloat(document.getElementById("s-w-flow").value),
        onchain: parseFloat(document.getElementById("s-w-chain").value),
        sentiment: parseFloat(document.getElementById("s-w-sent").value),
        macro: parseFloat(document.getElementById("s-w-macro").value),
      },
      rsi_period: parseInt(document.getElementById("s-rsi-period").value),
      rsi_overbought: parseInt(document.getElementById("s-rsi-ob").value),
      rsi_oversold: parseInt(document.getElementById("s-rsi-os").value),
      ema_periods: [
        parseInt(document.getElementById("s-ema-1").value),
        parseInt(document.getElementById("s-ema-2").value),
        parseInt(document.getElementById("s-ema-3").value),
        parseInt(document.getElementById("s-ema-4").value),
      ],
      min_confluence_high: parseInt(document.getElementById("s-conf-high").value),
      min_confluence_medium: parseInt(document.getElementById("s-conf-med").value),
      emergency_exit: parseInt(document.getElementById("s-conf-exit").value),
      rr_primary: parseFloat(document.getElementById("s-rr1").value),
      rr_secondary: parseFloat(document.getElementById("s-rr2").value),
    };

    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "update_settings", settings: newSettings }));
    }
    document.getElementById("settings-modal").classList.remove("open");
  }

  // ── Signal Log ──
  function loadSignalLog() {
    fetch("/api/signals")
      .then(function (r) { return r.json(); })
      .then(function (signals) {
        var tbody = document.getElementById("log-tbody");
        var html = "";
        for (var i = signals.length - 1; i >= 0; i--) {
          var s = signals[i];
          var dirClass = s.direction === "LONG" ? "text-green" : "text-red";
          html += "<tr>";
          html += "<td>" + fmtTime(s.timestamp) + "</td>";
          html += '<td class="' + dirClass + '">' + s.direction + "</td>";
          html += "<td>" + fmt(s.score, 0) + "</td>";
          html += "<td>" + s.confidence + "</td>";
          html += "<td>" + fmtUsd(s.entry) + "</td>";
          html += "<td>" + fmtUsd(s.stop_loss) + "</td>";
          html += "<td>" + fmtUsd(s.tp1) + "</td>";
          html += "<td>" + s.market_condition + "</td>";
          html += "</tr>";
        }
        tbody.innerHTML = html || '<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:20px">No signals logged yet</td></tr>';
      })
      .catch(function () {
        document.getElementById("log-tbody").innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted)">Error loading log</td></tr>';
      });
  }

  // ── Event Listeners ──
  function initEvents() {
    // Timeframe buttons
    document.querySelectorAll(".tf-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        document.querySelectorAll(".tf-btn").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        currentTf = btn.getAttribute("data-tf");
        if (lastData) updateChart(lastData);
      });
    });

    // Audio toggle
    document.getElementById("btn-audio").addEventListener("click", function () {
      audioEnabled = !audioEnabled;
      this.classList.toggle("active", audioEnabled);
      if (audioEnabled) {
        // Initialize audio context on user gesture
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        playAlert(); // test beep
      }
    });

    // Settings
    document.getElementById("btn-settings").addEventListener("click", function () {
      document.getElementById("settings-modal").classList.add("open");
      if (lastData && lastData.settings) populateSettings(lastData.settings);
    });
    document.getElementById("close-settings").addEventListener("click", function () {
      document.getElementById("settings-modal").classList.remove("open");
    });
    document.getElementById("save-settings").addEventListener("click", saveSettings);

    // Log
    document.getElementById("btn-log").addEventListener("click", function () {
      document.getElementById("log-modal").classList.add("open");
      loadSignalLog();
    });
    document.getElementById("close-log").addEventListener("click", function () {
      document.getElementById("log-modal").classList.remove("open");
    });

    // Close modals on overlay click
    document.querySelectorAll(".modal-overlay").forEach(function (overlay) {
      overlay.addEventListener("click", function (e) {
        if (e.target === overlay) overlay.classList.remove("open");
      });
    });

    // Keyboard shortcuts
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        document.querySelectorAll(".modal-overlay").forEach(function (m) { m.classList.remove("open"); });
      }
    });
  }

  // ── Init ──
  function init() {
    initChart();
    initEvents();
    connect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
