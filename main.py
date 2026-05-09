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
    # Записываем как строку, чтобы Google не ломал формат
    sheet.append_row([str(ticker), str(date), str(amount)])

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
        
        # Если Binance возвращает ошибку (например, неверный тикер)
        if isinstance(data, dict) and "code" in data:
            return {"error": f"Binance: {data.get('msg')}"}
            
        # Если данных нет (свеча еще не появилась)
        if not data or len(data) == 0: 
            return {"error": "Нет торгов в эту минуту (свеча не найдена)"}
            
        return {
            "open": float(data[0][1]), 
            "high": float(data[0][2]), 
            "low": float(data[0][3]),
            "close": float(data[0][4])
        }
    except Exception as e:
        return {"error": f"Сбой запроса: {e}"}

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
    await message.answer("Сколько токенов насыпали? (можно с точкой или запятой)")
    await state.set_state(AddDrop.waiting_for_amount)

@dp.message(AddDrop.waiting_for_amount)
async def add_amount(message: types.Message, state: FSMContext):
    data = await state.get_data()
    # Заменяем запятую на точку, если ввели случайно с запятой
    amount_str = message.text.replace(",", ".")
    try:
        add_drop_to_sheet(data['ticker'], data['date'], amount_str)
        await message.answer(f"✅ <b>{data['ticker']}</b> успешно добавлен в таблицу!", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка записи в таблицу: {e}")
    await state.clear()

@dp.message(F.text == "🎁 Раздачи")
async def show_menu(message: types.Message):
    await message.answer("Выберите количество или период:", reply_markup=get_drops_menu())

@dp.callback_query(F.data.startswith("drops_"))
async def process_drops(callback: types.CallbackQuery):
    await callback.message.edit_text("⏳ Запрашиваю данные из таблицы и Binance...")
    _, f_type, val = callback.data.split("_")
    
    try:
        all_drops = get_all_drops()
        
        # Защита от пустой таблицы или неправильных заголовков
        if not all_drops:
            await callback.message.edit_text("Таблица пуста или бот не видит заголовки (Ticker, Date, Amount).")
            return

        all_drops.sort(key=lambda x: datetime.strptime(str(x['Date']), "%d.%m.%Y %H:%M"), reverse=True)
        
        if f_type == "count":
            filtered = all_drops[:int(val)]
        else:
            limit = datetime.now() - timedelta(days=int(val))
            filtered = [d for d in all_drops if datetime.strptime(str(d['Date']), "%d.%m.%Y %H:%M") >= limit]

        if not filtered:
            await callback.message.edit_text("В базе пока нет подходящих раздач за этот период.")
            return

        res = "📊 <b>Отчет по раздачам:</b>\n\n"
        for d in filtered:
            try:
                ts = parse_kyiv_time(str(d['Date']))
                m = get_kline_data(str(d['Ticker']), ts)
                
                amount_val = float(str(d['Amount']).replace(',', '.'))
                
                # Если бинанс вернул данные без ошибки
                if m and "error" not in m:
                    res += (f"🪙 <b>{d['Ticker']}</b> ({d['Date']})\n"
                            f"Раздали: {amount_val} шт.\n"
                            f"Цена: L <b>${m['low']:.4f}</b> | H <b>${m['high']:.4f}</b>\n"
                            f"Профит: <b>${amount_val*m['low']:.2f} — ${amount_val*m['high']:.2f}</b>\n"
                            f"------------------\n")
                else:
                    # Если произошла ошибка с Binance, выводим причину
                    err_msg = m["error"] if m else "Неизвестная ошибка"
                    res += (f"🪙 <b>{d['Ticker']}</b> ({d['Date']})\n"
                            f"❌ <i>{err_msg}</i>\n"
                            f"------------------\n")
            except Exception as e:
                res += (f"🪙 <b>{d.get('Ticker', 'Неизвестно')}</b>\n❌ Ошибка данных: {e}\n------------------\n")
        
        await callback.message.edit_text(res, parse_mode="HTML")
    except Exception as e:
        await callback.message.edit_text(f"Критическая ошибка: {e}")

@dp.message(F.text == "🔍 Поиск цены")
async def start_price_search(message: types.Message, state: FSMContext):
    await message.answer("Введите тикер (напр. BTC, NOT):")
    await state.set_state(PriceSearch.waiting_for_ticker)

@dp.message(PriceSearch.waiting_for_ticker)
async def process_p_ticker(message: types.Message, state: FSMContext):
    await state.update_data(ticker=message.text.upper())
    await message.answer("Введите время по Киеву (ДД.ММ.ГГГГ ЧЧ:ММ):")
    await state.set_state(PriceSearch.waiting_for_date)

@dp.message(PriceSearch.waiting_for_date)
async def process_p_date(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    ticker = user_data['ticker']
    date_str = message.text
    
    try:
        ts = parse_kyiv_time(date_str)
        m = get_kline_data(ticker, ts)
        
        if m and "error" not in m:
            text = (f"🔍 <b>{ticker}USDT</b>\n"
                    f"🕒 {date_str} (Киев)\n\n"
                    f"🟢 Открытие: <b>${m['open']:.4f}</b>\n"
                    f"🔴 Закрытие: <b>${m['close']:.4f}</b>\n"
                    f"📈 Максимум: <b>${m['high']:.4f}</b>\n"
                    f"📉 Минимум: <b>${m['low']:.4f}</b>")
            await message.answer(text, parse_mode="HTML")
        else:
            err_msg = m["error"] if m else "Неизвестная ошибка"
            await message.answer(f"❌ <b>Данные не найдены:</b>\n{err_msg}", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    
    await state.clear()

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
