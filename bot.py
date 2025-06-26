import asyncio
import logging
import sqlite3
import requests
from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from datetime import datetime, timedelta
import time
import config

# Настройка логов
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Подключение к БД
conn = sqlite3.connect('wb_price_monitor.db')
cursor = conn.cursor()

# Создание таблиц
cursor.execute('''
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    article INTEGER NOT NULL,
    name TEXT,
    current_price REAL,
    last_update DATETIME,
    UNIQUE(user_id, article)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    price REAL,
    change_date DATETIME,
    FOREIGN KEY(product_id) REFERENCES products(id)
''')
conn.commit()

class ProductState(StatesGroup):
    waiting_for_article = State()

def get_wb_product_info(article):
    """Получение информации о товаре с Wildberries по артикулу"""
    url = f"https://card.wb.ru/cards/detail?nm={article}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if not data.get('data', {}).get('products'):
            return None, None, None
        
        product = data['data']['products'][0]
        name = product.get('name')
        price = product.get('salePriceU')
        
        # Цена в API приходит в копейках, переводим в рубли
        if price:
            price = price / 100
        
        return name, price, True
    except Exception as e:
        logger.error(f"Ошибка при получении данных: {e}")
        return None, None, False

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("➕ Добавить товар", callback_data="add_product"))
    keyboard.add(InlineKeyboardButton("🗑️ Удалить товар", callback_data="remove_product"))
    keyboard.add(InlineKeyboardButton("📋 Список товаров", callback_data="list_products"))
    keyboard.add(InlineKeyboardButton("⚙️ Интервал проверки", callback_data="set_interval"))
    
    await message.answer(
        "🔔 Добро пожаловать в WB Price Guardian!\n\n"
        "Я отслеживаю изменения цен ваших товаров на Wildberries и мгновенно уведомляю о любых изменениях.\n\n"
        "Основные функции:\n"
        "• Автоматическая проверка цен каждые 30 минут\n"
        "• Мгновенные уведомления об изменениях\n"
        "• История изменений цен\n"
        "• Управление списком товаров\n\n"
        "Добавьте первый товар по его артикулу WB:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data == 'add_product')
async def process_add_product(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        callback_query.from_user.id,
        "✍️ Введите артикул товара (WB код):"
    )
    await ProductState.waiting_for_article.set()

@dp.message_handler(state=ProductState.waiting_for_article)
async def process_article(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    article = message.text.strip()
    
    # Проверка валидности артикула
    if not article.isdigit():
        await message.answer("❌ Артикул должен состоять только из цифр. Попробуйте еще раз.")
        return
    
    # Проверка существования товара
    name, price, success = get_wb_product_info(article)
    
    if not success:
        await message.answer("⚠️ Ошибка подключения к Wildberries. Попробуйте позже.")
        await state.finish()
        return
    
    if price is None:
        await message.answer("❌ Товар с таким артикулом не найден. Проверьте артикул.")
        await state.finish()
        return
    
    # Проверка, не добавлен ли уже товар
    cursor.execute("SELECT id FROM products WHERE user_id = ? AND article = ?", (user_id, article))
    if cursor.fetchone():
        await message.answer("ℹ️ Этот товар уже добавлен в ваш список отслеживания.")
        await state.finish()
        return
    
    # Добавление товара в БД
    current_time = datetime.now().isoformat()
    cursor.execute(
        "INSERT INTO products (user_id, article, name, current_price, last_update) VALUES (?, ?, ?, ?, ?)",
        (user_id, article, name, price, current_time)
    )
    product_id = cursor.lastrowid
    
    # Сохранение начальной цены в историю
    cursor.execute(
        "INSERT INTO price_history (product_id, price, change_date) VALUES (?, ?, ?)",
        (product_id, price, current_time)
    )
    conn.commit()
    
    await message.answer(
        f"✅ Товар успешно добавлен!\n\n"
        f"Название: {name}\n"
        f"Артикул: {article}\n"
        f"Текущая цена: {price} руб."
    )
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == 'list_products')
async def process_list_products(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    cursor.execute("SELECT article, name, current_price FROM products WHERE user_id = ?", (user_id,))
    products = cursor.fetchall()
    
    if not products:
        await bot.answer_callback_query(callback_query.id, "У вас нет добавленных товаров.")
        return
    
    response = "📋 Ваши товары:\n\n"
    for idx, (article, name, price) in enumerate(products, 1):
        response += f"{idx}. {name}\nАртикул: {article}\nЦена: {price} руб.\n\n"
    
    await bot.send_message(user_id, response)

@dp.callback_query_handler(lambda c: c.data == 'remove_product')
async def process_remove_product(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    cursor.execute("SELECT id, article, name FROM products WHERE user_id = ?", (user_id,))
    products = cursor.fetchall()
    
    if not products:
        await bot.answer_callback_query(callback_query.id, "У вас нет добавленных товаров.")
        return
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    for product_id, article, name in products:
        # Обрезаем длинное название для кнопки
        btn_text = f"{name[:20]}..." if len(name) > 20 else name
        keyboard.add(InlineKeyboardButton(
            f"{btn_text} ({article})",
            callback_data=f"remove_{product_id}"
        ))
    
    await bot.send_message(
        user_id,
        "Выберите товар для удаления:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('remove_'))
async def confirm_remove(callback_query: types.CallbackQuery):
    product_id = callback_query.data.split('_')[1]
    cursor.execute("SELECT name, article FROM products WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    
    if not product:
        await bot.answer_callback_query(callback_query.id, "Товар не найден.")
        return
    
    name, article = product
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("✅ Да", callback_data=f"confirm_remove_{product_id}"))
    keyboard.add(InlineKeyboardButton("❌ Нет", callback_data="cancel_remove"))
    
    await bot.send_message(
        callback_query.from_user.id,
        f"Вы уверены, что хотите удалить товар:\n{name} (арт. {article})?",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('confirm_remove_'))
async def remove_product(callback_query: types.CallbackQuery):
    product_id = callback_query.data.split('_')[2]
    
    cursor.execute("DELETE FROM products WHERE id = ?", (product_id,))
    cursor.execute("DELETE FROM price_history WHERE product_id = ?", (product_id,))
    conn.commit()
    
    await bot.answer_callback_query(callback_query.id, "Товар удален.")
    await bot.send_message(callback_query.from_user.id, "✅ Товар успешно удален из отслеживания.")

@dp.callback_query_handler(lambda c: c.data == 'cancel_remove')
async def cancel_remove(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id, "Отменено.")
    await bot.send_message(callback_query.from_user.id, "Удаление отменено.")

@dp.callback_query_handler(lambda c: c.data == 'set_interval')
async def set_check_interval(callback_query: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(row_width=3)
    intervals = [
        ("15 минут", 900),
        ("30 минут", 1800),
        ("1 час", 3600),
        ("2 часа", 7200),
        ("4 часа", 14400),
        ("6 часов", 21600)
    ]
    
    for text, seconds in intervals:
        keyboard.insert(InlineKeyboardButton(text, callback_data=f"interval_{seconds}"))
    
    await bot.send_message(
        callback_query.from_user.id,
        "⏱ Выберите интервал проверки цен:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('interval_'))
async def apply_interval(callback_query: types.CallbackQuery):
    global CHECK_INTERVAL
    seconds = int(callback_query.data.split('_')[1])
    CHECK_INTERVAL = seconds
    
    # Сохраняем настройку в БД
    user_id = callback_query.from_user.id
    cursor.execute("REPLACE INTO settings (user_id, check_interval) VALUES (?, ?)", (user_id, seconds))
    conn.commit()
    
    minutes = seconds // 60
    await bot.answer_callback_query(callback_query.id, f"Интервал изменен на {minutes} минут")
    await bot.send_message(user_id, f"✅ Интервал проверки установлен: {minutes} минут")

async def price_check_task():
    """Фоновая задача для проверки цен"""
    while True:
        logger.info(f"Начало проверки цен. Интервал: {CHECK_INTERVAL} сек")
        
        # Получаем все товары для отслеживания
        cursor.execute("SELECT id, user_id, article, name, current_price FROM products")
        products = cursor.fetchall()
        
        for product in products:
            product_id, user_id, article, name, old_price = product
            
            # Получаем актуальную цену
            _, new_price, success = get_wb_product_info(article)
            
            if not success:
                logger.error(f"Ошибка получения цены для товара {article}")
                continue
            
            if new_price is None:
                logger.warning(f"Товар {article} не найден, возможно, снят с продажи")
                continue
            
            current_time = datetime.now().isoformat()
            
            # Если цена изменилась
            if abs(new_price - old_price) > 0.01:  # Учитываем погрешность округления
                # Отправляем уведомление
                message = (
                    f"⚠️ <b>Изменение цены!</b>\n\n"
                    f"Товар: {name}\n"
                    f"Артикул: {article}\n\n"
                    f"Старая цена: <s>{old_price:.2f} руб.</s>\n"
                    f"Новая цена: <b>{new_price:.2f} руб.</b>\n\n"
                    f"Разница: {new_price - old_price:+.2f} руб."
                )
                
                try:
                    await bot.send_message(user_id, message, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Ошибка отправки сообщения пользователю {user_id}: {e}")
                
                # Обновляем цену в БД
                cursor.execute(
                    "UPDATE products SET current_price = ?, last_update = ? WHERE id = ?",
                    (new_price, current_time, product_id)
                
                # Сохраняем изменение в историю
                cursor.execute(
                    "INSERT INTO price_history (product_id, price, change_date) VALUES (?, ?, ?)",
                    (product_id, new_price, current_time))
                
                conn.commit()
        
        # Ожидаем заданный интервал
        await asyncio.sleep(CHECK_INTERVAL)

async def on_startup(dp):
    # Создаем таблицу настроек
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        user_id INTEGER PRIMARY KEY,
        check_interval INTEGER DEFAULT 1800
    )''')
    conn.commit()
    
    # Запускаем фоновую задачу проверки цен
    asyncio.create_task(price_check_task())

if __name__ == '__main__':
    # Стандартный интервал проверки (30 минут)
    CHECK_INTERVAL = 1800
    
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
