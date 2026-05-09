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

# --- КОНФИГ (Переменные Railway) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON") 
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID") 

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

def add_drop_to_sheet(ticker, date, amount, low_p, high_p):
    sheet = get_sheets_client()
    # Расчет профита для записи в таблицу
    try:
        p_low = float(amount) * float(low_p)
        p_high = float(amount) * float(high_p)
        profit_str = f"{p_low:.2f}-{p_high:.2f} USDT"
    except:
        profit_str = "Ошибка расчета"
        
    sheet.append_row([str(ticker), str(date), str(amount), str(low_p), str(high_p), profit_str])

def get_all_drops():
    try:
        sheet = get_sheets_client()
        return sheet.get_all_records()
    except Exception as e:
        print(f"Ошибка чтения таблицы: {e}")
        return []

# --- BINANCE API ---
def get_kline_data(symbol: str, timestamp_ms: int):
    if symbol.startswith("0x"):
        return {"error": "Web3 контракт"}
        
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": f"{symbol.strip().upper()}USDT",
        "interval": "1m",
        "startTime": timestamp_ms,
        "limit": 1
    }
    try:
        response = requests.get(url, params=params)
        data = response.json()
        if isinstance(data, dict) and "code" in data:
            return {"error": f"Binance: {data.get('msg')}"}
        if not data: 
            return {"error": "Свеча не найдена"}
        return {
            "open": float(data[0][1]), "high": float(data[0][2]), 
            "low": float(data[0][3]), "close": float(data[0][4])
        }
    except:
        return {"error": "Ошибка API"}

def parse_kyiv_time(date_str: str) -> int:
    dt_obj = datetime.strptime(date_str, "%d.%m.%Y %H:%M")
    kyiv_dt = KYIV_TZ.localize(dt_obj)
    return int(kyiv_dt.timestamp() * 1000)

# --- СОСТОЯНИЯ ---
class PriceSearch(StatesGroup):
    waiting_for_ticker = State()
    waiting_for_date = State()

class AddDrop(StatesGroup):
    waiting_for_ticker = State()
    waiting_for_date = State()
    waiting_for_amount = State()
    waiting_for_manual_price = State()

# --- МЕНЮ ---
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

# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer("Бот Binance Alpha обновлен. Все данные пишутся в Google Sheets.", reply_markup=main_kb)

@dp.message(F.text == "➕ Добавить раздачу")
async def start_add(message: types.Message, state: FSMContext):
    await message.answer("Введите тикер (NOT) или адрес контракта (0x...):")
    await state.set_state(AddDrop.waiting_for_ticker)

@dp.message(AddDrop.waiting_for_ticker)
async def add_ticker(message: types.Message, state: FSMContext):
    ticker = message.text.strip()
    await state.update_data(ticker=ticker if ticker.startswith("0x") else ticker.upper())
    await message.answer("Дата раздачи по Киеву (ДД.ММ.ГГГГ ЧЧ:ММ):")
    await state.set_state(AddDrop.waiting_for_date)

@dp.message(AddDrop.waiting_for_date)
async def add_date(message: types.Message, state: FSMContext):
    await state.update_data(date=message.text.strip())
    await message.answer("Количество токенов:")
    await state.set_state(AddDrop.waiting_for_amount)

@dp.message(AddDrop.waiting_for_amount)
async def add_amount(message: types.Message, state: FSMContext):
    amount_str = message.text.replace(",", ".")
    await state.update_data(amount=amount_str)
    data = await state.get_data()
    
    await message.answer("⏳ Проверяю график...")
    try:
        ts = parse_kyiv_time(data['date'])
        m = get_kline_data(data['ticker'], ts)
        
        if m and "error" not in m:
            add_drop_to_sheet(data['ticker'], data['date'], amount_str, m['low'], m['high'])
            await message.answer(f"✅ Добавлено! Цена найдена: ${m['low']:.4f} - ${m['high']:.4f}")
            await state.clear()
        else:
            await message.answer(f"⚠️ {m['error'] if m else 'Ошибка'}. Введи Low и High через пробел:")
            await state.set_state(AddDrop.waiting_for_manual_price)
    except:
        await message.answer("Ошибка в дате. Попробуй заново.")
        await state.clear()

@dp.message(AddDrop.waiting_for_manual_price)
async def add_manual(message: types.Message, state: FSMContext):
    p = message.text.replace(",", ".").split()
    if len(p) != 2:
        await message.answer("Нужно 2 числа!")
        return
    data = await state.get_data()
    ticker = data['ticker']
    if ticker.startswith("0x"): ticker = f"{ticker[:6]}...{ticker[-4:]}"
    
    add_drop_to_sheet(ticker, data['date'], data['amount'], p[0], p[1])
    await message.answer(f"✅ Сохранено в таблицу с вашими ценами.")
    await state.clear()

@dp.message(F.text == "🎁 Раздачи")
async def show_drops(message: types.Message):
    await message.answer("Что показать?", reply_markup=get_drops_menu())

@dp.callback_query(F.data.startswith("drops_"))
async def process_drops(callback: types.CallbackQuery):
    _, f_type, val = callback.data.split("_")
    data = get_all_drops()
    if not data:
        await callback.message.answer("Таблица пуста.")
        return
    
    data.sort(key=lambda x: datetime.strptime(str(x['Date']), "%d.%m.%Y %H:%M"), reverse=True)
    filtered = data[:int(val)] if f_type == "count" else [d for d in data if datetime.strptime(str(d['Date']), "%d.%m.%Y %H:%M") >= datetime.now() - timedelta(days=int(val))]

    res = "📊 <b>Отчет по раздачам:</b>\n\n"
    for d in filtered:
        res += (f"🪙 <b>{d['Ticker']}</b> ({d['Date']})\n"
                f"Насыпали: {d['Amount']} шт.\n"
                f"Профит: <b>{d.get('Profit Range', 'Нет данных')}</b>\n"
                f"------------------\n")
    await callback.message.edit_text(res, parse_mode="HTML")

@dp.message(F.text == "🔍 Поиск цены")
async def search_p(message: types.Message, state: FSMContext):
    await message.answer("Тикер (BTC):")
    await state.set_state(PriceSearch.waiting_for_ticker)

@dp.message(PriceSearch.waiting_for_ticker)
async def search_t(message: types.Message, state: FSMContext):
    await state.update_data(t=message.text.upper())
    await message.answer("Время Киев (ДД.ММ.ГГГГ ЧЧ:ММ):")
    await state.set_state(PriceSearch.waiting_for_date)

@dp.message(PriceSearch.waiting_for_date)
async def search_d(message: types.Message, state: FSMContext):
    d = await state.get_data()
    try:
        m = get_kline_data(d['t'], parse_kyiv_time(message.text))
        if m and "error" not in m:
            await message.answer(f"📊 <b>{d['t']}</b>\nLow: ${m['low']}\nHigh: ${m['high']}\nOpen: ${m['open']}\nClose: ${m['close']}", parse_mode="HTML")
        else:
            await message.answer(f"Ошибка: {m['error']}")
    except:
        await message.answer("Ошибка даты.")
    await state.clear()

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
