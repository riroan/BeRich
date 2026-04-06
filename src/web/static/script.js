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
            krwPnl.className = `value ${data.pnl_krw >= 0 ? 'positive' : 'negative'}`;
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
            usdPnl.className = `value ${data.pnl_usd >= 0 ? 'positive' : 'negative'}`;
        }
    }

    updatePositionsTable(positions) {
        const tbody = document.querySelector('.positions-section tbody');
        if (!tbody) return;

        if (positions.length === 0) {
            tbody.innerHTML = '<tr><td colspan="8" class="empty">No positions</td></tr>';
            return;
        }

        tbody.innerHTML = positions.map(pos => `
            <tr onclick="window.location='/symbol/${pos.symbol}'">
                <td class="symbol">${pos.symbol}</td>
                <td>${pos.market}</td>
                <td class="number">${pos.quantity.toLocaleString()}</td>
                <td class="number">${this.formatPrice(pos.avg_price, pos.market)}</td>
                <td class="number">${this.formatPrice(pos.current_price, pos.market)}</td>
                <td class="number ${pos.pnl >= 0 ? 'positive' : 'negative'}">
                    ${this.formatPnl(pos.pnl, pos.market)}
                </td>
                <td class="number ${pos.pnl_pct >= 0 ? 'positive' : 'negative'}">
                    ${pos.pnl_pct >= 0 ? '+' : ''}${pos.pnl_pct.toFixed(2)}%
                </td>
                <td class="number rsi ${this.getRSIClass(pos.rsi)}">
                    ${pos.rsi ? pos.rsi.toFixed(1) : '-'}
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
                <a href="/symbol/${symbol}" class="rsi-item ${this.getRSIClass(rsi)}">
                    <div class="rsi-header">
                        <span class="rsi-symbol">${symbol}</span>
                        ${price ? `<span class="rsi-price">${this.formatPrice(price, market)}</span>` : ''}
                    </div>
                    <span class="rsi-value ${this.getRSIClass(rsi)}">${rsi.toFixed(1)}</span>
                    <div class="rsi-bar">
                        <div class="rsi-fill" style="width: ${rsi}%"></div>
                    </div>
                </a>
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
        if (!rsi) return '';
        if (rsi <= 30) return 'oversold';
        if (rsi >= 70) return 'overbought';
        return '';
    }
}

// Initialize WebSocket connection
let dashboardWS = null;

document.addEventListener('DOMContentLoaded', () => {
    dashboardWS = new DashboardWebSocket();
    dashboardWS.connect();
});
