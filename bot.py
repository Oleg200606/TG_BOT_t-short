import asyncio
import os
import shutil
from typing import List, Dict
from datetime import datetime
import logging
import sys
from pathlib import Path
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv
from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, joinedload, Session
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Локальные импорты
from database import get_db, init_db
from models import User, Category, Product, CartItem, Order, OrderItem
from repositories import (
    UserRepository, CategoryRepository, ProductRepository,
    CartRepository, OrderRepository
)
from admin_panel_v_2 import register_admin_panel, register_support 

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_IDS = [int(x.strip()) for x in os.getenv("ADMIN_CHAT_IDS", "").split(",") if x.strip()]
if not BOT_TOKEN:
    raise RuntimeError("Не указан BOT_TOKEN в .env")

# Создаем папку для изображений
IMAGES_DIR = Path("product_images")
IMAGES_DIR.mkdir(exist_ok=True)

# Инициализация базы данных
init_db()


# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

class OrderFSM(StatesGroup):
    waiting_fullname = State()
    waiting_phone = State()
    waiting_delivery_type = State()
    waiting_cdek_city = State()
    waiting_cdek_pvz = State()
    waiting_address = State()
    confirm = State()



def main_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="📸 Каталог")
    kb.button(text="🛒 Корзина")
    kb.button(text="🧾 Оформить заказ")
    kb.button(text="❓ Помощь")
    kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True)



def categories_ikb() -> InlineKeyboardMarkup:
    db = next(get_db())
    try:
        categories = CategoryRepository.get_all_active(db)

        ib = InlineKeyboardBuilder()
        for category in categories:
            ib.button(text=category.title, callback_data=f"cat:{category.key}")
        ib.adjust(1)
        return ib.as_markup()
    finally:
        db.close()

def category_products_ikb(cat_key: str) -> InlineKeyboardMarkup:
    db = next(get_db())
    try:
        category = CategoryRepository.get_by_key(db, cat_key)
        if not category:
            return InlineKeyboardMarkup(inline_keyboard=[])

        products = ProductRepository.get_by_category(db, category.id)

        ib = InlineKeyboardBuilder()
        for product in products:
            ib.button(text=f"{product.name} — {product.price} ₽", callback_data=f"prod:{product.id}")
        ib.button(text="⬅️ Назад к категориям", callback_data="back:cats")
        ib.adjust(1)
        return ib.as_markup()
    finally:
        db.close()

def product_sizes_ikb(product_id: int) -> InlineKeyboardMarkup:
    db = next(get_db())
    try:
        product = ProductRepository.get_by_id(db, product_id)
        if not product:
            return InlineKeyboardMarkup(inline_keyboard=[])

        category_key = product.category.key

        ib = InlineKeyboardBuilder()
        for size in product.sizes:
            ib.button(text=size, callback_data=f"size:{product.id}:{size}")
        ib.button(text="⬅️ Назад к товарам", callback_data=f"back:cat:{category_key}")
        ib.adjust(4, 1)
        return ib.as_markup()
    finally:
        db.close()

def qty_ikb(product_id: int, size: str) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    for q in [1, 2, 3, 4, 5]:
        ib.button(text=str(q), callback_data=f"qty:{product_id}:{size}:{q}")
    ib.button(text="⬅️ Назад к размерам", callback_data=f"back:size:{product_id}")
    ib.adjust(5, 1)
    return ib.as_markup()

def checkout_delivery_ikb() -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="📦 CDEK (ПВЗ)", callback_data="delivery:cdek")
    ib.button(text="🚚 Курьер до двери", callback_data="delivery:courier")
    ib.adjust(1)
    return ib.as_markup()

def confirm_ikb() -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="✅ Подтвердить заказ", callback_data="confirm:yes")
    ib.button(text="✏️ Изменить адрес", callback_data="confirm:edit_address")
    ib.button(text="❌ Отмена", callback_data="confirm:cancel")
    ib.adjust(1)
    return ib.as_markup()

def format_cart(user_id: int) -> str:
    db = next(get_db())
    try:
        user = db.query(User).filter(User.telegram_id == user_id).first()
        if not user:
            return "Пользователь не найден."

        cart_items = db.query(CartItem).filter(CartItem.user_id == user.id).all()

        if not cart_items:
            return "Ваша корзина пуста."

        lines = ["🛒 *Корзина:*"]
        total = 0

        for item in cart_items:
            product = db.query(Product).filter(Product.id == item.product_id).first()
            if product:
                line_total = product.price * item.quantity
                lines.append(f"• {product.name} — {item.size} × {item.quantity} = *{line_total} ₽*")
                total += line_total

        lines.append(f"\nИтого: *{total} ₽*")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error formatting cart: {e}")
        return "Ошибка при загрузке корзины."
    finally:
        db.close()

async def notify_admins(bot: Bot, order: Order):
    text_lines = [
        "🆕 *Новый заказ!*",
        f"Номер заказа: {order.order_number}",
        f"Дата/время: {order.created_at.strftime('%Y-%m-%d %H:%M')}",
        f"Покупатель: {order.fullname}",
        f"Телефон: {order.phone}",
        "",
        "Товары:"
    ]

    db = next(get_db())
    try:
        for item in order.items:
            text_lines.append(f"• {item.product_name} — {item.size} × {item.quantity} = {item.total} ₽")
    finally:
        db.close()

    text_lines.append(f"\nИтого: *{order.total_amount} ₽*")
    text_lines.append("")
    text_lines.append(f"Доставка: {order.delivery_type}")

    if order.delivery_type == "cdek":
        delivery_data = order.delivery_address
        text_lines.append(f"Город (CDEK): {delivery_data.get('city')}")
        text_lines.append(f"ПВЗ: {delivery_data.get('pvz')}")
    else:
        delivery_data = order.delivery_address
        text_lines.append(f"Адрес: {delivery_data.get('address')}")

    text_lines.append(f"\nUser ID: {order.user.telegram_id}")
    payload = "\n".join(text_lines)
    for chat_id in ADMIN_CHAT_IDS:
        try:
            await bot.send_message(chat_id, payload, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления админу {chat_id}: {e}")

dp = Dispatcher(storage=MemoryStorage())
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))


register_admin_panel(dp, bot)
register_support(dp, bot)

@dp.message(CommandStart())
async def on_start(message: Message):
    logger.info(f"User {message.from_user.id} started bot")

    db = next(get_db())
    try:
        user = UserRepository.get_or_create_user(
            db,
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name
        )
        
        # Автоматически сделать администратором, если ID в списке
        if message.from_user.id in ADMIN_CHAT_IDS and not user.is_admin:
            user.is_admin = True
            db.commit()
            logger.info(f"User {message.from_user.id} set as admin")
            
    finally:
        db.close()

    await message.answer(
        "Привет! Это бот для заказов одежды Эмперадор.\n\nВыбери действие:",
        reply_markup=main_menu_kb()
    )



@dp.message(Command("help"))
@dp.message(F.text == "❓ Помощь")
async def on_help(message: Message):
    logger.info(f"User {message.from_user.id} requested help")
    await message.answer(
        "Доступные команды:\n"
        "• /start — главное меню\n"
        "• /catalog — открыть каталог\n"
        "• /cart — показать корзину\n"
        "• /checkout — оформить заказ\n"
        "• /cancel — отменить текущее действие\n"
        "• /admin — панель администратора\n"
    )

@dp.message(Command("catalog"))
@dp.message(F.text == "📸 Каталог")
async def on_catalog(message: Message):
    await message.answer("Выберите категорию:", reply_markup=categories_ikb())

@dp.callback_query(F.data.startswith("cat:"))
async def on_category_select(cb: CallbackQuery):
    try:
        category_key = cb.data.split(":")[1]
        await cb.message.answer(f"Товары категории:", reply_markup=category_products_ikb(category_key))
        await cb.answer()
    except Exception as e:
        logger.error(f"Error in on_category_select: {e}")
        await cb.answer("Произошла ошибка при выборе категории.")

@dp.callback_query(F.data.startswith("prod:"))
async def on_product_select(cb: CallbackQuery):
    product_id = int(cb.data.split(":")[1])

    db = next(get_db())
    try:
        product = ProductRepository.get_by_id(db, int(product_id))
        if not product:
            await cb.answer("Товар не найден")
            return

        description = [
            f"*{product.name}*",
            f"Цена: {product.price} ₽",
            f"Описание: {product.description}",
            f"Доступные размеры: {', '.join(product.sizes)}",
            "",
            "Выберите размер:"
        ]

        if product.images:
            with open(product.images[0], 'rb') as photo:
                await cb.message.answer_photo(
                    photo=photo,
                    caption="\n".join(description),
                    reply_markup=product_sizes_ikb(product.id),
                    parse_mode="Markdown"
                )
        else:
            await cb.message.answer(
                "\n".join(description),
                reply_markup=product_sizes_ikb(product.id),
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error(f"Error showing product: {e}")
        await cb.answer("Ошибка при загрузке товара")
    finally:
        db.close()

    await cb.answer()

@dp.callback_query(F.data.startswith("size:"))
async def on_size_select(cb: CallbackQuery):
    _, product_id, size = cb.data.split(":")

    db = next(get_db())
    try:
        product = ProductRepository.get_by_id(db, int(product_id))
        if not product:
            await cb.answer("Товар не найден")
            return

        category_key = product.category.key

    finally:
        db.close()

    await cb.message.answer(f"Выберите количество для размера {size}:",
                           reply_markup=qty_ikb(int(product_id), size))
    await cb.answer()

@dp.callback_query(F.data.startswith("qty:"))
async def on_qty(cb: CallbackQuery):
    _, product_id, size, qty_str = cb.data.split(":")
    qty = int(qty_str)

    db = next(get_db())
    try:
        product = ProductRepository.get_by_id(db, int(product_id))
        if not product:
            await cb.answer("Товар не найден")
            return

        user = UserRepository.get_or_create_user(
            db,
            cb.from_user.id,
            cb.from_user.username,
            cb.from_user.first_name,
            cb.from_user.last_name
        )

        CartRepository.add_to_cart(db, user.id, product.id, size, qty)

        product_name = product.name
        product_price = product.price

    except Exception as e:
        logger.error(f"Error adding to cart: {e}")
        await cb.answer("Ошибка при добавлении в корзину")
        return
    finally:
        db.close()

    cart_text = format_cart(cb.from_user.id)

    await cb.message.answer(
        f"Добавлено: {product_name} — {size} × {qty} = *{product_price * qty} ₽*\n\n{cart_text}",
        reply_markup=main_menu_kb()
    )
    await cb.answer("В корзине!")

@dp.message(Command("cart"))
@dp.message(F.text == "🛒 Корзина")
async def on_cart(message: Message):
    await message.answer(format_cart(message.from_user.id))

@dp.message(Command("checkout"))
@dp.message(F.text == "🧾 Оформить заказ")
async def on_checkout(message: Message, state: FSMContext):
    cart_text = format_cart(message.from_user.id)
    if "пуста" in cart_text:
        await message.answer("Ваша корзина пуста. Добавьте товары перед оформлением заказа.")
        return

    await message.answer("Введите ваше ФИО:")
    await state.set_state(OrderFSM.waiting_fullname)

@dp.message(OrderFSM.waiting_fullname)
async def on_fullname(message: Message, state: FSMContext):
    await state.update_data(fullname=message.text)
    await message.answer("Введите ваш телефон:")
    await state.set_state(OrderFSM.waiting_phone)

@dp.message(OrderFSM.waiting_phone)
async def on_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await message.answer("Выберите способ доставки:", reply_markup=checkout_delivery_ikb())
    await state.set_state(OrderFSM.waiting_delivery_type)

@dp.callback_query(OrderFSM.waiting_delivery_type, F.data.startswith("delivery:"))
async def on_delivery_type(cb: CallbackQuery, state: FSMContext):
    delivery_type = cb.data.split(":")[1]
    await state.update_data(delivery_type=delivery_type)

    if delivery_type == "cdek":
        await cb.message.answer("Введите ваш город для поиска ПВЗ CDEK:")
        await state.set_state(OrderFSM.waiting_cdek_city)
    else:
        await cb.message.answer("Введите адрес доставки:")
        await state.set_state(OrderFSM.waiting_address)

    await cb.answer()

@dp.message(OrderFSM.waiting_cdek_city)
async def on_cdek_city(message: Message, state: FSMContext):
    await state.update_data(cdek_city=message.text)
    await message.answer("Введите номер ПВЗ CDEK:")
    await state.set_state(OrderFSM.waiting_cdek_pvz)

@dp.message(OrderFSM.waiting_cdek_pvz)
async def on_cdek_pvz(message: Message, state: FSMContext):
    await state.update_data(cdek_pvz=message.text)

    data = await state.get_data()
    delivery_info = f"Город: {data['cdek_city']}, ПВЗ: {data['cdek_pvz']}"

    await message.answer(
        f"Проверьте данные доставки:\n{delivery_info}\n\nПодтвердить заказ?",
        reply_markup=confirm_ikb()
    )
    await state.set_state(OrderFSM.confirm)

@dp.message(OrderFSM.waiting_address)
async def on_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text)

    data = await state.get_data()
    await message.answer(
        f"Проверьте адрес доставки:\n{data['address']}\n\nПодтвердить заказ?",
        reply_markup=confirm_ikb()
    )
    await state.set_state(OrderFSM.confirm)

@dp.callback_query(OrderFSM.confirm, F.data == "confirm:yes")
async def confirm_order(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    data = await state.get_data()

    db = next(get_db())
    try:
        user = UserRepository.get_or_create_user(
            db,
            cb.from_user.id,
            cb.from_user.username,
            cb.from_user.first_name,
            cb.from_user.last_name
        )

        cart_items = CartRepository.get_user_cart(db, user.id)
        if not cart_items:
            await state.clear()
            await cb.message.answer("Корзина пуста. Нечего подтверждать.", reply_markup=main_menu_kb())
            await cb.answer()
            return

        delivery_data = {}
        if data.get("delivery_type") == "cdek":
            delivery_data = {
                "city": data.get("cdek_city"),
                "pvz": data.get("cdek_pvz")
            }
        else:
            delivery_data = {
                "address": data.get("address")
            }

        order = OrderRepository.create_order(
            db, user.id, cart_items,
            data.get("fullname"), data.get("phone"),
            data.get("delivery_type"), delivery_data
        )

        await notify_admins(cb.message.bot, order)
    finally:
        db.close()

    await state.clear()
    await cb.message.answer(
        "✅ Заказ принят! Мы свяжемся с вами для подтверждения деталей. Спасибо!",
        reply_markup=main_menu_kb()
    )
    await cb.answer()


@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return

    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu_kb())

# Добавляем обработчики для навигации назад
@dp.callback_query(F.data.startswith("back:"))
async def on_back(cb: CallbackQuery):
    back_type = cb.data.split(":")[1]

    if back_type == "cats":
        await cb.message.answer("Выберите категорию:", reply_markup=categories_ikb())
    elif back_type == "cat":
        category_key = cb.data.split(":")[2]
        await cb.message.answer(f"Товары категории:", reply_markup=category_products_ikb(category_key))
    elif back_type == "size":
        product_id = int(cb.data.split(":")[2])
        await cb.message.answer("Выберите размер:", reply_markup=product_sizes_ikb(product_id))

    await cb.answer()

# Добавляем обработчик для кнопки "Главное меню" в админке
@dp.message(F.text == "👤 Главное меню")
async def back_to_main_menu(message: Message):
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@dp.message(Command("check_db"))
async def check_db(message: Message):
    db = next(get_db())
    try:
        admin_count = db.query(User).filter(User.is_admin == True).count()
        await message.answer(f"Администраторов в базе: {admin_count}")
    finally:
        db.close()

async def main():
    logger.info("Bot starting...")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Bot stopped with error: {e}")
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped")
