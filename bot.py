import asyncio
import os
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
import re
import traceback
from functools import wraps
from typing import Optional, List

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
from sqlalchemy.orm import Session, joinedload
import json

# Локальные импорты
from database import get_db, init_db
from models import User, Category, Product, CartItem, Order, OrderItem, Review
from repositories import (
    TicketRepository, UserRepository, CategoryRepository, ProductRepository,
    CartRepository, OrderRepository, ReviewRepository
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

# =============================================================================
# 🛡️ СИСТЕМА ЗАЩИТЫ ОТ СПАМА
# =============================================================================

class RateLimiter:
    def __init__(self):
        self.user_actions = defaultdict(list)
        self.limits = {
            'message': (5, 10),  # 5 сообщений в 10 секунд
            'callback': (10, 5),  # 10 нажатий в 5 секунд
            'support': (2, 60),   # 2 обращения в минуту
            'order': (3, 300)     # 3 заказа в 5 минут
        }
    
    async def check_limit(self, user_id: int, action_type: str) -> bool:
        now = datetime.now()
        limit_count, limit_seconds = self.limits.get(action_type, (5, 10))
        
        user_key = f"{user_id}_{action_type}"
        
        # Очищаем старые записи
        self.user_actions[user_key] = [
            timestamp for timestamp in self.user_actions[user_key]
            if (now - timestamp).seconds < limit_seconds
        ]
        
        # Проверяем лимит
        if len(self.user_actions[user_key]) >= limit_count:
            return False
        
        self.user_actions[user_key].append(now)
        return True

rate_limiter = RateLimiter()

# Декоратор для проверки лимитов
def rate_limit(action_type: str):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Определяем user_id из аргументов
            update = args[0]
            user_id = update.from_user.id if hasattr(update, 'from_user') else update.message.from_user.id
            
            if not await rate_limiter.check_limit(user_id, action_type):
                if hasattr(update, 'answer'):
                    await update.answer("🚫 Слишком много запросов. Подождите немного.", show_alert=True)
                return
            return await func(*args, **kwargs)
        return wrapper
    return decorator

# =============================================================================
# ✅ ВАЛИДАЦИЯ ДАННЫХ
# =============================================================================

class OrderValidation:
    @staticmethod
    def validate_fullname(fullname: str) -> tuple[bool, str]:
        fullname = fullname.strip()
        if len(fullname) < 2 or len(fullname) > 100:
            return False, 'ФИО должно быть от 2 до 100 символов'
        if not re.match(r'^[a-zA-Zа-яА-ЯёЁ\s\-]+$', fullname):
            return False, 'ФИО может содержать только буквы, пробелы и дефисы'
        return True, fullname
    
    @staticmethod
    def validate_phone(phone: str) -> tuple[bool, str]:
        # Очищаем номер от всего кроме цифр
        cleaned = re.sub(r'\D', '', phone)
        if len(cleaned) not in [10, 11]:
            return False, 'Неверный формат телефона. Пример: +7 999 123-45-67'
        return True, cleaned
    
    @staticmethod
    def validate_delivery_type(delivery_type: str) -> tuple[bool, str]:
        if delivery_type not in ['cdek', 'courier']:
            return False, 'Неверный тип доставки'
        return True, delivery_type

# =============================================================================
# 🛠️ ОБРАБОТКА ОШИБОК
# =============================================================================

# Декоратор для безопасной работы с БД
def safe_db_operation(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Database operation failed in {func.__name__}: {e}")
            
            # Пытаемся отправить сообщение об ошибке пользователю
            try:
                update = args[0]
                if hasattr(update, 'message'):
                    await update.message.answer("😔 Произошла ошибка. Попробуйте позже.")
                elif hasattr(update, 'answer'):
                    await update.answer("😔 Произошла ошибка. Попробуйте позже.", show_alert=True)
            except:
                pass
            
            return None
    return wrapper

# Функция для безопасного получения сессии БД
def get_db_safe():
    try:
        db = next(get_db())
        return db
    except Exception as e:
        logger.error(f"Failed to get database session: {e}")
        return None

# Утилита для повторных попыток
async def retry_operation(operation, max_retries=3, delay=1):
    for attempt in range(max_retries):
        try:
            return await operation()
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            await asyncio.sleep(delay)
    return None

# =============================================================================
# 🔔 СИСТЕМА УВЕДОМЛЕНИЙ
# =============================================================================

async def send_order_notification(user_id: int, order: Order, old_status: str = None):
    status_messages = {
        "pending": "🟡 Ваш заказ ожидает обработки",
        "confirmed": "✅ Заказ подтвержден",
        "processing": "🔧 Заказ собирается",
        "shipped": "🚚 Заказ отправлен",
        "delivered": "📦 Заказ доставлен",
        "cancelled": "❌ Заказ отменен",
        "closed": "🔒 Заказ завершен"
    }
    
    message = status_messages.get(order.status, "📋 Статус заказа обновлен")
    
    try:
        await bot.send_message(
            user_id, 
            f"{message} #{order.order_number}\n"
            f"💳 Сумма: {order.total_amount} ₽\n"
            f"📦 Статус: {order.status}"
        )
    except Exception as e:
        logger.error(f"Failed to send notification to {user_id}: {e}")

# =============================================================================
# СОСТОЯНИЯ FSM
# =============================================================================

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

class ReviewFSM(StatesGroup):
    waiting_rating = State()
    waiting_comment = State()

# =============================================================================
# КЛАВИАТУРЫ
# =============================================================================

def main_menu_kb(user_id: int = None) -> ReplyKeyboardMarkup:
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
    with get_db_safe() as db:
        if not db:
            return InlineKeyboardMarkup(inline_keyboard=[])
        categories = CategoryRepository.get_all_active(db)
        ib = InlineKeyboardBuilder()
        for category in categories:
            ib.button(text=category.title, callback_data=f"cat:{category.key}")
        ib.adjust(1)
        return ib.as_markup()

def category_products_ikb(cat_key: str, page: int = 0, products_per_page: int = 5) -> InlineKeyboardMarkup:
    with get_db_safe() as db:
        if not db:
            return InlineKeyboardMarkup(inline_keyboard=[])
            
        category = CategoryRepository.get_by_key(db, cat_key)
        if not category:
            return InlineKeyboardMarkup(inline_keyboard=[])

        products = ProductRepository.get_by_category(db, category.id)
        
        # Пагинация
        total_pages = (len(products) + products_per_page - 1) // products_per_page
        start_idx = page * products_per_page
        end_idx = start_idx + products_per_page
        paginated_products = products[start_idx:end_idx]
        
        ib = InlineKeyboardBuilder()
        
        # Товары текущей страницы
        for product in paginated_products:
            ib.button(text=f"{product.name} — {product.price} ₽", callback_data=f"prod:{product.id}")
        
        # Навигация
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"cat_page:{cat_key}:{page-1}"))
        
        nav_buttons.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
        
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"cat_page:{cat_key}:{page+1}"))
        
        if nav_buttons:
            ib.row(*nav_buttons)
        
        ib.button(text="⬅️ Назад к категориям", callback_data="back:cats")
        ib.adjust(1)
        return ib.as_markup()

def product_sizes_ikb(product_id: int) -> InlineKeyboardMarkup:
    with get_db_safe() as db:
        if not db:
            return InlineKeyboardMarkup(inline_keyboard=[])
            
        product = ProductRepository.get_by_id(db, product_id)
        if not product:
            return InlineKeyboardMarkup(inline_keyboard=[])

        ib = InlineKeyboardBuilder()
        for size in product.sizes:
            ib.button(text=size, callback_data=f"size:{product.id}:{size}")
        
        # Кнопка просмотра отзывов
        reviews = ReviewRepository.get_product_reviews(db, product_id)
        if reviews:
            ib.button(text="⭐ Посмотреть отзывы", callback_data=f"show_reviews:{product_id}")
            
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

def order_actions_ikb(order_id: int, status: str, user_id: int = None) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    if status in ["pending", "confirmed"]:
        ib.button(text="❌ Отменить заказ", callback_data=f"order_cancel:{order_id}")
    
    # Кнопка отзыва для завершенных заказов
    if status == "delivered":
        ib.button(text="⭐ Оставить отзыв", callback_data=f"order_review:{order_id}")
    
    ib.button(text="⬅️ Назад к заказам", callback_data="orders:list")
    ib.adjust(1)
    return ib.as_markup()

def rating_ikb(product_id: int, order_id: int) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    for i in range(1, 6):
        ib.button(text="⭐" * i, callback_data=f"review_rating:{product_id}:{order_id}:{i}")
    ib.button(text="❌ Отмена", callback_data="review_cancel")
    ib.adjust(1)
    return ib.as_markup()

def format_cart(user_id: int) -> str:
    with get_db_safe() as db:
        if not db:
            return "❌ Ошибка загрузки корзины"
            
        user = db.query(User).filter(User.telegram_id == user_id).first()
        if not user:
            return "Пользователь не найден."

        cart_items = CartRepository.get_user_cart(db, user.id)

        if not cart_items:
            return "🛒 *Ваша корзина пуста*"

        lines = ["🛒 *Ваша корзина:*"]
        total = 0

        for item in cart_items:
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

# =============================================================================
# ИНИЦИАЛИЗАЦИЯ БОТА
# =============================================================================

dp = Dispatcher(storage=MemoryStorage())
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))

# Глобальный обработчик ошибок
@dp.errors()
async def global_error_handler(event: Exception, bot: Bot):
    logger.error(f"Global error: {event}", exc_info=True)
    
    # Отправляем уведомление админам об критических ошибках
    error_text = f"🚨 Critical error: {str(event)}\n\n{traceback.format_exc()[:1000]}"
    
    for admin_id in ADMIN_CHAT_IDS:
        try:
            await bot.send_message(admin_id, error_text)
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")

# Регистрируем админские обработчики
register_admin_panel(dp, bot)
register_support(dp, bot)

# =============================================================================
# ОСНОВНЫЕ ОБРАБОТЧИКИ
# =============================================================================

@dp.message(CommandStart())
@safe_db_operation
@rate_limit("message")
async def on_start(message: Message):
    logger.info(f"User {message.from_user.id} started bot")

    with get_db_safe() as db:
        if db:
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
        reply_markup=main_menu_kb(message.from_user.id)
    )

@dp.message(Command("help"))
@safe_db_operation
@rate_limit("message")
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
    
    await message.answer("\n".join(help_text), reply_markup=main_menu_kb(message.from_user.id))

# =============================================================================
# КАТАЛОГ И ТОВАРЫ
# =============================================================================

@dp.message(Command("catalog"))
@dp.message(F.text == "📸 Каталог")
@safe_db_operation
@rate_limit("message")
async def on_catalog(message: Message):
    await message.answer("📂 Выберите категорию:", reply_markup=categories_ikb())

@dp.callback_query(F.data.startswith("cat:"))
@safe_db_operation
@rate_limit("callback")
async def on_category_select(cb: CallbackQuery):
    category_key = cb.data.split(":")[1]
    await cb.message.answer("🛍️ Товары категории:", reply_markup=category_products_ikb(category_key))
    await cb.answer()

@dp.callback_query(F.data.startswith("cat_page:"))
@safe_db_operation
@rate_limit("callback")
async def on_category_page(cb: CallbackQuery):
    _, cat_key, page_str = cb.data.split(":")
    page = int(page_str)
    await cb.message.edit_reply_markup(reply_markup=category_products_ikb(cat_key, page))
    await cb.answer()

@dp.callback_query(F.data.startswith("prod:"))
@safe_db_operation
@rate_limit("callback")
async def on_product_select(cb: CallbackQuery):
    product_id = int(cb.data.split(":")[1])

    with get_db_safe() as db:
        if not db:
            await cb.answer("❌ Ошибка загрузки товара")
            return
            
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
                with open(product.images[0], 'rb') as photo:
                    await cb.message.answer_photo(
                        photo=photo,
                        caption="\n".join(description),
                        reply_markup=product_sizes_ikb(product.id),
                        parse_mode="Markdown"
                    )
                
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
@safe_db_operation
@rate_limit("callback")
async def on_size_select(cb: CallbackQuery):
    _, product_id, size = cb.data.split(":")
    await cb.message.answer(f"🔢 Выберите количество для размера {size}:",
                           reply_markup=qty_ikb(int(product_id), size))
    await cb.answer()

@dp.callback_query(F.data.startswith("qty:"))
@safe_db_operation
@rate_limit("callback")
async def on_qty(cb: CallbackQuery):
    _, product_id, size, qty_str = cb.data.split(":")
    qty = int(qty_str)

    product_name = None
    product_price = None
    
    with get_db_safe() as db:
        if not db:
            await cb.answer("❌ Ошибка добавления в корзину")
            return
            
        product = ProductRepository.get_by_id(db, int(product_id))
        if not product:
            await cb.answer("❌ Товар не найден")
            return

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
        reply_markup=main_menu_kb(cb.from_user.id)
    )
    await cb.answer("📦 Добавлено в корзину!")

# =============================================================================
# ⭐ СИСТЕМА ОТЗЫВОВ
# =============================================================================

@dp.callback_query(F.data.startswith("show_reviews:"))
@safe_db_operation
@rate_limit("callback")
async def show_product_reviews(cb: CallbackQuery):
    product_id = int(cb.data.split(":")[1])
    
    with get_db_safe() as db:
        if not db:
            await cb.answer("❌ Ошибка загрузки отзывов")
            return
            
        product = ProductRepository.get_by_id(db, product_id)
        reviews = ReviewRepository.get_product_reviews(db, product_id)
    
    if not reviews:
        await cb.answer("😔 Отзывов пока нет", show_alert=True)
        return
    
    avg_rating = sum(r.rating for r in reviews) / len(reviews)
    
    text = [f"⭐ Отзывы о {product.name} (средняя оценка: {avg_rating:.1f}/5):"]
    
    for review in reviews[:5]:
        user_name = review.user.first_name or "Пользователь"
        text.append(f"\n⭐ {review.rating}/5 от {user_name}")
        if review.comment:
            text.append(f"💬 {review.comment}")
    
    await cb.message.answer("\n".join(text))
    await cb.answer()

@dp.callback_query(F.data.startswith("order_review:"))
@safe_db_operation
@rate_limit("callback")
async def on_order_review(cb: CallbackQuery, state: FSMContext):
    order_id = int(cb.data.split(":")[1])
    
    with get_db_safe() as db:
        if not db:
            await cb.answer("❌ Ошибка")
            return
            
        order = OrderRepository.get_order_by_id(db, order_id)
        if not order or order.status != "delivered":
            await cb.answer("❌ Нельзя оставить отзыв для этого заказа")
            return
        
        ib = InlineKeyboardBuilder()
        for item in order.items:
            ib.button(text=f"⭐ {item.product_name}", 
                     callback_data=f"leave_review:{item.product_id}:{order_id}")
        
        ib.button(text="❌ Отмена", callback_data=f"order:{order_id}")
        ib.adjust(1)
        
        await cb.message.edit_text(
            "Выберите товар для отзыва:",
            reply_markup=ib.as_markup()
        )
    
    await cb.answer()

@dp.callback_query(F.data.startswith("leave_review:"))
@safe_db_operation
@rate_limit("callback")
async def start_review(cb: CallbackQuery, state: FSMContext):
    product_id = int(cb.data.split(":")[1])
    order_id = int(cb.data.split(":")[2])
    
    with get_db_safe() as db:
        if not db:
            await cb.answer("❌ Ошибка")
            return
            
        # Получаем пользователя из базы
        user = UserRepository.get_or_create_user(
            db,
            cb.from_user.id,
            cb.from_user.username,
            cb.from_user.first_name,
            cb.from_user.last_name
        )
        
        # Проверяем, может ли пользователь оставить отзыв для этого заказа
        order = OrderRepository.get_order_by_id(db, order_id)
        if not order or order.user_id != user.id or order.status != "delivered":
            await cb.answer("❌ Нельзя оставить отзыв для этого заказа")
            return
            
        product = ProductRepository.get_by_id(db, product_id)
        if not product:
            await cb.answer("❌ Товар не найден")
            return
    
    await state.update_data(
        product_id=product_id, 
        order_id=order_id,
        user_id=user.id  # Сохраняем ID пользователя из базы
    )
    await state.set_state(ReviewFSM.waiting_rating)
    
    await cb.message.answer(
        f"💬 Оставьте отзыв о товаре: {product.name}\n"
        "Выберите оценку:",
        reply_markup=rating_ikb(product_id, order_id)
    )
    await cb.answer()

@dp.callback_query(ReviewFSM.waiting_rating, F.data.startswith("review_rating:"))
@safe_db_operation
@rate_limit("callback")
async def on_rating_select(cb: CallbackQuery, state: FSMContext):
    rating = int(cb.data.split(":")[3])
    await state.update_data(rating=rating)
    await state.set_state(ReviewFSM.waiting_comment)
    
    await cb.message.edit_text(
        f"⭐ Ваша оценка: {rating}/5\n"
        "Напишите комментарий к отзыву (или отправьте '-' чтобы пропустить):"
    )
    await cb.answer()

@dp.callback_query(ReviewFSM.waiting_rating, F.data == "review_cancel")
@safe_db_operation
@rate_limit("callback")
async def on_review_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Создание отзыва отменено")
    await cb.answer()

@dp.message(ReviewFSM.waiting_comment)
@safe_db_operation
@rate_limit("message")
async def on_review_comment(message: Message, state: FSMContext):
    data = await state.get_data()
    comment = message.text if message.text != "-" else ""
    
    with get_db_safe() as db:
        if not db:
            await message.answer("❌ Ошибка при сохранении отзыва")
            await state.clear()
            return
            
        # Получаем или создаем пользователя
        user = UserRepository.get_or_create_user(
            db,
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name
        )
        
        # Проверяем существование заказа и товара
        order = OrderRepository.get_order_by_id(db, data['order_id'])
        product = ProductRepository.get_by_id(db, data['product_id'])
        
        if not order or not product:
            await message.answer("❌ Ошибка: заказ или товар не найден")
            await state.clear()
            return
            
        # Создаем отзов
        ReviewRepository.create_review(
            db, user.id, data['product_id'], 
            data['order_id'], data['rating'], comment
        )
    
    await message.answer("✅ Спасибо за ваш отзыв!", reply_markup=main_menu_kb(message.from_user.id))
    await state.clear()

# =============================================================================
# КОРЗИНА
# =============================================================================

@dp.message(Command("cart"))
@dp.message(F.text == "🛒 Корзина")
@safe_db_operation
@rate_limit("message")
async def on_cart(message: Message):
    cart_text = format_cart(message.from_user.id)
    if "пуста" in cart_text:
        await message.answer(cart_text)
    else:
        await message.answer(cart_text, reply_markup=cart_actions_ikb())

@dp.callback_query(F.data.startswith("cart:"))
@safe_db_operation
@rate_limit("callback")
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
        with get_db_safe() as db:
            if db:
                user = UserRepository.get_or_create_user(
                    db,
                    cb.from_user.id,
                    cb.from_user.username,
                    cb.from_user.first_name,
                    cb.from_user.last_name
                )
                CartRepository.clear_cart(db, user.id)
            
        await cb.message.answer("✅ Корзина очищена!", reply_markup=main_menu_kb(cb.from_user.id))
        
    elif action == "edit":
        with get_db_safe() as db:
            if db:
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
@safe_db_operation
@rate_limit("callback")
async def on_remove_item(cb: CallbackQuery):
    cart_item_id = int(cb.data.split(":")[1])
    
    with get_db_safe() as db:
        if db:
            cart_item = db.query(CartItem).filter(CartItem.id == cart_item_id).first()
            if cart_item:
                db.delete(cart_item)
                db.commit()
                
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
                    await cb.message.edit_text("✅ Корзина очищена!", reply_markup=main_menu_kb(cb.from_user.id))
            else:
                await cb.answer("❌ Товар не найден")
    
    await cb.answer()

@dp.callback_query(F.data == "cart:done")
@safe_db_operation
@rate_limit("callback")
async def on_cart_edit_done(cb: CallbackQuery):
    await cb.message.edit_text("✅ Редактирование корзины завершено!", reply_markup=main_menu_kb(cb.from_user.id))
    await cb.answer()

# =============================================================================
# ЗАКАЗЫ
# =============================================================================

@dp.message(Command("orders"))
@dp.message(F.text == "🧾 Мои заказы")
@safe_db_operation
@rate_limit("message")
async def on_orders(message: Message):
    with get_db_safe() as db:
        if not db:
            await message.answer("❌ Ошибка загрузки заказов")
            return
            
        user = UserRepository.get_or_create_user(
            db,
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name
        )
        
        orders = OrderRepository.get_user_orders(db, user.id)
        
        if not orders:
            await message.answer("📭 У вас пока нет заказов.", reply_markup=main_menu_kb(message.from_user.id))
            return
            
        await message.answer("📋 Ваши заказы:", reply_markup=orders_list_ikb(orders))

@dp.callback_query(F.data.startswith("order:"))
@safe_db_operation
@rate_limit("callback")
async def on_order_detail(cb: CallbackQuery):
    order_id = int(cb.data.split(":")[1])
    
    with get_db_safe() as db:
        if not db:
            await cb.answer("❌ Ошибка загрузки заказа")
            return
            
        order = OrderRepository.get_order_by_id(db, order_id)
        if not order:
            await cb.answer("❌ Заказ не найден")
            return
            
        await cb.message.answer(format_order(order), reply_markup=order_actions_ikb(order.id, order.status, cb.from_user.id))
    
    await cb.answer()

@dp.callback_query(F.data.startswith("order_cancel:"))
@safe_db_operation
@rate_limit("callback")
async def on_order_cancel(cb: CallbackQuery):
    order_id = int(cb.data.split(":")[1])
    
    with get_db_safe() as db:
        if not db:
            await cb.answer("❌ Ошибка")
            return
            
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
            reply_markup=order_actions_ikb(order.id, "cancelled", cb.from_user.id)
        )
    
    await cb.answer()

# =============================================================================
# ТЕХПОДДЕРЖКА
# =============================================================================

@dp.message(Command("support"))
@dp.message(F.text == "❓ Техподдержка")
@safe_db_operation
@rate_limit("message")
async def on_support(message: Message, state: FSMContext):
    await message.answer(
        "💬 Напишите ваше сообщение в техподдержку. Мы ответим в ближайшее время:",
        reply_markup=back_to_main_kb()
    )
    await state.set_state(SupportFSM.waiting_message)

@dp.message(SupportFSM.waiting_message)
@safe_db_operation
@rate_limit("support")
async def on_support_message(message: Message, state: FSMContext):
    support_message = message.text
    
    with get_db_safe() as db:
        if db:
            user = UserRepository.get_or_create_user(
                db,
                message.from_user.id,
                message.from_user.username,
                message.from_user.first_name,
                message.from_user.last_name
            )
            
            ticket = TicketRepository.create_ticket(db, user.id, support_message)
    
    logger.info(f"Support request from {message.from_user.id}: {support_message}")
    
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
    
    for admin_id in ADMIN_CHAT_IDS:
        try:
            await bot.send_message(
                admin_id,
                admin_message,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Error sending to admin {admin_id}: {e}")
    
    await message.answer(
        f"✅ Ваше сообщение отправлено в техподдержку. Номер вашего обращения: #{ticket.id}\n"
        "Мы свяжемся с вами в ближайшее время.",
        reply_markup=main_menu_kb(message.from_user.id)
    )
    await state.clear()

# =============================================================================
# ОФОРМЛЕНИЕ ЗАКАЗА С ВАЛИДАЦИЕЙ
# =============================================================================

@dp.message(Command("checkout"))
@safe_db_operation
@rate_limit("message")
async def on_checkout(message: Message, state: FSMContext):
    cart_text = format_cart(message.from_user.id)
    if "пуста" in cart_text:
        await message.answer("🛒 Ваша корзина пуста. Добавьте товары перед оформлением заказа.")
        return

    await message.answer("👤 Введите ваше ФИО:")
    await state.set_state(OrderFSM.waiting_fullname)

@dp.message(OrderFSM.waiting_fullname)
@safe_db_operation
@rate_limit("message")
async def on_fullname(message: Message, state: FSMContext):
    is_valid, result = OrderValidation.validate_fullname(message.text)
    if not is_valid:
        await message.answer(f"❌ {result}\nПожалуйста, введите ФИО еще раз:")
        return
        
    await state.update_data(fullname=result)
    await message.answer("📞 Введите ваш телефон:")
    await state.set_state(OrderFSM.waiting_phone)

@dp.message(OrderFSM.waiting_phone)
@safe_db_operation
@rate_limit("message")
async def on_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    is_valid, result = OrderValidation.validate_phone(message.text)
    if not is_valid:
        await message.answer(f"❌ {result}\nПожалуйста, введите телефон еще раз:")
        return
        
    await state.update_data(phone=result)
    await message.answer("🚚 Выберите способ доставки:", reply_markup=checkout_delivery_ikb())
    await state.set_state(OrderFSM.waiting_delivery_type)

@dp.callback_query(OrderFSM.waiting_delivery_type, F.data.startswith("delivery:"))
@safe_db_operation
@rate_limit("callback")
async def on_delivery_type(cb: CallbackQuery, state: FSMContext):
    delivery_type = cb.data.split(":")[1]
    is_valid, result = OrderValidation.validate_delivery_type(delivery_type)
    if not is_valid:
        await cb.answer("❌ Неверный тип доставки")
        return
        
    await state.update_data(delivery_type=result)

    if delivery_type == "cdek":
        await cb.message.answer("🏙️ Введите ваш город для поиска ПВЗ CDEK:")
        await state.set_state(OrderFSM.waiting_cdek_city)
    else:
        await cb.message.answer("🏠 Введите адрес доставки:")
        await state.set_state(OrderFSM.waiting_address)

    await cb.answer()

@dp.message(OrderFSM.waiting_cdek_city)
@safe_db_operation
@rate_limit("message")
async def on_cdek_city(message: Message, state: FSMContext):
    await state.update_data(cdek_city=message.text)
    await message.answer("📍 Введите номер ПВЗ CDEK:")
    await state.set_state(OrderFSM.waiting_cdek_pvz)

@dp.message(OrderFSM.waiting_cdek_pvz)
@safe_db_operation
@rate_limit("message")
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
@safe_db_operation
@rate_limit("message")
async def on_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text)

    data = await state.get_data()
    await message.answer(
        f"📋 Проверьте адрес доставки:\n{data['address']}\n\nПодтвердить заказ?",
        reply_markup=confirm_ikb()
    )
    await state.set_state(OrderFSM.confirm)

@dp.callback_query(OrderFSM.confirm, F.data == "confirm:yes")
@safe_db_operation
@rate_limit("callback")
async def confirm_order(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()

    with get_db_safe() as db:
        if not db:
            await state.clear()
            await cb.message.answer("❌ Ошибка создания заказа", reply_markup=main_menu_kb(cb.from_user.id))
            await cb.answer()
            return
            
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
            await cb.message.answer("🛒 Корзина пуста. Нечего подтверждать.", reply_markup=main_menu_kb(cb.from_user.id))
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
                reply_markup=main_menu_kb(cb.from_user.id)
            )
            
        except Exception as e:
            logger.error(f"Error creating order: {e}")
            await cb.message.answer("❌ Ошибка при создании заказа. Попробуйте позже.")

    await state.clear()
    await cb.answer()

@dp.callback_query(OrderFSM.confirm, F.data == "confirm:cancel")
@safe_db_operation
@rate_limit("callback")
async def cancel_order(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("❌ Заказ отменен.", reply_markup=main_menu_kb(cb.from_user.id))
    await cb.answer()

# =============================================================================
# НАВИГАЦИЯ И ОТМЕНА
# =============================================================================

@dp.message(Command("cancel"))
@safe_db_operation
@rate_limit("message")
async def cancel_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return

    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_menu_kb(message.from_user.id))

@dp.callback_query(F.data.startswith("back:"))
@safe_db_operation
@rate_limit("callback")
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
        await cb.message.answer("📱 Главное меню:", reply_markup=main_menu_kb(cb.from_user.id))

@dp.callback_query(F.data == "orders:list")
@safe_db_operation
@rate_limit("callback")
async def on_back_to_orders(cb: CallbackQuery):
    with get_db_safe() as db:
        if not db:
            await cb.answer("❌ Ошибка")
            return
            
        user = UserRepository.get_or_create_user(
            db,
            cb.from_user.id,
            cb.from_user.username,
            cb.from_user.first_name,
            cb.from_user.last_name
        )
        
        orders = OrderRepository.get_user_orders(db, user.id)
        
        if not orders:
            await cb.message.answer("📭 У вас пока нет заказов.", reply_markup=main_menu_kb(cb.from_user.id))
            return
            
        await cb.message.answer("📋 Ваши заказы:", reply_markup=orders_list_ikb(orders))
    
    await cb.answer()

# =============================================================================
# ЗАПУСК БОТА
# =============================================================================

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