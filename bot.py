import asyncio
import os
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
from sqlalchemy.orm import Session
import json

# Локальные импорты
from database import get_db, init_db
from models import User, Category, Product, CartItem, Order, OrderItem
from repositories import (
    TicketRepository, UserRepository, CategoryRepository, ProductRepository,
    CartRepository, OrderRepository
)

# Импорт админских обработчиков
from admins_panel import mention_user, register_admin_panel, register_support, ADMIN_CHAT_IDS


load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("Не указан BOT_TOKEN в .env")

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

# Состояния для пользовательской части
class OrderFSM(StatesGroup):
    waiting_fullname = State()
    waiting_phone = State()
    waiting_delivery_type = State()
    waiting_cdek_city = State()
    waiting_cdek_pvz = State()
    waiting_address = State()
    confirm = State()

class SupportFSM(StatesGroup):
    waiting_message = State()

class CartEditFSM(StatesGroup):
    waiting_action = State()


# Утилиты для создания клавиатур (из пользовательской части)
def main_menu_kb(user_id: int=None) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="📸 Каталог")
    kb.button(text="🛒 Корзина")
    kb.button(text="🧾 Мои заказы")
    kb.button(text="❓ Техподдержка")
    # Добавляем кнопку админки для администраторов
    if user_id and user_id in ADMIN_CHAT_IDS:
        kb.button(text="👑 Админка")
    kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True)

def back_to_main_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="⬅️ Главное меню")
    return kb.as_markup(resize_keyboard=True)

def categories_ikb() -> InlineKeyboardMarkup:
    with next(get_db()) as db:
        categories = CategoryRepository.get_all_active(db)
        ib = InlineKeyboardBuilder()
        for category in categories:
            ib.button(text=category.title, callback_data=f"cat:{category.key}")
        ib.adjust(1)
        return ib.as_markup()

def category_products_ikb(cat_key: str) -> InlineKeyboardMarkup:
    with next(get_db()) as db:
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

def product_sizes_ikb(product_id: int) -> InlineKeyboardMarkup:
    with next(get_db()) as db:
        product = ProductRepository.get_by_id(db, product_id)
        if not product:
            return InlineKeyboardMarkup(inline_keyboard=[])

        ib = InlineKeyboardBuilder()
        for size in product.sizes:
            ib.button(text=size, callback_data=f"size:{product.id}:{size}")
        ib.button(text="⬅️ Назад к товарам", callback_data=f"back:cat:{product.category.key}")
        ib.adjust(4, 1)
        return ib.as_markup()

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
    ib.button(text="✏️ Изменить данные", callback_data="confirm:edit")
    ib.button(text="❌ Отменить заказ", callback_data="confirm:cancel")
    ib.adjust(1)
    return ib.as_markup()

def cart_actions_ikb() -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="🛒 Оформить заказ", callback_data="cart:checkout")
    ib.button(text="✏️ Редактировать корзину", callback_data="cart:edit")
    ib.button(text="🗑️ Очистить корзину", callback_data="cart:clear")
    ib.adjust(1)
    return ib.as_markup()

def cart_edit_ikb(cart_items) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    for item in cart_items:
        # Это объект CartItem с загруженным продуктом
        if hasattr(item, 'product') and item.product:
            ib.button(
                text=f"❌ {item.product.name} - {item.size} × {item.quantity}", 
                callback_data=f"remove:{item.id}"
            )
    ib.button(text="✅ Завершить редактирование", callback_data="cart:done")
    ib.adjust(1)
    return ib.as_markup()

def orders_list_ikb(orders: list) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    for order in orders:
        status_emoji = "🟡" if order.status == "pending" else "🟢" if order.status == "confirmed" else "🔴"
        ib.button(text=f"{status_emoji} Заказ #{order.order_number} - {order.total_amount}₽", 
                 callback_data=f"order:{order.id}")
    ib.button(text="⬅️ Главное меню", callback_data="back:main")
    ib.adjust(1)
    return ib.as_markup()

def order_actions_ikb(order_id: int, status: str) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    if status in ["pending", "confirmed"]:
        ib.button(text="❌ Отменить заказ", callback_data=f"order_cancel:{order_id}")
    ib.button(text="⬅️ Назад к заказам", callback_data="orders:list")
    ib.adjust(1)
    return ib.as_markup()

def format_cart(user_id: int) -> str:
    with next(get_db()) as db:
        user = db.query(User).filter(User.telegram_id == user_id).first()
        if not user:
            return "Пользователь не найден."

        # Используем метод репозитория для получения корзины с продуктами
        cart_items = CartRepository.get_user_cart(db, user.id)

        if not cart_items:
            return "🛒 *Ваша корзина пуста*"

        lines = ["🛒 *Ваша корзина:*"]
        total = 0

        for item in cart_items:
            # Теперь product уже загружен благодаря joinedload
            if hasattr(item, 'product') and item.product:
                line_total = item.product.price * item.quantity
                lines.append(f"• {item.product.name} — {item.size} × {item.quantity} = *{line_total} ₽*")
                total += line_total

        lines.append(f"\n💰 *Итого: {total} ₽*")
        return "\n".join(lines)

def format_order(order: Order) -> str:
    order_text = [
        f"🧾 *Заказ #{order.order_number}*",
        f"📊 Статус: {order.status}",
        f"📅 Дата: {order.created_at.strftime('%d.%m.%Y %H:%M')}",
        f"💳 Сумма: {order.total_amount} ₽",
        f"🚚 Доставка: {order.delivery_type}",
        "",
        "*📦 Товары:*"
    ]
    
    for item in order.items:
        order_text.append(f"• {item.product_name} - {item.size} × {item.quantity} = {item.total} ₽")
        
    if order.delivery_type == "cdek":
        delivery_data = order.delivery_address
        order_text.append(f"\n*📍 Доставка CDEK:*")
        order_text.append(f"Город: {delivery_data.get('city', 'Не указан')}")
        order_text.append(f"ПВЗ: {delivery_data.get('pvz', 'Не указан')}")
    else:
        delivery_data = order.delivery_address
        order_text.append(f"\n*🏠 Адрес доставки:*")
        order_text.append(f"{delivery_data.get('address', 'Не указан')}")
        
    return "\n".join(order_text)

dp = Dispatcher(storage=MemoryStorage())
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))

# Регистрируем админские обработчики
register_admin_panel(dp, bot)
register_support(dp, bot)

# Обработчики команд
@dp.message(CommandStart())
async def on_start(message: Message):
    logger.info(f"User {message.from_user.id} started bot")

    with next(get_db()) as db:
        UserRepository.get_or_create_user(
            db,
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name
        )

    await message.answer(
        "👋 Привет! Добро пожаловать в магазин одежды Эмперадор!\n\n"
        "Здесь вы можете:\n"
        "• 🛍️ Просматривать товары с фото\n"
        "• 🛒 Добавлять товары в корзину\n"
        "• ✏️ Редактировать корзину\n"
        "• 🚚 Оформлять заказы с доставкой\n"
        "• 📦 Отслеживать свои заказы\n"
        "• ❓ Обращаться в техподдержку\n\n"
        "Выберите действие:",
        reply_markup=main_menu_kb()
    )

@dp.message(Command("help"))
async def on_help(message: Message):
    help_text = [
        "ℹ️ *Доступные команды:*",
        "• /start — главное меню",
        "• /catalog — открыть каталог товаров",
        "• /cart — показать корзину",
        "• /orders — мои заказы",
        "• /support — техподдержка",
        "• /cancel — отменить текущее действие",
    ]
    
    # Добавляем команду админки для администраторов
    if message.from_user.id in ADMIN_CHAT_IDS:
        help_text.append("• /admin — панель администратора")
    
    help_text.extend([
        "",
        "*📱 Основные функции:*",
        "• Просмотр товаров с фотографиями",
        "• Добавление/удаление из корзины",
        "• Редактирование корзины",
        "• Оформление заказа с выбором доставки",
        "• Просмотр истории заказов",
        "• Отмена заказов"
    ])
    
    await message.answer("\n".join(help_text), reply_markup=main_menu_kb())

# Каталог и товары
@dp.message(Command("catalog"))
@dp.message(F.text == "📸 Каталог")
async def on_catalog(message: Message):
    await message.answer("📂 Выберите категорию:", reply_markup=categories_ikb())

@dp.callback_query(F.data.startswith("cat:"))
async def on_category_select(cb: CallbackQuery):
    category_key = cb.data.split(":")[1]
    await cb.message.answer("🛍️ Товары категории:", reply_markup=category_products_ikb(category_key))
    await cb.answer()

@dp.callback_query(F.data.startswith("prod:"))
async def on_product_select(cb: CallbackQuery):
    product_id = int(cb.data.split(":")[1])

    with next(get_db()) as db:
        product = ProductRepository.get_by_id(db, product_id)
        if not product:
            await cb.answer("❌ Товар не найден")
            return

        description = [
            f"🎯 *{product.name}*",
            f"💰 Цена: {product.price} ₽",
            f"📝 {product.description}",
            f"📏 Размеры: {', '.join(product.sizes)}",
            "",
            "Выберите размер:"
        ]

        try:
            if product.images:
                # Отправляем первую фотографию как основную
                with open(product.images[0], 'rb') as photo:
                    await cb.message.answer_photo(
                        photo=photo,
                        caption="\n".join(description),
                        reply_markup=product_sizes_ikb(product.id),
                        parse_mode="Markdown"
                    )
                
                # Если есть дополнительные фото, отправляем их
                for img_path in product.images[1:]:
                    try:
                        with open(img_path, 'rb') as additional_photo:
                            await cb.message.answer_photo(photo=additional_photo)
                    except Exception as e:
                        logger.error(f"Error sending additional image {img_path}: {e}")
                        
            else:
                await cb.message.answer(
                    "\n".join(description),
                    reply_markup=product_sizes_ikb(product.id),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Error showing product image: {e}")
            await cb.message.answer(
                "📷 " + "\n".join(description),
                reply_markup=product_sizes_ikb(product.id),
                parse_mode="Markdown"
            )

    await cb.answer()

@dp.callback_query(F.data.startswith("size:"))
async def on_size_select(cb: CallbackQuery):
    _, product_id, size = cb.data.split(":")
    await cb.message.answer(f"🔢 Выберите количество для размера {size}:",
                           reply_markup=qty_ikb(int(product_id), size))
    await cb.answer()

@dp.callback_query(F.data.startswith("qty:"))
async def on_qty(cb: CallbackQuery):
    _, product_id, size, qty_str = cb.data.split(":")
    qty = int(qty_str)

    # Извлекаем данные о продукте до закрытия сессии
    product_name = None
    product_price = None
    
    with next(get_db()) as db:
        product = ProductRepository.get_by_id(db, int(product_id))
        if not product:
            await cb.answer("❌ Товар не найден")
            return

        # Сохраняем необходимые данные до закрытия сессии
        product_name = product.name
        product_price = product.price

        user = UserRepository.get_or_create_user(
            db,
            cb.from_user.id,
            cb.from_user.username,
            cb.from_user.first_name,
            cb.from_user.last_name
        )

        CartRepository.add_to_cart(db, user.id, product.id, size, qty)

    cart_text = format_cart(cb.from_user.id)
    await cb.message.answer(
        f"✅ Добавлено: {product_name} — {size} × {qty} = *{product_price * qty} ₽*\n\n{cart_text}",
        reply_markup=main_menu_kb()
    )
    await cb.answer("📦 Добавлено в корзину!")

# Корзина
@dp.message(Command("cart"))
@dp.message(F.text == "🛒 Корзина")
async def on_cart(message: Message):
    cart_text = format_cart(message.from_user.id)
    if "пуста" in cart_text:
        await message.answer(cart_text)
    else:
        await message.answer(cart_text, reply_markup=cart_actions_ikb())

@dp.callback_query(F.data.startswith("cart:"))
async def on_cart_action(cb: CallbackQuery, state: FSMContext):
    action = cb.data.split(":")[1]
    
    if action == "checkout":
        cart_text = format_cart(cb.from_user.id)
        if "пуста" in cart_text:
            await cb.answer("🛒 Корзина пуста!")
            return
            
        await cb.message.answer("👤 Введите ваше ФИО:")
        await state.set_state(OrderFSM.waiting_fullname)
        
    elif action == "clear":
        with next(get_db()) as db:
            user = UserRepository.get_or_create_user(
                db,
                cb.from_user.id,
                cb.from_user.username,
                cb.from_user.first_name,
                cb.from_user.last_name
            )
            CartRepository.clear_cart(db, user.id)
            
        await cb.message.answer("✅ Корзина очищена!", reply_markup=main_menu_kb())
        
    elif action == "edit":
        with next(get_db()) as db:
            user = UserRepository.get_or_create_user(
                db,
                cb.from_user.id,
                cb.from_user.username,
                cb.from_user.first_name,
                cb.from_user.last_name
            )
            cart_items = CartRepository.get_user_cart(db, user.id)
            
            if not cart_items:
                await cb.answer("🛒 Корзина пуста!")
                return
                
            await cb.message.answer(
                "✏️ Выберите товар для удаления:",
                reply_markup=cart_edit_ikb(cart_items)
            )
    
    await cb.answer()

@dp.callback_query(F.data.startswith("remove:"))
async def on_remove_item(cb: CallbackQuery):
    cart_item_id = int(cb.data.split(":")[1])
    
    with next(get_db()) as db:
        cart_item = db.query(CartItem).filter(CartItem.id == cart_item_id).first()
        if cart_item:
            db.delete(cart_item)
            db.commit()
            
            # Получаем обновленную корзину
            user = UserRepository.get_or_create_user(
                db,
                cb.from_user.id,
                cb.from_user.username,
                cb.from_user.first_name,
                cb.from_user.last_name
            )
            cart_items = CartRepository.get_user_cart(db, user.id)
            
            if cart_items:
                await cb.message.edit_text(
                    "✅ Товар удален. Выберите следующий товар для удаления:",
                    reply_markup=cart_edit_ikb(cart_items)
                )
            else:
                await cb.message.edit_text("✅ Корзина очищена!", reply_markup=main_menu_kb())
        else:
            await cb.answer("❌ Товар не найден")
    
    await cb.answer()

@dp.callback_query(F.data == "cart:done")
async def on_cart_edit_done(cb: CallbackQuery):
    await cb.message.edit_text("✅ Редактирование корзины завершено!", reply_markup=main_menu_kb())
    await cb.answer()

# Заказы
@dp.message(Command("orders"))
@dp.message(F.text == "🧾 Мои заказы")
async def on_orders(message: Message):
    with next(get_db()) as db:
        user = UserRepository.get_or_create_user(
            db,
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name
        )
        
        orders = OrderRepository.get_user_orders(db, user.id)
        
        if not orders:
            await message.answer("📭 У вас пока нет заказов.", reply_markup=main_menu_kb())
            return
            
        await message.answer("📋 Ваши заказы:", reply_markup=orders_list_ikb(orders))

@dp.callback_query(F.data.startswith("order:"))
async def on_order_detail(cb: CallbackQuery):
    order_id = int(cb.data.split(":")[1])
    
    with next(get_db()) as db:
        order = OrderRepository.get_order_by_id(db, order_id)
        if not order:
            await cb.answer("❌ Заказ не найден")
            return
            
        await cb.message.answer(format_order(order), reply_markup=order_actions_ikb(order.id, order.status))
    
    await cb.answer()

@dp.callback_query(F.data.startswith("order_cancel:"))
async def on_order_cancel(cb: CallbackQuery):
    order_id = int(cb.data.split(":")[1])
    
    with next(get_db()) as db:
        order = OrderRepository.get_order_by_id(db, order_id)
        if not order:
            await cb.answer("❌ Заказ не найден")
            return
            
        if order.status not in ["pending", "confirmed"]:
            await cb.answer("❌ Нельзя отменить заказ в текущем статусе")
            return
            
        OrderRepository.cancel_order(db, order_id)
        
        await cb.message.edit_text(
            f"✅ Заказ #{order.order_number} отменен!\n\n{format_order(order)}",
            reply_markup=order_actions_ikb(order.id, "cancelled")
        )
    
    await cb.answer()

# Техподдержка
# Техподдержка
@dp.message(Command("support"))
@dp.message(F.text == "❓ Техподдержка")
async def on_support(message: Message, state: FSMContext):
    await message.answer(
        "💬 Напишите ваше сообщение в техподдержку. Мы ответим в ближайшее время:",
        reply_markup=back_to_main_kb()
    )
    await state.set_state(SupportFSM.waiting_message)

@dp.message(SupportFSM.waiting_message)
async def on_support_message(message: Message, state: FSMContext):
    support_message = message.text
    
    # Сохраняем обращение в базу данных
    with next(get_db()) as db:
        user = UserRepository.get_or_create_user(
            db,
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name
        )
        
        ticket = TicketRepository.create_ticket(db, user.id, support_message)
    
    # Логирование обращения в техподдержку
    logger.info(f"Support request from {message.from_user.id}: {support_message}")
    
    # Отправляем уведомление администраторам
    user_mention = mention_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name
    )
    
    admin_message = (
        f"🆘 *Новое обращение в техподдержку!*\n"
        f"🎫 ID: #{ticket.id}\n"
        f"👤 От: {user_mention}\n"
        f"📅 Дата: {ticket.created_at.strftime('%d.%m.%Y %H:%M')}\n\n"
        f"💬 Сообщение:\n{support_message}"
    )
    
    # Отправляем всем администраторам
    for admin_id in ADMIN_CHAT_IDS:
        try:
            await bot.send_message(
                admin_id,
                admin_message,
                parse_mode="Markdown",
                reply_markup=ticket_actions_ikb(ticket.id)  # Функцию создадим ниже
            )
        except Exception as e:
            logger.error(f"Error sending to admin {admin_id}: {e}")
    
    await message.answer(
        f"✅ Ваше сообщение отправлено в техподдержку. Номер вашего обращения: #{ticket.id}\n"
        "Мы свяжемся с вами в ближайшее время.",
        reply_markup=main_menu_kb()
    )
    await state.clear()

# Функция для создания клавиатуры действий с тикетом
def ticket_actions_ikb(ticket_id: int) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="💬 Ответить", callback_data=f"adm_sup:reply:{ticket_id}")
    ib.button(text="🔒 Закрыть", callback_data=f"adm_sup:close:{ticket_id}")
    ib.adjust(2)
    return ib.as_markup()
# Оформление заказа
@dp.message(Command("checkout"))
async def on_checkout(message: Message, state: FSMContext):
    cart_text = format_cart(message.from_user.id)
    if "пуста" in cart_text:
        await message.answer("🛒 Ваша корзина пуста. Добавьте товары перед оформлением заказа.")
        return

    await message.answer("👤 Введите ваше ФИО:")
    await state.set_state(OrderFSM.waiting_fullname)

@dp.message(OrderFSM.waiting_fullname)
async def on_fullname(message: Message, state: FSMContext):
    await state.update_data(fullname=message.text)
    await message.answer("📞 Введите ваш телефон:")
    await state.set_state(OrderFSM.waiting_phone)

@dp.message(OrderFSM.waiting_phone)
async def on_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await message.answer("🚚 Выберите способ доставка:", reply_markup=checkout_delivery_ikb())
    await state.set_state(OrderFSM.waiting_delivery_type)

@dp.callback_query(OrderFSM.waiting_delivery_type, F.data.startswith("delivery:"))
async def on_delivery_type(cb: CallbackQuery, state: FSMContext):
    delivery_type = cb.data.split(":")[1]
    await state.update_data(delivery_type=delivery_type)

    if delivery_type == "cdek":
        await cb.message.answer("🏙️ Введите ваш город для поиска ПВЗ CDEK:")
        await state.set_state(OrderFSM.waiting_cdek_city)
    else:
        await cb.message.answer("🏠 Введите адрес доставки:")
        await state.set_state(OrderFSM.waiting_address)

    await cb.answer()

@dp.message(OrderFSM.waiting_cdek_city)
async def on_cdek_city(message: Message, state: FSMContext):
    await state.update_data(cdek_city=message.text)
    await message.answer("📍 Введите номер ПВЗ CDEK:")
    await state.set_state(OrderFSM.waiting_cdek_pvz)

@dp.message(OrderFSM.waiting_cdek_pvz)
async def on_cdek_pvz(message: Message, state: FSMContext):
    await state.update_data(cdek_pvz=message.text)

    data = await state.get_data()
    delivery_info = f"🏙️ Город: {data['cdek_city']}\n📍 ПВЗ: {data['cdek_pvz']}"

    await message.answer(
        f"📋 Проверьте данные доставки:\n{delivery_info}\n\nПодтвердить заказ?",
        reply_markup=confirm_ikb()
    )
    await state.set_state(OrderFSM.confirm)

@dp.message(OrderFSM.waiting_address)
async def on_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text)

    data = await state.get_data()
    await message.answer(
        f"📋 Проверьте адрес доставки:\n{data['address']}\n\nПодтвердить заказ?",
        reply_markup=confirm_ikb()
    )
    await state.set_state(OrderFSM.confirm)

@dp.callback_query(OrderFSM.confirm, F.data == "confirm:yes")
async def confirm_order(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()

    with next(get_db()) as db:
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
            await cb.message.answer("🛒 Корзина пуста. Нечего подтверждать.", reply_markup=main_menu_kb())
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

        try:
            order = OrderRepository.create_order(
                db, user.id, cart_items,
                data.get("fullname"), data.get("phone"),
                data.get("delivery_type"), delivery_data
            )

            await cb.message.answer(
                "✅ Заказ принят! Мы свяжемся с вами для подтверждения деталей. Спасибо!",
                reply_markup=main_menu_kb()
            )
            
        except Exception as e:
            logger.error(f"Error creating order: {e}")
            await cb.message.answer("❌ Ошибка при создании заказа. Попробуйте позже.")

    await state.clear()
    await cb.answer()

@dp.callback_query(OrderFSM.confirm, F.data == "confirm:cancel")
async def cancel_order(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("❌ Заказ отменен.", reply_markup=main_menu_kb())
    await cb.answer()

# Навигация и отмена
@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return

    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_menu_kb())

@dp.callback_query(F.data.startswith("back:"))
async def on_back(cb: CallbackQuery):
    back_type = cb.data.split(":")[1]

    if back_type == "cats":
        await cb.message.answer("📂 Выберите категорию:", reply_markup=categories_ikb())
    elif back_type == "cat":
        category_key = cb.data.split(":")[2]
        await cb.message.answer("🛍️ Товары категории:", reply_markup=category_products_ikb(category_key))
    elif back_type == "size":
        product_id = int(cb.data.split(":")[2])
        await cb.message.answer("📏 Выберите размер:", reply_markup=product_sizes_ikb(product_id))
    elif back_type == "main":
        await cb.message.answer("📱 Главное меню:", reply_markup=main_menu_kb())

@dp.callback_query(F.data == "orders:list")
async def on_back_to_orders(cb: CallbackQuery):
    with next(get_db()) as db:
        user = UserRepository.get_or_create_user(
            db,
            cb.from_user.id,
            cb.from_user.username,
            cb.from_user.first_name,
            cb.from_user.last_name
        )
        
        orders = OrderRepository.get_user_orders(db, user.id)
        
        if not orders:
            await cb.message.answer("📭 У вас пока нет заказов.", reply_markup=main_menu_kb())
            return
            
        await cb.message.answer("📋 Ваши заказы:", reply_markup=orders_list_ikb(orders))
    
    await cb.answer()

# Запуск бота
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
        print("Bot stopped.")