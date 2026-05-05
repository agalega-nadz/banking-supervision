"""
Банковский надзорный агент — серверная версия (production)
==========================================================

Автоматический ежедневный мониторинг пула банков с отправкой отчётов в Telegram.
Сохраняет историю — отправляет только если уровень риска изменился ИЛИ это первый запуск.

ВОЗМОЖНОСТИ:
  - Анализ пула банков через Claude
  - Отправка форматированных отчётов в Telegram
  - Сохранение состояния (history.json) — отправляет только при изменении риска
  - Защита от сбоев — отдельные ошибки не валят весь цикл
  - Подробный лог в файл

ЗАВИСИМОСТИ:
  pip install anthropic requests

КОНФИГУРАЦИЯ через переменные окружения:
  ANTHROPIC_API_KEY     - ключ Claude
  TELEGRAM_BOT_TOKEN    - токен бота от @BotFather
  TELEGRAM_CHAT_ID      - ID чата/группы

Запуск:
  python bank_monitor.py

Расписание (cron, ежедневно в 9:00):
  0 9 * * * /usr/bin/python3 /path/to/bank_monitor.py
"""

import os
import re
import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from anthropic import Anthropic

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

# API ключи (берём из переменных окружения для безопасности)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Пул банков для мониторинга
BANK_POOL = [
    {"reg": "646",  "name": "АО ДАТАБАНК"},
    {"reg": "3479", "name": "УРИ БАНК"},
    {"reg": "3340", "name": "МСП БАНК"},
    {"reg": "2309", "name": "БЭНК ОФ ЧАЙНА"},
    {"reg": "3324", "name": "Платежи и Расчеты"},
    # Добавьте свои банки сюда:
    # {"reg": "...", "name": "..."},
]

# Поведение:
# "all"             - отправлять все отчёты каждый раз
# "changes_only"    - только если уровень риска изменился (рекомендуется)
# "high_only"       - только высокий риск
SEND_MODE = "changes_only"

# Особый фокус анализа (опционально)
FOCUS = None  # например: "иски на сумму свыше 100 млн руб."

# Файл для хранения истории
STATE_FILE = Path(__file__).parent / "history.json"
LOG_FILE = Path(__file__).parent / "bank_monitor.log"

# Пауза между банками (защита от rate limits)
DELAY_BETWEEN_BANKS_SEC = 2

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("bank_agent")

# ============================================================
# ХРАНИЛИЩЕ СОСТОЯНИЯ
# ============================================================

def load_state() -> dict:
    """Загружает прошлое состояние (риски по банкам)."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Не удалось прочитать {STATE_FILE}: {e}. Начинаем с нуля.")
        return {}

def save_state(state: dict):
    """Сохраняет текущее состояние."""
    try:
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        log.error(f"Не удалось сохранить состояние: {e}")

# ============================================================
# CLAUDE API
# ============================================================

def analyze_bank(client: Anthropic, bank: dict) -> str:
    """Запрашивает у Claude анализ по одному банку."""

    prompt = f"""Ты — старший аналитик банковского надзора. Подготовь оперативный мониторинговый отчёт по следующему банку.

Банк: {bank['name']}
Регистрационный номер ЦБ РФ: {bank['reg']}
{f'Особый фокус: {FOCUS}' if FOCUS else ''}

Структура отчёта (используй именно эти разделы):

⚖️ СУДЕБНАЯ АКТИВНОСТЬ
Опиши характерные категории судебных исков для данного банка, типичные суммы требований, наиболее значимые тенденции.

📰 НОВОСТНОЙ ФОН
Ключевые медийные события, репутационные риски, корпоративные изменения.

🏛️ РЕГУЛЯТОРНЫЙ ПРОФИЛЬ
Статус взаимодействия с ЦБ РФ, лицензия, нормативы, системная значимость.

🎯 ИТОГОВАЯ ОЦЕНКА
Уровень риска: ВЫСОКИЙ / СРЕДНИЙ / НИЗКИЙ (обязательно одним из этих слов в начале строки)
Краткое обоснование (2-3 предложения).

📌 РЕКОМЕНДАЦИИ НАДЗОРУ
2-3 конкретных действия.

Используй только публично известные факты. Будь лаконичен — каждый раздел 2-4 предложения. Русский язык."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def detect_risk_level(text: str) -> str:
    """Определяет уровень риска из ответа модели."""
    upper = text.upper()
    # Ищем ключевые слова после "УРОВЕНЬ РИСКА" или просто в тексте
    if "ВЫСОКИЙ" in upper or "КРИТИЧ" in upper:
        return "high"
    if "СРЕДНИЙ" in upper or "УМЕРЕННЫЙ" in upper:
        return "mid"
    return "low"


# ============================================================
# ФОРМАТИРОВАНИЕ И ОТПРАВКА
# ============================================================

def format_message(bank: dict, analysis: str, risk_level: str,
                   prev_risk: Optional[str] = None) -> str:
    """Форматирует отчёт для Telegram (HTML)."""

    risk_emoji = {"high": "🔴", "mid": "🟡", "low": "🟢"}[risk_level]
    risk_label = {"high": "ВЫСОКИЙ", "mid": "СРЕДНИЙ", "low": "НИЗКИЙ"}[risk_level]
    date = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Индикатор изменения
    change_marker = ""
    if prev_risk and prev_risk != risk_level:
        prev_label = {"high": "ВЫСОКИЙ", "mid": "СРЕДНИЙ", "low": "НИЗКИЙ"}[prev_risk]
        if {"low": 0, "mid": 1, "high": 2}[risk_level] > {"low": 0, "mid": 1, "high": 2}[prev_risk]:
            change_marker = f"\n📈 <b>УХУДШЕНИЕ:</b> {prev_label} → {risk_label}"
        else:
            change_marker = f"\n📉 Улучшение: {prev_label} → {risk_label}"

    # Экранирование HTML и markdown → Telegram HTML
    body = analysis.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', body)
    body = re.sub(r'__(.+?)__', r'<b>\1</b>', body)

    # Telegram limit = 4096 символов
    if len(body) > 3500:
        body = body[:3500] + "\n\n[отчёт сокращён]"

    return f"""{risk_emoji} <b>НАДЗОРНЫЙ ОТЧЁТ</b>
🏦 <b>{bank['name']}</b> (рег. №{bank['reg']})
📅 {date}
⚠️ Риск: <b>{risk_label}</b>{change_marker}

━━━━━━━━━━━━━━━━━━━━

{body}

━━━━━━━━━━━━━━━━━━━━
<i>Автоматический мониторинг. Требует верификации специалистом.</i>"""


def send_telegram(text: str) -> bool:
    """Отправляет сообщение в Telegram. Возвращает True при успехе."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=20)
        data = r.json()
        if not data.get("ok"):
            log.error(f"Telegram error: {data.get('description')}")
            return False
        return True
    except Exception as e:
        log.error(f"Telegram exception: {e}")
        return False


def should_send(risk_level: str, prev_risk: Optional[str], mode: str) -> tuple[bool, str]:
    """Решает, отправлять ли отчёт. Возвращает (отправить?, причина)."""
    if mode == "all":
        return True, "режим all"
    if mode == "high_only":
        return (risk_level == "high"), f"режим high_only, риск={risk_level}"
    if mode == "changes_only":
        if prev_risk is None:
            return True, "первая проверка"
        if prev_risk != risk_level:
            return True, f"изменение: {prev_risk} → {risk_level}"
        if risk_level == "high":
            return True, "сохраняется ВЫСОКИЙ риск"
        return False, f"риск без изменений ({risk_level})"
    return True, "default"


# ============================================================
# ОСНОВНОЙ ЦИКЛ
# ============================================================

def validate_config() -> bool:
    """Проверяет наличие всех необходимых ключей."""
    missing = []
    if not ANTHROPIC_API_KEY or "sk-ant" not in ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        log.error(f"Не заданы переменные окружения: {', '.join(missing)}")
        return False
    return True


def run_monitoring():
    """Запускает один полный цикл мониторинга."""

    log.info("=" * 60)
    log.info(f"▶ ЗАПУСК МОНИТОРИНГА")
    log.info(f"  Банков в пуле: {len(BANK_POOL)}")
    log.info(f"  Режим: {SEND_MODE}")
    log.info("=" * 60)

    if not validate_config():
        return

    state = load_state()
    log.info(f"Загружено состояние: {len(state)} банков в истории")

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    stats = {"processed": 0, "sent": 0, "skipped": 0, "errors": 0}
    new_state = {}

    for bank in BANK_POOL:
        log.info("─" * 50)
        log.info(f"▶ {bank['name']} (рег. №{bank['reg']})")

        try:
            # 1. Анализ
            analysis = analyze_bank(client, bank)
            risk = detect_risk_level(analysis)
            prev_risk = state.get(bank["reg"], {}).get("risk")
            log.info(f"  Риск: {risk.upper()}" +
                     (f" (было: {prev_risk.upper()})" if prev_risk else " (первый замер)"))

            # 2. Решение об отправке
            send, reason = should_send(risk, prev_risk, SEND_MODE)
            log.info(f"  Решение: {'ОТПРАВИТЬ' if send else 'ПРОПУСТИТЬ'} ({reason})")

            # 3. Отправка
            if send:
                message = format_message(bank, analysis, risk, prev_risk)
                if send_telegram(message):
                    log.info(f"  ✓ Telegram отправлен")
                    stats["sent"] += 1
                else:
                    stats["errors"] += 1
            else:
                stats["skipped"] += 1

            # 4. Обновляем состояние
            new_state[bank["reg"]] = {
                "name": bank["name"],
                "risk": risk,
                "last_check": datetime.now().isoformat(),
            }
            stats["processed"] += 1

            time.sleep(DELAY_BETWEEN_BANKS_SEC)

        except Exception as e:
            log.error(f"  ✗ Ошибка для {bank['name']}: {e}")
            stats["errors"] += 1
            # Сохраняем старое состояние, если было
            if bank["reg"] in state:
                new_state[bank["reg"]] = state[bank["reg"]]

    # Сохраняем итоговое состояние
    save_state(new_state)

    log.info("=" * 60)
    log.info(f"✅ ЗАВЕРШЕНО")
    log.info(f"  Обработано: {stats['processed']}")
    log.info(f"  Отправлено: {stats['sent']}")
    log.info(f"  Пропущено:  {stats['skipped']}")
    log.info(f"  Ошибок:     {stats['errors']}")
    log.info("=" * 60)

    # Если были отправки - можно отправить итоговую сводку (опционально)
    if stats["sent"] > 0 and stats["errors"] == 0:
        summary = (f"📊 <b>Сводка мониторинга</b>\n"
                   f"⏱ {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
                   f"Проверено банков: {stats['processed']}\n"
                   f"Отправлено отчётов: {stats['sent']}\n"
                   f"Без изменений: {stats['skipped']}")
        send_telegram(summary)


if __name__ == "__main__":
    run_monitoring()
