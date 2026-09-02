import asyncio
import aiohttp
import json
import re
import os
from datetime import datetime, timedelta
from collections import deque
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession

# ================= СПИСОК URL ДЛЯ МОНИТОРИНГА =================
# Пропиши сюда ссылки на те страницы, которые нужно отслеживать.
# При первом старте бот обойдет ВСЕ эти ссылки для прогрева базы.
# В рабочем режиме раз в минуту будет проверяться только ПЕРВАЯ ссылка из этого списка.
KUFAR_URLS = [
    "https://re.kufar.by/l/minsk/snyat/kvartiru?cur=USD&prc=r%3A0%2C420&rms=v.or%3A2%2C3%2C4%2C5",
    "https://re.kufar.by/l/minsk/snyat/kvartiru?cur=USD&page=1&prc=r%3A0%2C420&rms=v.or%3A2%2C3%2C4%2C5&size=30&cursor=eyJ0IjoiYWJzIiwiZiI6dHJ1ZSwicCI6MiwicGl0IjoiMjk2NzE1NzUifQ==",
]

# ================= ЗАГРУЗКА .env =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ Не найден BOT_TOKEN в файле .env!")

admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admin_ids_raw.split(",") if x.strip().isdigit()]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
}

# ================= ПУТИ К ФАЙЛАМ (pathlib) =================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(exist_ok=True)

DB_FILE = DATA_DIR / 'seen_ads.json'
RECENT_ADS_FILE = DATA_DIR / 'recent_ads.json'

# ================= ЛОГИ И СОСТОЯНИЯ =================
system_logs = deque(maxlen=10)


def add_log(msg: str):
    """Добавляет лог с текущим временем (+3 часа)."""
    timestamp = (datetime.now() + timedelta(hours=3)).strftime('%d.%m %H:%M:%S')
    log_line = f"[{timestamp}] {msg}"
    system_logs.append(log_line)
    print(log_line)


def load_json_file(file_path: Path, default_type=list):
    """Универсальная функция загрузки JSON."""
    if file_path.exists():
        try:
            with file_path.open('r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return default_type()
    return default_type()


def save_json_file(file_path: Path, data):
    """Универсальная функция сохранения JSON."""
    with file_path.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


# ================= ПАРСЕР =================
def parse_ad_element(elem):
    """Извлекает нужные данные из HTML-элемента карточки."""
    raw_href = elem.get('href', '')
    clean_href = raw_href.split('?')[0]
    ad_id = clean_href.split('/')[-1]

    address_tag = elem.find('span', class_=re.compile(r'styles_address'))
    address = address_tag.text.strip() if address_tag else "Адрес не указан"

    desc_tag = elem.find('p', class_=re.compile(r'styles_body'))
    description = desc_tag.text.strip() if desc_tag else "Нет описания"

    pub_time = (datetime.now() + timedelta(hours=3)).strftime('%d.%m %H:%M:%S')

    price_tag = elem.find('span', class_=re.compile(r'styles_price__byr'))
    price = price_tag.text.strip() if price_tag else "Цена не указана"

    # Экранируем спецсимволы HTML для Telegram (чтобы не сломать ParseMode.HTML)
    description = description.replace('<', '&lt;').replace('>', '&gt;')

    return {
        'id': ad_id,
        'href': clean_href,
        'address': address,
        'description': description,
        'time': pub_time,
        'price': price
    }


async def fetch_ads_from_url(session: aiohttp.ClientSession, url: str):
    """Асинхронно идет на указанный URL, парсит карточки и возвращает список объявлений."""
    try:
        async with session.get(url, headers=HEADERS, timeout=10) as response:
            response.raise_for_status()
            html = await response.text()
    except Exception as e:
        add_log(f"❌ Ошибка запроса к Kufar ({url.split('?')[0]}): {e}")
        return []

    soup = BeautifulSoup(html, 'lxml')
    elements = soup.find_all('a', attrs={"data-testid": re.compile(r'^kufar-realty-card')})

    ads = []
    for elem in elements:
        try:
            ad_data = parse_ad_element(elem)
            if ad_data['id']:
                ads.append(ad_data)
        except Exception:
            continue
    return ads


# ================= TELEGRAM БОТ И ЛОГИКА =================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

dp = Dispatcher()

# Клавиатура для админов
admin_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📝 Последние 10 логов")],
        [KeyboardButton(text="🏠 Последние 3 квартир")]
    ],
    resize_keyboard=True
)


def format_ad_message(ad: dict) -> str:
    """Формирует красивое сообщение для Telegram."""
    return (
        f"🔥 <b>Новая квартира!</b>\n"
        f"💰 Цена: <b>{ad['price']}</b>\n"
        f"📍 Адрес: {ad['address']}\n"
        f"🕒 Опубликовано: {ad['time']}\n\n"
        f"📝 <i>{ad['description'][:150]}...</i>\n\n"
        f"🔗 <a href='{ad['href']}'>Перейти к объявлению</a>"
    )


async def broadcast_to_admins(text: str):
    """Рассылает сообщение всем админами из списка."""
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, disable_web_page_preview=True)
        except Exception as e:
            add_log(f"⚠️ Ошибка отправки админу {admin_id}: {e}")


# --- Обработчики команд бота ---
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    if message.from_user.id in ADMIN_IDS:
        await message.answer("👋 Привет, админ! Панель управления загружена.", reply_markup=admin_kb)
    else:
        await message.answer("⛔ У вас нет доступа к этому боту.")


@dp.message(F.text == "📝 Последние 10 логов")
async def show_logs(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    if not system_logs:
        await message.answer("Логов пока нет.")
        return

    logs_text = "\n".join(system_logs)
    await message.answer(f"🖥 <b>Системные логи:</b>\n<pre>{logs_text}</pre>")


@dp.message(F.text == "🏠 Последние 3 квартир")
async def show_recent_ads(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    recent_ads = load_json_file(RECENT_ADS_FILE, default_type=list)
    if not recent_ads:
        await message.answer("🤷‍♂️ База последних квартир пуста.")
        return

    await message.answer("👇 Выгружаю последние 3 найденных квартир:")
    for ad in recent_ads:
        await message.answer(format_ad_message(ad), disable_web_page_preview=True)
        await asyncio.sleep(0.3)


# ================= ФОНОВАЯ ЗАДАЧА =================
async def scraper_task():
    """Фоновый цикл парсера, который крутится постоянно."""
    add_log("🚀 Скрипт запущен. Загрузка базы...")

    seen_ads_list = load_json_file(DB_FILE, default_type=list)
    seen_ads = set(seen_ads_list)
    recent_ads = load_json_file(RECENT_ADS_FILE, default_type=list)

    # Прогреваем базу данных, если при старте файл базы пустой
    is_first_run = len(seen_ads) == 0

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # ----------------- ПРОГРЕВ БАЗЫ (Первый запуск) -----------------
                if is_first_run:
                    add_log(f"🔥 Первый запуск: глубокий прогрев базы по {len(KUFAR_URLS)} ссылкам...")
                    for i, url in enumerate(KUFAR_URLS, 1):
                        add_log(f"Парсинг URL {i} из {len(KUFAR_URLS)}...")
                        page_ads = await fetch_ads_from_url(session, url)
                        for ad in page_ads:
                            seen_ads.add(ad['id'])
                            # Заполняем начальный кэш последних объявлений
                            if ad not in recent_ads:
                                recent_ads.append(ad)
                                if len(recent_ads) > 3:
                                    recent_ads.pop(0)
                        await asyncio.sleep(1.5)  # Безопасный интервал между запросами

                    is_first_run = False
                    save_json_file(DB_FILE, list(seen_ads)[-2000:])
                    save_json_file(RECENT_ADS_FILE, recent_ads)
                    add_log("✅ База успешно прогрета. Мониторинг запущен!")
                    continue  # Переходим к стандартному режиму ожидания

                # ----------------- РАБОЧИЙ РЕЖИМ (Каждую минуту) -----------------
                add_log("🔄 Проверка новых объявлений...")
                # Мониторим только первую ссылку из списка (обычно 1-ю страницу выдачи)
                primary_url = KUFAR_URLS[0]
                page_1_ads = await fetch_ads_from_url(session, primary_url)

                new_ads = []
                for ad in reversed(page_1_ads):
                    if ad['id'] and ad['id'] not in seen_ads:
                        new_ads.append(ad)
                        seen_ads.add(ad['id'])

                if new_ads:
                    for ad in new_ads:
                        msg = format_ad_message(ad)
                        await broadcast_to_admins(msg)

                        # Обновляем список последних 3 квартир
                        recent_ads.append(ad)
                        if len(recent_ads) > 3:
                            recent_ads.pop(0)

                        await asyncio.sleep(1)

                    save_json_file(DB_FILE, list(seen_ads)[-2000:])
                    save_json_file(RECENT_ADS_FILE, recent_ads)
                    add_log(f"✅ Найдено новых объявлений: {len(new_ads)}")
                else:
                    add_log("➖ Новых объявлений нет.")

            except Exception as e:
                add_log(f"❌ Непредвиденная ошибка в цикле парсера: {e}")

            # Ждем 60 секунд перед следующей проверкой
            await asyncio.sleep(60)


# ================= ЗАПУСК =================
async def main():
    asyncio.create_task(scraper_task())
    add_log("🤖 Бот запущен и готов к работе.")
    await dp.start_polling(bot)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Остановка скрипта.")
        print("Остановка скрипта.")