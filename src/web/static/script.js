// BeRich Dashboard Scripts

// Auto-refresh data every 10 seconds
const REFRESH_INTERVAL = 10000;

async function fetchData(endpoint) {
    try {
        const response = await fetch(endpoint);
        return await response.json();
    } catch (error) {
        console.error(`Error fetching ${endpoint}:`, error);
        return null;
    }
}

async function updateDashboard() {
    // Update positions
    const positions = await fetchData('/api/positions');
    if (positions) {
        updatePositionsTable(positions);
    }

    // Update RSI
    const rsi = await fetchData('/api/rsi');
    if (rsi) {
        updateRSIGrid(rsi);
    }

    // Update status
    const status = await fetchData('/api/status');
    if (status) {
        updateStatusBar(status);
    }
}

function updatePositionsTable(positions) {
    const tbody = document.querySelector('.positions-panel tbody');
    if (!tbody) return;

    if (positions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty">No positions</td></tr>';
        return;
    }

    tbody.innerHTML = positions.map(pos => `
        <tr>
            <td class="symbol">${pos.symbol}</td>
            <td>${pos.market}</td>
            <td class="number">${pos.quantity}</td>
            <td class="number">${formatNumber(pos.avg_price)}</td>
            <td class="number">${formatNumber(pos.current_price)}</td>
            <td class="number ${pos.pnl >= 0 ? 'positive' : 'negative'}">
                ${formatNumber(pos.pnl, true)}
            </td>
            <td class="number ${pos.pnl_pct >= 0 ? 'positive' : 'negative'}">
                ${pos.pnl_pct >= 0 ? '+' : ''}${pos.pnl_pct.toFixed(1)}%
            </td>
            <td class="number rsi ${getRSIClass(pos.rsi)}">
                ${pos.rsi ? pos.rsi.toFixed(1) : '-'}
            </td>
        </tr>
    `).join('');
}

function updateRSIGrid(rsiValues) {
    const grid = document.querySelector('.rsi-grid');
    if (!grid) return;

    const symbols = Object.keys(rsiValues);
    if (symbols.length === 0) {
        grid.innerHTML = '<p class="empty">No RSI data</p>';
        return;
    }

    grid.innerHTML = symbols.map(symbol => {
        const rsi = rsiValues[symbol];
        return `
            <div class="rsi-item ${getRSIClass(rsi)}">
                <span class="rsi-symbol">${symbol}</span>
                <span class="rsi-value">${rsi.toFixed(1)}</span>
                <div class="rsi-bar">
                    <div class="rsi-fill" style="width: ${rsi}%"></div>
                </div>
            </div>
        `;
    }).join('');
}

function updateStatusBar(status) {
    const updateTime = document.querySelector('.update-time');
    if (updateTime && status.last_update) {
        const date = new Date(status.last_update);
        updateTime.textContent = `Last update: ${date.toLocaleTimeString()}`;
    }
}

function formatNumber(num, showSign = false) {
    if (num === null || num === undefined) return '-';
    const formatted = Math.abs(num).toLocaleString('ko-KR', {
        maximumFractionDigits: 0
    });
    if (showSign) {
        return (num >= 0 ? '+' : '-') + formatted;
    }
    return formatted;
}

function getRSIClass(rsi) {
    if (!rsi) return '';
    if (rsi <= 30) return 'oversold';
    if (rsi >= 70) return 'overbought';
    return '';
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    // Start auto-refresh
    setInterval(updateDashboard, REFRESH_INTERVAL);
});
