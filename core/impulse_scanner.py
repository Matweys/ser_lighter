# core/impulse_scanner.py
"""
Глобальный сканер рынка для обнаружения импульсных движений.
Управляет единым WebSocket-соединением для анализа большого списка торговых пар
и генерирует глобальные события для всех активных пользовательских сессий.
"""
import asyncio
import json
import websockets
from typing import Optional, List, Dict, Any
from decimal import Decimal

from core.logger import log_info, log_error, log_warning
from core.events import EventBus, GlobalCandleEvent  # GlobalCandleEvent - это новый тип события
from core.settings_config import system_config
from api.bybit_api import BybitAPI


class ImpulseScanner:
    """
    Управляет глобальным WebSocket-соединением для сканирования рынка
    на предмет импульсных сигналов. Динамически загружает все торговые пары.
    """

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.public_url = "wss://stream.bybit.com/v5/public/linear"
        self.connection: Optional[websockets.WebSocketClientProtocol] = None
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self.symbols_to_scan: List[str] = []  # <-- Изначально список пуст

    async def start(self):
        """Запуск сканера."""
        if self.running:
            return
        log_info(0, "Запуск глобального ImpulseScanner...", module_name=__name__)

        # Динамически получаем все символы перед запуском
        await self._fetch_all_symbols()
        if not self.symbols_to_scan:
            log_error(0, "Не удалось получить список символов для сканирования. ImpulseScanner не будет запущен.",
                      module_name=__name__)
            return


        self.running = True
        self._task = asyncio.create_task(self._websocket_loop())


    async def _fetch_all_symbols(self ):
        """
        Получает полный список активных USDT фьючерсов с биржи Bybit.
        """
        log_info(0, "Получение списка всех фьючерсных пар с Bybit...", module_name=__name__)
        temp_api = BybitAPI(api_key="", api_secret="", user_id=0, demo=False)
        try:
            instruments = await temp_api.get_instruments_info()

            if instruments:
                # Фильтруем только активные (Trading) бессрочные USDT контракты
                self.symbols_to_scan = [
                    info['symbol'] for info in instruments.values()
                    if info.get('status') == 'Trading' and '-' not in info['symbol'] and info['symbol'].endswith('USDT')
                ]
                log_info(0, f"Получено {len(self.symbols_to_scan)} активных USDT контрактов для сканирования.",
                         module_name=__name__)
            else:
                log_warning(0, "Не удалось получить информацию об инструментах от API.", module_name=__name__)
        except Exception as e:
            log_error(0, f"Критическая ошибка при получении списка символов: {e}", module_name=__name__)
        finally:
            # ВАЖНО: закрываем HTTP соединение временного API
            await temp_api.close()

    async def stop(self):
        """Остановка сканера."""
        if not self.running:
            return
        log_info(0, "Остановка ImpulseScanner...", module_name=__name__)
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.connection:
            await self.connection.close()
        log_info(0, "ImpulseScanner остановлен.", module_name=__name__)

    async def _websocket_loop(self):
        """Основной цикл WebSocket-соединения."""
        while self.running:
            try:
                async with websockets.connect(self.public_url) as websocket:
                    self.connection = websocket
                    log_info(0, f"ImpulseScanner подключен к {self.public_url}", module_name=__name__)
                    await self._subscribe_to_symbols()

                    async for message in websocket:
                        await self._handle_message(message)

            except Exception as e:
                log_error(0, f"Ошибка в ImpulseScanner WebSocket: {e}", module_name=__name__)
                if self.running:
                    await asyncio.sleep(5)  # Пауза перед переподключением

    async def _subscribe_to_symbols(self):
        """Подписка на свечные данные для всех символов."""
        if not self.connection or not self.symbols_to_scan:
            return

        # Bybit позволяет до 200 подписок в одном сообщении для kline
        chunk_size = 100
        log_info(0, f"ImpulseScanner: Начинаю подписку на {len(self.symbols_to_scan)} символов...",
                 module_name=__name__)
        for i in range(0, len(self.symbols_to_scan), chunk_size):
            chunk = self.symbols_to_scan[i:i + chunk_size]
            args = [f"kline.5.{symbol}" for symbol in chunk]  # Используем минутные свечи как триггер
            subscribe_msg = {
                "op": "subscribe",
                "args": args
            }
            await self.connection.send(json.dumps(subscribe_msg))
            log_info(0, f"ImpulseScanner: отправлена подписка на {len(chunk)} символов (часть {i // chunk_size + 1}).",
                     module_name=__name__)
            await asyncio.sleep(0.5)

    async def _handle_message(self, message: str):
        """Обработка входящих сообщений."""
        try:
            data = json.loads(message)
            topic = data.get("topic", "")

            if topic.startswith("kline."):
                candle_data_list = data.get("data", [])
                if not candle_data_list:
                    return

                candle_raw = candle_data_list[0]
                # Нас интересуют только закрытые свечи
                if not candle_raw.get("confirm", False):
                    return

                symbol = topic.split(".")[2]

                candle_event_data = {
                    "symbol": symbol,
                    "interval": "5m",  # Мы подписываемся на минутные свечи
                    "timestamp": int(candle_raw["start"]),
                    "open": Decimal(str(candle_raw["open"])),
                    "high": Decimal(str(candle_raw["high"])),
                    "low": Decimal(str(candle_raw["low"])),
                    "close": Decimal(str(candle_raw["close"])),
                    "volume": Decimal(str(candle_raw["volume"]))
                }

                # Публикуем новое ГЛОБАЛЬНОЕ событие
                event = GlobalCandleEvent(candle_data=candle_event_data)
                await self.event_bus.publish(event)
        except Exception as e:
            # Логируем ошибку, но не останавливаем цикл
            log_error(0, f"Ошибка обработки сообщения в ImpulseScanner: {e}", module_name=__name__)