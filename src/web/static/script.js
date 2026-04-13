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
    }

    updatePositionsTable(positions) {
        const tbody = document.querySelector('#positions-table tbody');
        if (!tbody) return;

        if (positions.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="empty">No positions</td></tr>';
            return;
        }

        tbody.innerHTML = positions.map(pos => `
            <tr>
                <td data-label="Symbol" class="full-width"><a href="/symbol/${pos.symbol}" class="sym-badge">${pos.symbol}</a></td>
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
        `).join('');
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

            return `
                <div class="rsi-item" onclick="location.href='/symbol/${symbol}'">
                    <div class="rsi-left">
                        <div class="rsi-symbol">${symbol}</div>
                        <div class="rsi-price">${price ? this.formatPrice(price, market) : '-'}</div>
                    </div>
                    <span class="rsi-value ${this.getRSIClass(rsi)}">${rsi.toFixed(1)}</span>
                </div>
            `;
        }).join('');
    }

    updateStatusBar(data) {
        // Bot status
        if (data.bot_status) {
            const statusSpan = document.querySelector('.status-bar .status');
            if (statusSpan) {
                statusSpan.textContent = data.bot_status.running ? 'RUNNING' : 'STOPPED';
                statusSpan.className = `status ${data.bot_status.running ? 'running' : 'stopped'}`;
            }

            const modeSpan = document.querySelector('.status-bar .mode');
            if (modeSpan) {
                modeSpan.textContent = data.bot_status.paper_trading ? 'PAPER' : 'REAL';
                modeSpan.className = `mode ${data.bot_status.paper_trading ? 'paper' : 'real'}`;
            }

            const warmupSpan = document.querySelector('.status-bar .warmup');
            if (warmupSpan) {
                if (data.bot_status.warmup_remaining) {
                    warmupSpan.textContent = `WARMUP: ${data.bot_status.warmup_remaining}`;
                    warmupSpan.style.display = '';
                } else {
                    warmupSpan.style.display = 'none';
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
        const updateTime = document.querySelector('.update-time');
        if (updateTime) {
            const date = new Date(lastUpdate);
            updateTime.textContent = `Last: ${date.toLocaleTimeString()}`;
        }
    }

    updateConnectionStatus(connected) {
        let indicator = document.getElementById('ws-status');
        if (!indicator) {
            indicator = document.createElement('span');
            indicator.id = 'ws-status';
            indicator.className = 'ws-status';
            const statusBar = document.querySelector('.status-bar');
            if (statusBar) {
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
}

// Initialize WebSocket connection
let dashboardWS = null;

document.addEventListener('DOMContentLoaded', () => {
    dashboardWS = new DashboardWebSocket();
    dashboardWS.connect();
    initHamburgerMenu();
    initSwipeNav();
    initChartTheme();
});

// ===================== Hamburger Menu =====================
function initHamburgerMenu() {
    const toggle = document.getElementById('menu-toggle');
    const nav = document.getElementById('main-nav');
    const backdrop = document.getElementById('menu-backdrop');
    if (!toggle || !nav) return;

    function openMenu() {
        nav.classList.add('open');
        toggle.classList.add('open');
        toggle.setAttribute('aria-expanded', 'true');
        if (backdrop) backdrop.classList.add('show');
    }

    function closeMenu() {
        nav.classList.remove('open');
        toggle.classList.remove('open');
        toggle.setAttribute('aria-expanded', 'false');
        if (backdrop) backdrop.classList.remove('show');
    }

    toggle.addEventListener('click', () => {
        nav.classList.contains('open') ? closeMenu() : openMenu();
    });

    if (backdrop) {
        backdrop.addEventListener('click', closeMenu);
    }

    nav.querySelectorAll('a').forEach(link => {
        link.addEventListener('click', closeMenu);
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && nav.classList.contains('open')) {
            closeMenu();
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
    if (!mq) return;

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

    mq.addEventListener('change', (e) => applyChartTheme(e.matches));
}
