// Heikin-Ashi transform, shared by the symbol chart and the backtest chart.
// Loaded from head_extra so it is defined before either page's inline script
// runs (base.html puts {% block scripts %} ahead of script.js).
//
// HA close = the bar's OHLC average; HA open = the previous HA bar's midpoint.
// high/low extend to cover both, so the body is always inside the wick.
// Input and output are lightweight-charts candles: {time, open, high, low, close}.
function toHeikinAshi(candles) {
    let prev = null;
    return candles.map(c => {
        const close = (c.open + c.high + c.low + c.close) / 4;
        const open = prev ? (prev.open + prev.close) / 2 : (c.open + c.close) / 2;
        prev = {
            time: c.time,
            open,
            high: Math.max(c.high, open, close),
            low: Math.min(c.low, open, close),
            close,
        };
        return prev;
    });
}
