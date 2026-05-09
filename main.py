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

# --- АВТОМАТИЧЕСКИЙ ПОИСК ЦЕН (BINANCE SPOT + WEB3 DEX) ---
def get_dex_kline_data(contract_address: str, timestamp_ms: int):
    """Ищет цену смарт-контракта на DEX через GeckoTerminal"""
    try:
        # 1. Ищем пул ликвидности по адресу контракта
        search_url = f"https://api.geckoterminal.com/api/v2/search/pools?query={contract_address}"
        search_resp = requests.get(search_url).json()
        
        if not search_resp.get("data"):
            return {"error": "Пул ликвидности не найден на DEX"}
            
        pool_id = search_resp["data"][0]["id"] 
        network, pool_address = pool_id.split("_", 1)
        
        # 2. Запрашиваем минутную свечу
        ts_sec = timestamp_ms // 1000
        ohlcv_url = f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool_address}/ohlcv/minute"
        params = {
            "aggregate": 1,
            "before_timestamp": ts_sec + 60, 
            "limit": 1
        }
        
        ohlcv_resp = requests.get(ohlcv_url, params=params).json()
        
        if not ohlcv_resp.get("data") or not ohlcv_resp["data"]["attributes"]["ohlcv_list"]:
            return {"error": "DEX не хранит поминутную историю за эту дату"}
            
        candle = ohlcv_resp["data"]["attributes"]["ohlcv_list"][0]
        
        return {
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4])
        }
    except Exception as e:
        return {"error": f"Сбой DEX API: {e}"}

def get_kline_data(symbol: str, timestamp_ms: int):
    """Определяет, куда идти: на Binance или на DEX"""
    if symbol.startswith("0x"):
        return get_dex_kline_data(symbol, timestamp_ms)
        
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
            return {"error": "Свеча не найдена на Binance"}
        return {
            "open": float(data[0][1]), "high": float(data[0][2]), 
            "low": float(data[0][3]), "close": float(data[0][4])
        }
    except:
        return {"error": "Ошибка API Binance"}

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
    await message.answer("Бот Binance Alpha/Web3 готов. Все данные пишутся в Google Sheets.", reply_markup=main_kb)

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
    
    await message.answer("⏳ Ищу график в блокчейне/на бирже...")
    try:
        ts = parse_kyiv_time(data['date'])
        m = get_kline_data(data['ticker'], ts)
        
        if m and "error" not in m:
            add_drop_to_sheet(data['ticker'], data['date'], amount_str, m['low'], m['high'])
            await message.answer(f"✅ Добавлено! Бот сам нашел цены:\nLow: <b>${m['low']:.4f}</b> | High: <b>${m['high']:.4f}</b>", parse_mode="HTML")
            await state.clear()
        else:
            await message.answer(f"⚠️ <b>{m['error'] if m else 'График не найден'}</b>\n\nПожалуйста, введи Low и High вручную через пробел (например: 0.20 0.55):", parse_mode="HTML")
            await state.set_state(AddDrop.waiting_for_manual_price)
    except Exception as e:
        await message.answer(f"❌ Ошибка в дате или запросе: {e}. Попробуй заново.")
        await state.clear()

@dp.message(AddDrop.waiting_for_manual_price)
async def add_manual(message: types.Message, state: FSMContext):
    p = message.text.replace(",", ".").split()
    if len(p) != 2:
        await message.answer("❌ Нужно ровно 2 числа (Low и High) через пробел!")
        return
    data = await state.get_data()
    ticker = data['ticker']
    if ticker.startswith("0x"): 
        ticker = f"{ticker[:6]}...{ticker[-4:]}"
    
    try:
        add_drop_to_sheet(ticker, data['date'], data['amount'], p[0], p[1])
        await message.answer(f"✅ Успешно сохранено в таблицу с вашими ценами!")
    except Exception as e:
        await message.answer(f"❌ Ошибка записи: {e}")
    await state.clear()

@dp.message(F.text == "🎁 Раздачи")
async def show_drops(message: types.Message):
    await message.answer("Что показать?", reply_markup=get_drops_menu())

@dp.callback_query(F.data.startswith("drops_"))
async def process_drops(callback: types.CallbackQuery):
    _, f_type, val = callback.data.split("_")
    data = get_all_drops()
    if not data:
        await callback.message.edit_text("Таблица пуста.")
        return
    
    try:
        data.sort(key=lambda x: datetime.strptime(str(x['Date']), "%d.%m.%Y %H:%M"), reverse=True)
        filtered = data[:int(val)] if f_type == "count" else [d for d in data if datetime.strptime(str(d['Date']), "%d.%m.%Y %H:%M") >= datetime.now() - timedelta(days=int(val))]

        if not filtered:
            await callback.message.edit_text("За этот период ничего нет.")
            return

        res = "📊 <b>Отчет Alpha / Launchpool:</b>\n\n"
        for d in filtered:
            res += (f"🪙 <b>{d['Ticker']}</b> ({d['Date']})\n"
                    f"Насыпали: {d['Amount']} шт.\n"
                    f"Профит: <b>{d.get('Profit Range', 'Нет данных')}</b>\n"
                    f"------------------\n")
        await callback.message.edit_text(res, parse_mode="HTML")
    except Exception as e:
        await callback.message.edit_text(f"Ошибка чтения данных: {e}\nУбедитесь, что в таблице 6 колонок.")

@dp.message(F.text == "🔍 Поиск цены")
async def search_p(message: types.Message, state: FSMContext):
    await message.answer("Тикер или смарт-контракт:")
    await state.set_state(PriceSearch.waiting_for_ticker)

@dp.message(PriceSearch.waiting_for_ticker)
async def search_t(message: types.Message, state: FSMContext):
    t = message.text.strip()
    await state.update_data(t=t if t.startswith("0x") else t.upper())
    await message.answer("Время Киев (ДД.ММ.ГГГГ ЧЧ:ММ):")
    await state.set_state(PriceSearch.waiting_for_date)

@dp.message(PriceSearch.waiting_for_date)
async def search_d(message: types.Message, state: FSMContext):
    d = await state.get_data()
    try:
        m = get_kline_data(d['t'], parse_kyiv_time(message.text))
        if m and "error" not in m:
            ticker_display = f"{d['t'][:6]}...{d['t'][-4:]}" if d['t'].startswith("0x") else f"{d['t']}USDT"
            await message.answer(
                f"📊 <b>{ticker_display}</b>\n"
                f"Low: ${m['low']:.4f}\n"
                f"High: ${m['high']:.4f}\n"
                f"Open: ${m['open']:.4f}\n"
                f"Close: ${m['close']:.4f}", 
                parse_mode="HTML"
            )
        else:
            await message.answer(f"❌ Ошибка: {m['error']}")
    except:
        await message.answer("❌ Ошибка даты.")
    await state.clear()

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
