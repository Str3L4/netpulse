# NETPULSE

**NETPULSE** — автономный диспетчерский дашборд и агент самовосстановления для гибридных сетевых инфраструктур, сочетающих локальные рабочие станции, маршрутизаторы (MikroTik / OpenWrt) и Linux-шлюзы обхода блокировок (Zapret DPI bypass + Xray VLESS Reality).

---

## Архитектура системы

```
[ Рабочая станция ] ──(LAN)──> [ MikroTik Router ] ──(WAN)──> [ Интернет / GGC ]
        │                             │
  (ProxyBridge / SOCKS5)       (Mangle / FastTrack)
        │                             │
        └──────────────┬──────────────┘
                       ▼
              [ Linux Gateway ]
           (Zapret DPI + Xray Core)
                       │
                       ▼
             [ Offshore VLESS VPS ]
```

### Ключевые возможности:
1. **Динамическая топология**: Все сетевые узлы, роли и параметры зондирования настраиваются в `config.yaml`.
2. **Мониторинг обхода DPI и туннелей**:
   - Zapret NFQUEUE / Fake-TLS (проверка генерации HTTP 204 YouTube без прокси).
   - Xray VLESS + Reality (SOCKS5/HTTP probe через внешний IP).
   - Google CloudCode & Gemini API health check.
3. **Матрица проверки сервисов (DPI Probe Matrix)**:
   - Автоматическая проверка задержек и доступности ключевых сервисов: YouTube, Discord Gateway, OpenAI API, Google CloudCode, GitHub API через прямой маршрут или SOCKS5-туннель.
4. **Реальный трафик портов MikroTik (Live Bandwidth)**:
   - Снятие мгновенной скорости приема и передачи (Rx/Tx kbps/Mbps) на портах `ether1 (WAN)`, `ether2 (PC)`, `ether3 (Gateway)`.
5. **Обнаружение устройств и Wake-on-LAN**:
   - Автоматический сбор клиентов локальной сети по таблице DHCP Leases маршрутизатора.
   - Отправка Magic Packet (WOL) в один клик для удаленного пробуждения ПК.
6. **Фоновый автохилинг (Self-Healing Watchdog)**:
   - Мониторинг подряд идущих сбоев: если YouTube или VLESS недоступны заданное число проверок, система автоматически запускает конвейер восстановления эталона и уведомляет администратора.
7. **Telegram-бот для удаленного управления**:
   - Получение сводки `/status`, запуск починки `/repair` и включение ПК `/wol` прямо из Telegram.
   - Автоматические оповещения об авариях и успешных автовосстановлениях.
8. **Конвейер восстановления («Починить всё» / `/repair`)**:
   - Автоматический сброс системного прокси Windows и WinINet в прямое подключение (Direct, `0x01`).
   - Синхронизация профиля ProxyBridge (отключение перехвата для GUI Electron и включение для language-server).
   - Перезапуск служб Linux-шлюза и опциональная сверка с эталонным конфигом Xray.
   - Восстановление правил маршрутизатора (Mangle-правила автозахвата динамических CDN YouTube `*.googlevideo.com`, исключения FastTrack, сброс зависших сессий).
9. **Встроенный сетевой ассистент**:
   - Работает полностью автономно в оффлайн-режиме (встроенные диагностические эвристики и быстрые команды).
   - Поддерживает прямое подключение Google Gemini API при указании ключа.

---

## Быстрый старт

### Вариант 1: Локальный запуск (Python)

#### 1. Требования
- Python 3.9+
- Установленные зависимости из `requirements.txt`

#### 2. Установка
```bash
git clone https://github.com/Str3L4/netpulse.git
cd netpulse
pip install -r requirements.txt
```

#### 3. Конфигурация
Создайте конфигурационные файлы на основе примеров:

```bash
# Windows PowerShell
cp .env.example .env
cp config.example.yaml config.yaml
```

1. Заполните `.env` адресами и учетными записями роутера и шлюза:
   ```env
   MIKROTIK_HOST=192.168.1.1
   MIKROTIK_USER=admin
   MIKROTIK_PASSWORD=your_router_password

   GATEWAY_HOST=192.168.1.254
   GATEWAY_USER=root
   GATEWAY_PASSWORD=your_gateway_password

   # Опционально: Telegram-бот для алертов
   TELEGRAM_BOT_TOKEN=
   TELEGRAM_ADMIN_CHAT_ID=
   ```

2. Отредактируйте `config.yaml` под свою структуру сети: настройте список узлов (`nodes`), параметры портов (`services`), матрицу сервисов (`service_matrix`) и параметры автохилинга (`watchdog`).

#### 4. Запуск
```bash
python server.py
# или дважды кликните start_dashboard.bat в Windows
```

Откройте интерфейс в браузере: `http://127.0.0.1:8888`

---

### Вариант 2: Запуск в Docker

```bash
docker compose up -d
```

---

## Безопасность и хранение секретов

Репозиторий спроектирован по стандарту безопасной изоляции конфигураций:
- Файлы `.env`, `config.yaml`, `chat_config.json`, `xray_golden.json` и `ANTIGRAVITY_EMERGENCY_CONTEXT.md` включены в `.gitignore` и никогда не попадают в систему контроля версий.
- API-эндпоинт `/api/config` автоматически санитизирует конфигурацию перед передачей в браузер, исключая пароли и токены.
- В репозитории распространяются только шаблонные файлы `config.example.yaml`, `.env.example` и `EMERGENCY_CONTEXT.example.md`.

---

## Структура проекта

```
netpulse/
├── server.py                     # Асинхронный сервер телеметрии, watchdog и бот
├── start_dashboard.bat           # Скрипт быстрого запуска для Windows
├── Dockerfile                    # Docker-образ
├── docker-compose.yml            # Docker Compose манифест
├── requirements.txt              # Зависимости Python
├── config.example.yaml           # Шаблон конфигурации сети, узлов и сервисов
├── .env.example                  # Шаблон переменных окружения
├── EMERGENCY_CONTEXT.example.md  # Шаблон архитектурного контекста
├── public/
│   └── index.html                # Инженерный дашборд (Chart.js, WebSockets/REST)
└── .gitignore                    # Исключения приватных данных и секретов
```
