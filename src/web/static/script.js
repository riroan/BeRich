// BeRich Dashboard Scripts - WebSocket Real-time Updates

class DashboardWebSocket {
    constructor() {
        this.ws = null;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 10;
        this.reconnectDelay = 3000;
        this.pingInterval = null;
        this.isConnected = false;
    }

    connect() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;

        console.log('Connecting to WebSocket:', wsUrl);

        try {
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                console.log('WebSocket connected');
                this.isConnected = true;
                this.reconnectAttempts = 0;
                this.updateConnectionStatus(true);
                this.startPing();
            };

            this.ws.onmessage = (event) => {
                this.handleMessage(event.data);
            };

            this.ws.onclose = (event) => {
                console.log('WebSocket closed:', event.code, event.reason);
                this.isConnected = false;
                this.updateConnectionStatus(false);
                this.stopPing();
                this.scheduleReconnect();
            };

            this.ws.onerror = (error) => {
                console.error('WebSocket error:', error);
                this.updateConnectionStatus(false);
            };
        } catch (error) {
            console.error('Failed to create WebSocket:', error);
            this.scheduleReconnect();
        }
    }

    handleMessage(data) {
        try {
            if (data === 'pong') return;

            const message = JSON.parse(data);
            console.log('Received:', message.type);

            if (message.type === 'init' || message.type === 'tick' || message.type === 'update') {
                this.updateDashboard(message.data);
            }
        } catch (error) {
            console.error('Error handling message:', error);
        }
    }

    updateDashboard(data) {
        if (!data) return;

        // Update balance cards
        this.updateBalances(data);

        // Update positions table
        if (data.positions) {
            this.updatePositionsTable(data.positions);
        }

        // Update RSI grid
        if (data.rsi_values) {
            this.updateRSIGrid(data.rsi_values, data.rsi_prices || {});
        }

        // Update status bar
        this.updateStatusBar(data);

        // Update system status
        if (data.system_status) {
            this.updateSystemStatus(data.system_status);
        }

        // Update last update time
        if (data.last_update) {
            this.updateLastUpdateTime(data.last_update);
        }
    }

    updateBalances(data) {
        // KRW Balance
        const krwBalance = document.getElementById('balance-krw');
        if (krwBalance && data.balance_krw !== undefined) {
            krwBalance.textContent = this.formatKRW(data.balance_krw);
        }

        const krwCash = document.getElementById('cash-krw');
        if (krwCash && data.cash_krw !== undefined) {
            krwCash.textContent = this.formatKRW(data.cash_krw);
        }

        const krwPnl = document.getElementById('pnl-krw');
        if (krwPnl && data.pnl_krw !== undefined) {
            krwPnl.textContent = this.formatKRW(data.pnl_krw, true);
            krwPnl.className = `value small ${data.pnl_krw >= 0 ? 'positive' : 'negative'}`;
        }

        // USD Balance
        const usdBalance = document.getElementById('balance-usd');
        if (usdBalance && data.balance_usd !== undefined) {
            usdBalance.textContent = this.formatUSD(data.balance_usd);
        }

        const usdCash = document.getElementById('cash-usd');
        if (usdCash && data.cash_usd !== undefined) {
            usdCash.textContent = this.formatUSD(data.cash_usd);
        }

        const usdPnl = document.getElementById('pnl-usd');
        if (usdPnl && data.pnl_usd !== undefined) {
            usdPnl.textContent = this.formatUSD(data.pnl_usd, true);
            usdPnl.className = `value small ${data.pnl_usd >= 0 ? 'positive' : 'negative'}`;
        }

        const stickyUsdPnl = document.getElementById('sticky-pnl-usd');
        if (stickyUsdPnl && data.pnl_usd !== undefined) {
            stickyUsdPnl.textContent = this.formatUSD(data.pnl_usd, true);
            stickyUsdPnl.className = `sticky-value ${data.pnl_usd >= 0 ? 'positive' : 'negative'}`;
        }

        // Mobile hero summary (mirrors the desktop cards above)
        const heroBalance = document.getElementById('hero-balance');
        if (heroBalance && data.balance_usd !== undefined) {
            heroBalance.textContent = this.formatUSD(data.balance_usd);
        }

        const heroCash = document.getElementById('hero-cash');
        if (heroCash && data.cash_usd !== undefined) {
            heroCash.textContent = this.formatUSD(data.cash_usd);
        }

        const heroPnl = document.getElementById('hero-pnl');
        if (heroPnl && data.pnl_usd !== undefined) {
            heroPnl.textContent = this.formatUSD(data.pnl_usd, true);
            const sign = data.pnl_usd >= 0 ? 'positive' : 'negative';
            heroPnl.className = `hero-stat-value ${sign}`;
            const heroPnlPct = document.getElementById('hero-pnl-pct');
            if (heroPnlPct && data.balance_usd) {
                const pct = data.pnl_usd / data.balance_usd * 100;
                heroPnlPct.textContent = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
                heroPnlPct.className = `hero-stat-sub ${sign}`;
            }
        }
    }

    updatePositionsTable(positions) {
        const tbody = document.querySelector('#positions-table tbody');
        if (!tbody) return;

        if (positions.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="empty">No positions</td></tr>';
            return;
        }

        tbody.innerHTML = positions.map(pos => {
            const symbol = String(pos.symbol || '');
            const escapedSymbol = this.escapeHTML(symbol);
            const href = `/symbol/${encodeURIComponent(symbol)}`;
            return `
                <tr class="position-row" data-href="${href}"
                    data-symbol="${escapedSymbol}" data-pnl="${pos.pnl_pct}" data-rsi="${pos.rsi != null ? pos.rsi : ''}" data-price="${pos.current_price != null ? pos.current_price : ''}"
                    tabindex="0" role="link" aria-label="Open ${escapedSymbol} chart">
                    <td data-label="Symbol" class="full-width"><a href="${href}" class="sym-badge">${escapedSymbol}</a></td>
                    <td data-label="Price">${this.formatPrice(pos.current_price, pos.market)}</td>
                    <td data-label="P&L" class="${pos.pnl_pct >= 0 ? 'positive' : 'negative'}">
                        ${pos.pnl_pct >= 0 ? '+' : ''}${pos.pnl_pct.toFixed(1)}%
                    </td>
                    <td data-label="RSI" class="${this.getRSIClass(pos.rsi)}">
                        ${pos.rsi != null ? Number(pos.rsi).toFixed(1) : '-'}
                    </td>
                    <td data-label="Stage">${this.renderStageCell(pos)}</td>
                </tr>
            `;
        }).join('');

        this.sortPositions();
    }

    renderStageCell(pos) {
        const buyStage = pos.buy_stage || 0;
        const sellStage = pos.sell_stage || 0;
        const maxBuy = pos.max_buy_stages || 3;
        const maxSell = pos.max_sell_stages || 3;
        const buyReset = this.escapeHTML(pos.buy_stage_reset_remaining || '-');
        const sellReset = this.escapeHTML(pos.sell_stage_reset_remaining || '-');
        const dots = (stage, max) => Array.from({length: max}, (_, i) =>
            `<span class="stage-dot ${i < stage ? 'filled' : ''}"></span>`
        ).join('');

        return `
            <div class="stage-stack">
                <div class="stage-line">
                    <span class="stage-kind">B</span>
                    <span class="stage-count">${buyStage}/${maxBuy}</span>
                    <div class="stage-bar">${dots(buyStage, maxBuy)}</div>
                    <span class="stage-reset">${buyReset}</span>
                </div>
                <div class="stage-line sell">
                    <span class="stage-kind">S</span>
                    <span class="stage-count">${sellStage}/${maxSell}</span>
                    <div class="stage-bar">${dots(sellStage, maxSell)}</div>
                    <span class="stage-reset">${sellReset}</span>
                </div>
            </div>
        `;
    }

    sortPositions() {
        const tbody = document.querySelector('#positions-table tbody');
        if (!tbody) return;
        const key = this._sortValue('positions-sort', 'pnl');
        const dir = this._sortDir('positions-sort-dir');
        const rows = Array.from(tbody.querySelectorAll('.position-row'));
        this._applySort(rows, key, dir, tbody);
    }

    _sortValue(id, fallback) {
        const el = document.getElementById(id);
        return el ? el.value : fallback;
    }

    _sortDir(id) {
        const el = document.getElementById(id);
        return el && el.dataset.dir === 'desc' ? 'desc' : 'asc';
    }

    _applySort(items, key, dir, container) {
        if (items.length < 2) return;
        const mult = dir === 'desc' ? -1 : 1;
        const num = v => { const n = parseFloat(v); return Number.isFinite(n) ? n : null; };
        items.sort((a, b) => {
            if (key === 'symbol') {
                return (a.dataset.symbol || '').localeCompare(b.dataset.symbol || '') * mult;
            }
            // numeric keys ('rsi' | 'price' | 'pnl') — missing values always last
            const av = num(a.dataset[key]);
            const bv = num(b.dataset[key]);
            if (av === null && bv === null) return 0;
            if (av === null) return 1;
            if (bv === null) return -1;
            return (av - bv) * mult;
        });
        items.forEach(item => container.appendChild(item));
    }

    updateRSIGrid(rsiValues, rsiPrices) {
        const grid = document.querySelector('.rsi-grid');
        if (!grid) return;

        const symbols = Object.keys(rsiValues);
        if (symbols.length === 0) {
            grid.innerHTML = '<p class="empty">No RSI data</p>';
            return;
        }

        grid.innerHTML = symbols.map(symbol => {
            const rsi = rsiValues[symbol];
            const priceInfo = rsiPrices[symbol] || {};
            const price = priceInfo.price;
            const market = priceInfo.market;
            const escapedSymbol = this.escapeHTML(symbol);
            const href = `/symbol/${encodeURIComponent(symbol)}`;

            return `
                <a class="rsi-item" href="${href}"
                   data-symbol="${escapedSymbol}" data-rsi="${rsi}" data-price="${price != null ? price : ''}"
                   aria-label="Open ${escapedSymbol} chart">
                    <div class="rsi-left">
                        <div class="rsi-symbol">${escapedSymbol}</div>
                        <div class="rsi-price">${price ? this.formatPrice(price, market) : '-'}</div>
                    </div>
                    <span class="rsi-value ${this.getRSIClass(rsi)}">${rsi != null ? Number(rsi).toFixed(1) : '-'}</span>
                </a>
            `;
        }).join('');

        this.sortRSIGrid();
    }

    sortRSIGrid() {
        const grid = document.querySelector('.rsi-grid');
        if (!grid) return;
        const key = this._sortValue('rsi-sort', 'rsi');
        const dir = this._sortDir('rsi-sort-dir');
        const items = Array.from(grid.querySelectorAll('.rsi-item'));
        this._applySort(items, key, dir, grid);
    }

    updateStatusBar(data) {
        // Bot status
        if (data.bot_status) {
            const statusSpan = document.getElementById('bot-run-status') || document.querySelector('.status-bar .status');
            if (statusSpan) {
                const botStatus = data.bot_status.running ? 'running' : 'stopped';
                statusSpan.className = `pill status bot-status ${botStatus}`;
                statusSpan.setAttribute('aria-label', `Bot ${botStatus}`);
                statusSpan.title = `Bot ${botStatus}`;
                statusSpan.innerHTML = '<span class="bot-status-dot" aria-hidden="true"></span><span class="bot-status-label">BOT</span>';
            }

            const modeSpan = document.getElementById('bot-mode-status') || document.querySelector('.status-bar .mode');
            if (modeSpan) {
                const isPaper = data.bot_status.paper_trading;
                modeSpan.textContent = isPaper ? 'P' : 'R';
                modeSpan.className = `pill mode ${data.bot_status.paper_trading ? 'paper' : 'real'}`;
                modeSpan.setAttribute('aria-label', isPaper ? 'Paper trading' : 'Real trading');
                modeSpan.title = isPaper ? 'Paper' : 'Real';
            }

            const warmupSpan = document.getElementById('bot-warmup-status') || document.querySelector('.status-bar .warmup');
            if (warmupSpan) {
                if (data.bot_status.warmup_remaining) {
                    warmupSpan.textContent = `${data.bot_status.warmup_remaining}`;
                    warmupSpan.setAttribute('aria-label', `Warmup ${data.bot_status.warmup_remaining} remaining`);
                    warmupSpan.hidden = false;
                } else {
                    warmupSpan.hidden = true;
                }
            }
        }
    }

    updateSystemStatus(status) {
        // Auto trading
        const autoTrading = document.querySelector('.system-item:nth-child(1)');
        if (autoTrading) {
            autoTrading.className = `system-item ${status.auto_trading_enabled ? 'ok' : 'off'}`;
            autoTrading.querySelector('.value').textContent = status.auto_trading_enabled ? 'ON' : 'OFF';
        }

        // Last price update
        const lastPriceUpdate = document.getElementById('last-price-update');
        if (lastPriceUpdate) {
            lastPriceUpdate.textContent = status.last_price_update || 'N/A';
        }
    }

    updateLastUpdateTime(lastUpdate) {
        const date = new Date(lastUpdate);
        const formattedTime = Number.isNaN(date.getTime())
            ? (lastUpdate || 'N/A')
            : date.toLocaleTimeString('ko-KR', {
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: false,
            });
        // Header timestamp was removed; keep the desktop sticky-pnl "Last" in sync.
        const updateTime = document.getElementById('last-update-time') || document.querySelector('.update-time');
        if (updateTime) updateTime.textContent = `Last: ${formattedTime}`;
        const stickyLast = document.getElementById('sticky-last-update');
        if (stickyLast) stickyLast.textContent = formattedTime;
    }

    updateConnectionStatus(connected) {
        let indicator = document.getElementById('ws-status');
        if (!indicator) {
            indicator = document.createElement('span');
            indicator.id = 'ws-status';
            indicator.className = 'ws-status';
            indicator.setAttribute('role', 'status');
            indicator.setAttribute('aria-live', 'polite');
            const statusBar = document.querySelector('.status-bar');
            const headerRight = document.querySelector('.header-right');
            if (headerRight) {
                headerRight.insertBefore(indicator, headerRight.firstChild);
            } else if (statusBar) {
                statusBar.insertBefore(indicator, statusBar.firstChild);
            }
        }

        if (connected) {
            indicator.textContent = '';
            indicator.className = 'ws-status connected';
            indicator.setAttribute('aria-label', 'Live connection');
            indicator.title = 'LIVE';
        } else {
            indicator.textContent = '';
            indicator.className = 'ws-status disconnected';
            indicator.setAttribute('aria-label', 'Offline connection');
            indicator.title = 'OFFLINE';
        }

        const stickyWs = document.getElementById('sticky-ws-value');
        if (stickyWs) {
            stickyWs.textContent = connected ? 'LIVE' : 'OFFLINE';
            stickyWs.className = `sticky-value ${connected ? 'positive' : 'negative'}`;
        }
    }

    startPing() {
        this.pingInterval = setInterval(() => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send('ping');
            }
        }, 30000);
    }

    stopPing() {
        if (this.pingInterval) {
            clearInterval(this.pingInterval);
            this.pingInterval = null;
        }
    }

    scheduleReconnect() {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            console.log('Max reconnect attempts reached');
            return;
        }

        this.reconnectAttempts++;
        const delay = this.reconnectDelay * this.reconnectAttempts;
        console.log(`Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})`);

        setTimeout(() => {
            this.connect();
        }, delay);
    }

    // Utility functions
    formatKRW(value, showSign = false) {
        const num = Math.round(value);
        const formatted = Math.abs(num).toLocaleString('ko-KR');
        if (showSign && num !== 0) {
            return (num >= 0 ? '+' : '-') + formatted + ' KRW';
        }
        return formatted + ' KRW';
    }

    formatUSD(value, showSign = false, decimals = 2) {
        const formatted = Math.abs(value).toLocaleString('en-US', {
            minimumFractionDigits: decimals,
            maximumFractionDigits: decimals
        });
        if (showSign && value !== 0) {
            return (value >= 0 ? '+$' : '-$') + formatted;
        }
        return '$' + formatted;
    }

    formatPrice(price, market) {
        if (price === null || price === undefined) return '-';
        if (market === 'KRX') {
            return Math.round(price).toLocaleString('ko-KR');
        }
        return '$' + price.toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    formatPnl(pnl, market) {
        if (pnl === null || pnl === undefined) return '-';
        if (market === 'KRX') {
            const sign = pnl >= 0 ? '+' : '';
            return sign + Math.round(pnl).toLocaleString('ko-KR');
        }
        const sign = pnl > 0 ? '+' : (pnl < 0 ? '-' : '');
        return sign + '$' + Math.abs(pnl).toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    getRSIClass(rsi) {
        const value = Number(rsi);
        if (rsi === null || rsi === undefined || Number.isNaN(value)) return 'rsi-neutral';
        if (value <= 24 || value >= 76) return 'rsi-danger';
        if (value <= 35 || value >= 65) return 'rsi-warning';
        return 'rsi-neutral';
    }

    escapeHTML(value) {
        return String(value).replace(/[&<>"']/g, char => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
        })[char]);
    }
}

// Initialize WebSocket connection
let dashboardWS = null;

document.addEventListener('DOMContentLoaded', () => {
    dashboardWS = new DashboardWebSocket();
    dashboardWS.connect();
    initPositionRowLinks();
    initSwipeNav();
    initChartTheme();
    initRSISort();
    initPositionsSort();
});

function initSortControls(selectId, dirId, storageKey, apply) {
    const select = document.getElementById(selectId);
    const dirBtn = document.getElementById(dirId);
    if (!select || !dirBtn) return;

    const savedKey = localStorage.getItem(storageKey + 'Key');
    const savedDir = localStorage.getItem(storageKey + 'Dir');
    if (savedKey) select.value = savedKey;
    if (savedDir === 'asc' || savedDir === 'desc') dirBtn.dataset.dir = savedDir;
    dirBtn.textContent = dirBtn.dataset.dir === 'desc' ? '↓' : '↑';

    const run = () => { if (dashboardWS) apply(); };

    select.addEventListener('change', () => {
        localStorage.setItem(storageKey + 'Key', select.value);
        run();
    });
    dirBtn.addEventListener('click', () => {
        const next = dirBtn.dataset.dir === 'desc' ? 'asc' : 'desc';
        dirBtn.dataset.dir = next;
        dirBtn.textContent = next === 'desc' ? '↓' : '↑';
        localStorage.setItem(storageKey + 'Dir', next);
        run();
    });
    run();
}

function initRSISort() {
    initSortControls('rsi-sort', 'rsi-sort-dir', 'rsiSort', () => dashboardWS.sortRSIGrid());
}

function initPositionsSort() {
    initSortControls('positions-sort', 'positions-sort-dir', 'positionsSort', () => dashboardWS.sortPositions());
}

function initPositionRowLinks() {
    document.addEventListener('click', event => {
        const row = event.target.closest('.position-row[data-href]');
        if (!row || event.target.closest('a, button, input, select, textarea')) return;
        window.location.href = row.dataset.href;
    });

    document.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        const row = event.target.closest('.position-row[data-href]');
        if (!row || event.target.closest('a, button, input, select, textarea')) return;
        event.preventDefault();
        window.location.href = row.dataset.href;
    });
}

// ===================== Swipe Navigation =====================
function initSwipeNav() {
    // Disabled: swipe navigation removed (caused accidental page changes)
}

// ===================== Chart Theme Detection =====================
function initChartTheme() {
    const mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;

    function applyChartTheme(isLight) {
        document.querySelectorAll('[data-chart]').forEach(el => {
            if (el._chart) {
                el._chart.applyOptions({
                    layout: {
                        background: { color: isLight ? '#f8fafc' : '#0f172a' },
                        textColor: isLight ? '#64748b' : '#94a3b8',
                    },
                    grid: {
                        vertLines: { color: isLight ? '#e2e8f0' : '#1e293b' },
                        horzLines: { color: isLight ? '#e2e8f0' : '#1e293b' },
                    },
                });
            }
        });
    }

    applyChartTheme(mq ? mq.matches : false);

    if (mq) {
        mq.addEventListener('change', event => {
            applyChartTheme(event.matches);
        });
    }
}
