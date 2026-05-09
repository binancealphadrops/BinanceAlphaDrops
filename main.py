import asyncio
import os
import json
from datetime import datetime, timedelta
import pytz
import requests

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# --- КОНФИГ (Берется из Variables в Railway) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON") 
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID") 

# Инициализация
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
KYIV_TZ = pytz.timezone("Europe/Kyiv")

# --- РАБОТА С GOOGLE SHEETS ---
def get_sheets_client():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).sheet1

def add_drop_to_sheet(ticker, date, amount):
    sheet = get_sheets_client()
    sheet.append_row([ticker, date, amount])

def get_all_drops():
    sheet = get_sheets_client()
    return sheet.get_all_records()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (BINANCE API) ---
def get_kline_data(symbol: str, timestamp_ms: int):
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": f"{symbol.upper()}USDT",
        "interval": "1m",
        "startTime": timestamp_ms,
        "limit": 1
    }
    try:
        response = requests.get(url, params=params)
        data = response.json()
        if not data or "code" in data: return None
        return {
            "open": float(data[0][1]), 
            "high": float(data[0][2]), 
            "low": float(data[0][3]),
            "close": float(data[0][4])
        }
    except: return None

def parse_kyiv_time(date_str: str) -> int:
    dt_obj = datetime.strptime(date_str, "%d.%m.%Y %H:%M")
    kyiv_dt = KYIV_TZ.localize(dt_obj)
    return int(kyiv_dt.timestamp() * 1000)

# --- СОСТОЯНИЯ (FSM) ---
class PriceSearch(StatesGroup):
    waiting_for_ticker = State()
    waiting_for_date = State()

class AddDrop(StatesGroup):
    waiting_for_ticker = State()
    waiting_for_date = State()
    waiting_for_amount = State()

# --- КЛАВИАТУРЫ ---
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎁 Раздачи"), KeyboardButton(text="🔍 Поиск цены")],
        [KeyboardButton(text="➕ Добавить раздачу")]
    ],
    resize_keyboard=True
)

def get_drops_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Последние 5", callback_data="drops_count_5"),
             InlineKeyboardButton(text="Последние 10", callback_data="drops_count_10")],
            [InlineKeyboardButton(text="За неделю", callback_data="drops_time_7"),
             InlineKeyboardButton(text="За месяц", callback_data="drops_time_30")]
        ]
    )

# --- ХЭНДЛЕРЫ ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer("Бот Binance Alpha готов к работе. Данные в Google Sheets.", reply_markup=main_kb)

@dp.message(F.text == "➕ Добавить раздачу")
async def start_add(message: types.Message, state: FSMContext):
    await message.answer("Введите тикер (напр. NOT):")
    await state.set_state(AddDrop.waiting_for_ticker)

@dp.message(AddDrop.waiting_for_ticker)
async def add_ticker(message: types.Message, state: FSMContext):
    await state.update_data(ticker=message.text.upper())
    await message.answer("Введите дату по Киеву (ДД.ММ.ГГГГ ЧЧ:ММ):")
    await state.set_state(AddDrop.waiting_for_date)

@dp.message(AddDrop.waiting_for_date)
async def add_date(message: types.Message, state: FSMContext):
    await state.update_data(date=message.text)
    await message.answer("Сколько токенов насыпали?")
    await state.set_state(AddDrop.waiting_for_amount)

@dp.message(AddDrop.waiting_for_amount)
async def add_amount(message: types.Message, state: FSMContext):
    data = await state.get_data()
    try:
        add_drop_to_sheet(data['ticker'], data['date'], message.text)
        await message.answer(f"✅ {data['ticker']} успешно добавлен!")
    except Exception as e:
        await message.answer(f"❌ Ошибка записи: {e}")
    await state.clear()

@dp.message(F.text == "🎁 Раздачи")
async def show_menu(message: types.Message):
    await message.answer("Выберите количество или период:", reply_markup=get_drops_menu())

@dp.callback_query(F.data.startswith("drops_"))
async def process_drops(callback: types.CallbackQuery):
    await callback.message.edit_text("⏳ Запрашиваю данные с Binance...")
    _, f_type, val = callback.data.split("_")
    
    try:
        all_drops = get_all_drops()
        all_drops.sort(key=lambda x: datetime.strptime(str(x['Date']), "%d.%m.%Y %H:%M"), reverse=True)
        
        if f_type == "count":
            filtered = all_drops[:int(val)]
        else:
            limit = datetime.now() - timedelta(days=int(val))
            filtered = [d for d in all_drops if datetime.strptime(str(d['Date']), "%d.%m.%Y %H:%M") >= limit]

        if not filtered:
            await callback.message.edit_text("В базе пока нет подходящих раздач.")
            return

        res = "📊 <b>Отчет по раздачам:</b>\n\n"
        for d in filtered:
            ts = parse_kyiv_time(str(d['Date']))
            m = get_kline_data(str(d['Ticker']), ts)
            if m:
                amount = float(d['Amount'])
                res += (f"🪙 <b>{d['Ticker']}</b> ({d['Date']})\n"
                        f"Цена: L ${m['low']:.4f} | H ${m['high']:.4f}\n"
                        f"Профит: <b>${amount*m['low']:.2f} - ${amount*m['high']:.2f}</b>\n"
                        f"------------------\n")
        
        await callback.message.edit_text(res, parse_mode="HTML")
    except Exception as e:
        await callback.message.edit_text(f"Ошибка: {e}")

@dp.message(F.text == "🔍 Поиск цены")
async def start_price_search(message: types.Message, state: FSMContext):
    await message.answer("Введите тикер (напр. BTC):")
    await state.set_state(PriceSearch.waiting_for_ticker)

@dp.message(PriceSearch.waiting_for_ticker)
async def process_p_ticker(message: types.Message, state: FSMContext):
    await state.update_data(ticker=message.text.upper())
    await message.answer("Введите время по Киеву (ДД.ММ.ГГГГ ЧЧ:ММ):")
    await state.set_state(PriceSearch.waiting_for_date)

@dp.message(PriceSearch.waiting_for_date)
async def process_p_date(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    try:
        ts = parse_kyiv_time(message.text)
        m = get_kline_data(user_data['ticker'], ts)
        if m:
            text = (f"🔍 <b>{user_data['ticker']}USDT</b>\n"
                    f"🕒 {message.text} (Киев)\n\n"
                    f"Open: ${m['open']:.4f}\n"
                    f"Close: ${m['close']:.4f}\n"
                    f"High: ${m['high']:.4f}\n"
                    f"Low: ${m['low']:.4f}")
            await message.answer(text, parse_mode="HTML")
        else:
            await message.answer("❌ Данные не найдены.")
    except:
        await message.answer("❌ Ошибка формата даты.")
    await state.clear()

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
