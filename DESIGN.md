# Design System — BeRich

## Product Context
- **What this is:** RSI Mean Reversion 기반 자동매매 트레이딩 봇 + 실시간 대시보드
- **Who it's for:** 개인 투자자 (본인 사용, 모니터링 및 전략 관리)
- **Space/industry:** 퀀트 트레이딩, 핀테크 대시보드
- **Project type:** Web dashboard (FastAPI + Jinja2)

## Aesthetic Direction
- **Direction:** Modern Fintech
- **Decoration level:** Intentional (카드 그림자, sparkline, 뱃지로 시각적 계층 구성)
- **Mood:** 전문적이면서 깔끔한 다크 대시보드. 데이터가 주인공이고, UI는 데이터를 방해하지 않는다.
- **Approved mockup:** Variant B (2026-04-09)

## Typography
- **Primary:** Inter — 깔끔한 산세리프, 숫자가 잘 정렬됨
- **Loading:** Google Fonts `@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap')`
- **Fallback:** -apple-system, BlinkMacSystemFont, sans-serif
- **Numbers:** `font-variant-numeric: tabular-nums` 적용 (모든 숫자 테이블, 카드 값)
- **Scale:**
  - Hero value: 28px / 700
  - Card value (small): 20px / 700
  - Section title: 16px / 600
  - Body: 13-14px / 400
  - Label: 12px / 500, uppercase, letter-spacing 0.5px
  - Badge/Tag: 11-12px / 600
  - Muted/Sub: 11px

## Color
- **Approach:** Restrained (1 accent + semantic colors)
- **Background:** `--bg: #0f172a` (deep navy)
- **Surface:** `--surface: #1e293b` (card/panel background)
- **Card/Border:** `--card: #334155` (subtle borders, secondary surfaces)
- **Border:** `--border: rgba(255,255,255,0.06)`
- **Text:** `--text: #e2e8f0` (primary), `--dim: #94a3b8` (secondary), `--white: #f8fafc` (emphasis)
- **Muted:** `--text-muted: #64748b`
- **Accent (primary):** `--blue: #3b82f6` (active nav, buttons, links, charts)
- **Semantic:**
  - Success/Positive: `--emerald: #10b981`
  - Error/Negative: `--red: #ef4444`
  - Warning: `--amber: #f59e0b`
  - Oversold: `--emerald` (RSI <= 35)
  - Overbought: `--red` (RSI >= 60)
  - Neutral RSI: `--white` (36-59)

## Spacing
- **Base unit:** 4px
- **Density:** Comfortable
- **Scale:** xs(4) sm(8) md(12) lg(16) xl(20) 2xl(24) 3xl(32)
- **Card padding:** 20px
- **Container padding:** 0 32px 32px
- **Gap between sections:** 20px
- **Gap between cards:** 16px

## Layout
- **Approach:** Grid-disciplined
- **Max content width:** 1200px, centered (`margin: 0 auto`)
- **Header/Nav:** 동일한 max-width, 중앙 정렬
- **Card grid:** `repeat(4, 1fr)` (summary cards)
- **Two-col:** `3fr 2fr` (positions + RSI monitor)
- **Three-col:** `repeat(3, 1fr)` (signal cards)
- **Border radius:**
  - Cards/Panels: 12px (`--radius`)
  - Buttons/Inputs: 8px
  - Pills/Badges: 20px (full round)
  - RSI badges: 12px
- **Box shadow:** `0 4px 24px rgba(0,0,0,0.3)` (`--shadow`)

## Components

### Pills (status badges)
- Padding: 6px 14px, border-radius: 20px, font-size: 12px, font-weight: 600
- Running: `rgba(16,185,129,0.15)` bg, emerald text
- Stopped/Real: `rgba(239,68,68,0.15)` bg, red text
- Paper: `rgba(245,158,11,0.15)` bg, amber text

### Navigation
- Horizontal pill-style nav bar
- Inactive: dim color, 8px 16px padding, 8px radius
- Active: blue background, white text
- Hover: surface background

### Tables
- Header: 11px, dim, uppercase, letter-spacing 0.5px
- Rows: 13px, border-bottom `rgba(51,65,85,0.5)`
- Hover: `rgba(59,130,246,0.04)` background
- Symbol column: font-weight 600, white

### Signal Cards
- Background: surface, 16px padding
- Title: 13px, dim, font-weight 500
- Items: flex space-between, 8px padding, border-bottom
- Tags: 3px 10px padding, 8px radius
  - Buy: `rgba(16,185,129,0.12)` bg, emerald text
  - Sell: `rgba(239,68,68,0.12)` bg, red text

### RSI Monitor
- 2-column grid, 10px gap
- Items: bg background, 10px radius, 14px padding
- Symbol: 14px, white, font-weight 600
- Price: 12px, dim
- RSI value: 22px, font-weight 700, color by threshold

### Forms
- Input/Select: bg background, card border, 8px radius, 10px 14px padding
- Focus: blue border
- Button: blue bg, white text, 20px radius, 10px 24px padding
- Button hover: #2563eb + blue glow shadow

### Charts (Lightweight Charts)
- Background: `#0f172a`
- Grid: `#1e293b`
- Up candle: `#10b981`
- Down candle: `#ef4444`
- Line/RSI: `#3b82f6`

## Motion
- **Approach:** Minimal-functional
- **Transitions:** `all 0.2s` (hover states, focus, color changes)
- **No entrance animations** (data dashboard, instant load preferred)

## Responsive Breakpoints
- **1024px:** cards 2-column, signal/three-col 1-column, two-col 1-column
- **768px:** cards 2-column, tables → card conversion, hamburger menu, sticky P&L header
- **480px:** dashboard summary → 1-column, system status → 2-column

## Mobile Overrides (≤768px)
- **Typography floor:** 10px for card data-labels (below desktop 11px floor)
- **Card padding:** 12px (reduced from desktop 20px)
- **Card values:** 16px (reduced from desktop 20px)
- **Navigation:** Hamburger menu (3-line → X transform, slide-down, 500px max-height transition)
- **Sticky P&L:** Always-visible header with total P&L + WS connection status
- **Touch feedback:** `:active` scale(0.98) + opacity on cards (hover: none media query)
- **Table → Card:** `.mobile-cards` class converts tables to 2-column grid cards with data-labels
- **Light theme:** `prefers-color-scheme: light` auto-detection with darkened accent colors for WCAG AA

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-09 | Variant B (Modern Fintech) 선택 | 3개 방향(Bloomberg Terminal, Modern Fintech, Minimal Command Center) 비교 후 선택 |
| 2026-04-09 | Inter 폰트 채택 | 숫자 정렬(tabular-nums)이 좋고 깔끔한 핀테크 느낌 |
| 2026-04-09 | Navy 배경(#0f172a) 채택 | 순검정보다 부드럽고, 데이터 가독성이 좋음 |
| 2026-04-09 | two-col 비율 3fr:2fr | Positions 테이블이 RSI 모니터보다 더 넓어야 함 |
| 2026-04-09 | RSI 색상 기준: <=35 green, >=60 red, 나머지 white | 30/70 기준보다 현실적인 시그널 범위 반영 |
