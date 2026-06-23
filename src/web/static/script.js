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
                        ${pos.rsi ? pos.rsi.toFixed(1) : '-'}
                    </td>
                    <td data-label="Stage">
                        <div class="stage-bar">
                            ${Array.from({length: pos.max_buy_stages || 3}, (_, i) =>
                                `<span class="stage-dot ${i < (pos.buy_stage || 0) ? 'filled' : ''}"></span>`
                            ).join('')}
                        </div>
                    </td>
                </tr>
            `;
        }).join('');

        this.sortPositions();
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
                    <span class="rsi-value ${this.getRSIClass(rsi)}">${rsi.toFixed(1)}</span>
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
                statusSpan.textContent = data.bot_status.running ? 'Running' : 'Stopped';
                statusSpan.className = `pill status ${data.bot_status.running ? 'running' : 'stopped'}`;
            }

            const modeSpan = document.getElementById('bot-mode-status') || document.querySelector('.status-bar .mode');
            if (modeSpan) {
                modeSpan.textContent = data.bot_status.paper_trading ? 'Paper' : 'Real';
                modeSpan.className = `pill mode ${data.bot_status.paper_trading ? 'paper' : 'real'}`;
            }

            const warmupSpan = document.getElementById('bot-warmup-status') || document.querySelector('.status-bar .warmup');
            if (warmupSpan) {
                if (data.bot_status.warmup_remaining) {
                    warmupSpan.textContent = `WARMUP: ${data.bot_status.warmup_remaining}`;
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
        const updateTime = document.getElementById('last-update-time') || document.querySelector('.update-time');
        if (updateTime) {
            const date = new Date(lastUpdate);
            if (Number.isNaN(date.getTime())) {
                const fallback = lastUpdate || 'N/A';
                updateTime.textContent = `Last: ${fallback}`;
                const stickyLast = document.getElementById('sticky-last-update');
                if (stickyLast) stickyLast.textContent = fallback;
                return;
            }
            const formattedTime = date.toLocaleTimeString('ko-KR', {
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: false,
            });
            updateTime.textContent = `Last: ${formattedTime}`;
            const stickyLast = document.getElementById('sticky-last-update');
            if (stickyLast) {
                stickyLast.textContent = formattedTime;
            }
        }
    }

    updateConnectionStatus(connected) {
        let indicator = document.getElementById('ws-status');
        if (!indicator) {
            indicator = document.createElement('span');
            indicator.id = 'ws-status';
            indicator.className = 'ws-status';
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
            indicator.textContent = 'LIVE';
            indicator.className = 'ws-status connected';
        } else {
            indicator.textContent = 'OFFLINE';
            indicator.className = 'ws-status disconnected';
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

    formatUSD(value, showSign = false) {
        const formatted = Math.abs(value).toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
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
        const sign = pnl >= 0 ? '+' : '';
        if (market === 'KRX') {
            return sign + Math.round(pnl).toLocaleString('ko-KR');
        }
        return sign + '$' + Math.abs(pnl).toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    getRSIClass(rsi) {
        if (!rsi) return 'rsi-neutral';
        if (rsi <= 24 || rsi >= 76) return 'rsi-danger';
        if (rsi <= 35 || rsi >= 65) return 'rsi-warning';
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
    initHamburgerMenu();
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

// ===================== Hamburger Menu =====================
function initHamburgerMenu() {
    const toggle = document.getElementById('menu-toggle');
    const nav = document.getElementById('main-nav');
    const backdrop = document.getElementById('menu-backdrop');
    if (!toggle || !nav) return;
    let lastFocusedBeforeMenu = null;

    function menuFocusables() {
        return Array.from(nav.querySelectorAll('a[href]')).filter(el => !el.hasAttribute('disabled'));
    }

    function openMenu() {
        lastFocusedBeforeMenu = document.activeElement;
        nav.classList.add('open');
        toggle.classList.add('open');
        toggle.setAttribute('aria-expanded', 'true');
        if (backdrop) backdrop.classList.add('show');
        const firstLink = nav.querySelector('a[href]');
        if (firstLink) firstLink.focus();
    }

    function closeMenu(restoreFocus = true) {
        nav.classList.remove('open');
        toggle.classList.remove('open');
        toggle.setAttribute('aria-expanded', 'false');
        if (backdrop) backdrop.classList.remove('show');
        if (restoreFocus && lastFocusedBeforeMenu && typeof lastFocusedBeforeMenu.focus === 'function') {
            lastFocusedBeforeMenu.focus();
        }
    }

    toggle.addEventListener('click', () => {
        nav.classList.contains('open') ? closeMenu() : openMenu();
    });

    if (backdrop) {
        backdrop.addEventListener('click', closeMenu);
    }

    nav.querySelectorAll('a').forEach(link => {
        link.addEventListener('click', () => closeMenu(false));
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && nav.classList.contains('open')) {
            closeMenu();
            return;
        }

        if (e.key !== 'Tab' || !nav.classList.contains('open')) return;
        const focusables = menuFocusables();
        if (focusables.length === 0) return;

        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
        }
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
