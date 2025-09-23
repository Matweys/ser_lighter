# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a multi-user cryptocurrency futures trading bot built with Python. The bot uses Telegram as the user interface and supports multiple trading strategies for Bybit exchange. The system is event-driven and supports concurrent trading sessions for multiple users.

## Common Development Commands

### Running the Bot
```bash
python main.py
```

### Virtual Environment
The project uses a Python virtual environment located in `.venv/`. Activate with:
```bash
# Windows
.venv\Scripts\activate

# Linux/Mac
source .venv/bin/activate
```

### Dependencies
Install dependencies with:
```bash
pip install -r requirements.txt
```

## High-Level Architecture

### Core Components

1. **main.py** - Application entry point that orchestrates startup sequence
2. **core/bot_application.py** - Main application class managing user sessions
3. **core/user_session.py** - Individual user trading session management
4. **core/events.py** - Event-driven system for inter-component communication
5. **strategies/** - Trading strategy implementations (signal_scalper, impulse_trailing)
6. **database/db_trades.py** - PostgreSQL database management with asyncpg
7. **cache/redis_manager.py** - Redis caching and configuration storage
8. **websocket/websocket_manager.py** - Real-time market data via WebSocket
9. **telegram/** - Telegram bot interface and handlers

### Architecture Patterns

- **Event-Driven**: Uses EventBus for decoupled communication between components
- **Multi-User**: Each user gets isolated trading session with own configurations
- **Strategy Pattern**: Trading strategies inherit from BaseStrategy abstract class
- **Repository Pattern**: Database operations abstracted through db_manager
- **Configuration Management**: Dynamic configs stored in Redis with hot-reloading

### Key Enumerations (core/enums.py)

- `StrategyType`: Available trading strategies
- `ConfigType`: Configuration categories (GLOBAL, STRATEGY_*, COMPONENT_*)
- `EventType`: System events for pub/sub communication
- `PositionSide`, `OrderType`, `OrderStatus`: Trading primitives

### Data Flow

1. **Startup**: main.py → BotApplication → UserSession restoration
2. **User Commands**: Telegram → EventBus → UserSession → Strategy execution
3. **Market Data**: WebSocket → EventBus → Active strategies
4. **Trade Execution**: Strategy → API → Database → Notifications

### Configuration System

- Global configs and strategy-specific configs stored separately in Redis
- ConfigType enum defines all configuration categories
- Hot-reloading of configurations without restart
- Default configurations defined in core/default_configs.py

### Database Schema

- **users**: User profiles and statistics
- **user_api_keys**: Encrypted exchange API credentials
- **trades**: Individual trade records with P&L tracking
- **strategy_stats**: Per-strategy performance metrics

### Critical Dependencies

- **aiogram**: Telegram Bot API framework
- **asyncpg**: Async PostgreSQL driver
- **aioredis**: Async Redis client
- **pybit**: Bybit exchange API wrapper
- **websockets**: WebSocket client for real-time data
- **decimal**: Precise financial calculations

## Important Notes

- All financial calculations use Python's `Decimal` type for precision
- System supports both demo and live trading modes
- Each user session is isolated with separate API connections
- Strategies are event-driven and can be started/stopped independently
- WebSocket connections are managed globally but data is distributed per-user