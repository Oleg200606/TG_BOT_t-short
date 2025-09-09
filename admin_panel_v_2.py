import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from dotenv import load_dotenv
from sqlalchemy.orm import Session
from database import get_db
from models import User, Category, Product, Order
from repositories import (
    UserRepository,
    CategoryRepository,
    ProductRepository,
    OrderRepository,
)

load_dotenv()
ADMIN_CHAT_IDS = [int(x.strip()) for x in os.getenv("ADMIN_CHAT_IDS", "").split(",") if x.strip()]

SUPPORT_FILE = Path("support_tickets.json")


def _load_support() -> List[Dict]:
    if not SUPPORT_FILE.exists():
        return []
    try:
        return json.loads(SUPPORT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_support(tickets: List[Dict]):
    SUPPORT_FILE.write_text(json.dumps(tickets, ensure_ascii=False, indent=2), encoding="utf-8")


def mention_user(user_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str]) -> str:
    if username:
        return f"@{username}"
    display = " ".join([x for x in [first_name, last_name] if x]) or "пользователь"
    return f"[{display}](tg://user?id={user_id})"


def paginate(items: List, page: int, per_page: int = 10):
    total = len(items)
    start = page * per_page
    end = start + per_page
    return items[start:end], total

def admin_menu_kb() -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="📦 Товары", callback_data="adm:products")
    ib.button(text="🧾 Заказы", callback_data="adm:orders")
    ib.button(text="🆘 Техподдержка", callback_data="adm:support")
    ib.button(text="📊 Статистика", callback_data="adm:stats")
    ib.button(text="👤 Главное меню", callback_data="adm:home")
    ib.adjust(2, 2, 1)
    return ib.as_markup()


def admin_products_menu_kb() -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="➕ Создать товар", callback_data="adm_prod:create")
    ib.button(text="🗂 Список товаров", callback_data="adm_prod:list:0")
    ib.button(text="⬅️ Назад", callback_data="adm:back")
    ib.adjust(1, 1, 1)
    return ib.as_markup()


def admin_orders_menu_kb() -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="📋 Все заказы", callback_data="adm_order:list:0")
    ib.button(text="⏳ В ожидании", callback_data="adm_order:filter:pending:0")
    ib.button(text="✅ Подтверждённые", callback_data="adm_order:filter:confirmed:0")
    ib.button(text="❌ Отменённые", callback_data="adm_order:filter:cancelled:0")
    ib.button(text="🔒 Закрытые", callback_data="adm_order:filter:closed:0")
    ib.button(text="⬅️ Назад", callback_data="adm:back")
    ib.adjust(2, 2, 1)
    return ib.as_markup()


def admin_support_menu_kb() -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="📨 Открытые", callback_data="adm_sup:list:open:0")
    ib.button(text="✅ Закрытые", callback_data="adm_sup:list:closed:0")
    ib.button(text="⬅️ Назад", callback_data="adm:back")
    ib.adjust(2, 1)
    return ib.as_markup()


def order_status_kb(order_id: int) -> InlineKeyboardMarkup:
    statuses = [
        ("⏳ pending", "pending"),
        ("✅ confirmed", "confirmed"),
        ("⚙️ processing", "processing"),
        ("📦 shipped", "shipped"),
        ("📬 delivered", "delivered"),
        ("❌ cancelled", "cancelled"),
        ("🔒 closed", "closed"),
    ]
    ib = InlineKeyboardBuilder()
    for text, key in statuses:
        ib.button(text=text, callback_data=f"adm_order:set_status:{order_id}:{key}")
    ib.button(text="⬅️ Назад", callback_data=f"adm_order:view:{order_id}")
    ib.adjust(2, 2, 2, 1, 1)
    return ib.as_markup()


def ticket_actions_kb(ticket_id: str) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="💬 Ответить", callback_data=f"adm_sup:reply:{ticket_id}")
    ib.button(text="🔒 Закрыть", callback_data=f"adm_sup:close:{ticket_id}")
    ib.adjust(2)
    return ib.as_markup()

class AdminProductCreateFSM(StatesGroup):
    name = State()
    description = State()
    price = State()
    category = State()
    sizes = State()
    images = State()
    confirm = State()


class AdminProductEditFSM(StatesGroup):
    waiting_field = State()  # name/description/price/sizes/add_image
    new_value = State()
    add_photo = State()


class AdminOrderEditFSM(StatesGroup):
    waiting_status = State()


class SupportUserFSM(StatesGroup):
    waiting_text = State()


class SupportAdminReplyFSM(StatesGroup):
    waiting_text = State()


def register_admin_panel(dp: Dispatcher, bot: Bot):

    @dp.message(Command("admin"))
    async def admin_entry(message: Message):
        # Простая проверка без обращения к БД
        if message.from_user.id not in ADMIN_CHAT_IDS:
            await message.answer("Команда доступна только администраторам.")
            return
        
        await message.answer("Панель администратора:", reply_markup=admin_menu_kb())

    #(инлайн)
    @dp.callback_query(F.data == "adm:products")
    async def adm_products_menu(cb: CallbackQuery):
        if cb.from_user.id not in ADMIN_CHAT_IDS:
            await cb.answer("Нет доступа", show_alert=True)
            return
        await cb.message.edit_text("📦 Управление товарами:", reply_markup=admin_products_menu_kb())
        await cb.answer()

    @dp.callback_query(F.data == "adm:orders")
    async def adm_orders_menu(cb: CallbackQuery):
        if cb.from_user.id not in ADMIN_CHAT_IDS:
            await cb.answer("Нет доступа", show_alert=True)
            return
        await cb.message.edit_text("🧾 Управление заказами:", reply_markup=admin_orders_menu_kb())
        await cb.answer()

    @dp.callback_query(F.data == "adm:support")
    async def adm_support_menu(cb: CallbackQuery):
        if cb.from_user.id not in ADMIN_CHAT_IDS:
            await cb.answer("Нет доступа", show_alert=True)
            return
        await cb.message.edit_text("🆘 Техподдержка:", reply_markup=admin_support_menu_kb())
        await cb.answer()

    @dp.callback_query(F.data == "adm:stats")
    async def adm_stats(cb: CallbackQuery):
        if cb.from_user.id not in ADMIN_CHAT_IDS:
            await cb.answer("Нет доступа", show_alert=True)
            return
        db = next(get_db())
        try:
            total_orders = db.query(Order).count()
            total_users = db.query(User).count()
            pending_orders = db.query(Order).filter(Order.status == "pending").count()
            revenue = db.query(Order).filter(Order.status.in_(["confirmed", "processing", "shipped", "delivered"]))
            total_revenue = sum(o.total_amount for o in revenue)
        finally:
            db.close()
        text = (
            "📊 *Статистика магазина*\n"
            f"Всего заказов: {total_orders}\n"
            f"Ожидают обработки: {pending_orders}\n"
            f"Всего пользователей: {total_users}\n"
            f"Общая выручка: {total_revenue} ₽"
        )
        await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_menu_kb())
        await cb.answer()

    @dp.callback_query(F.data == "adm:home")
    @dp.callback_query(F.data == "adm:back")
    async def adm_back(cb: CallbackQuery):
        await cb.message.edit_text("Панель администратора:", reply_markup=admin_menu_kb())
        await cb.answer()


    @dp.callback_query(F.data == "adm_prod:create")
    async def adm_prod_create_start(cb: CallbackQuery, state: FSMContext):
        if cb.from_user.id not in ADMIN_CHAT_IDS:
            await cb.answer("Нет доступа", show_alert=True)
            return
        await state.clear()
        await state.set_state(AdminProductCreateFSM.name)
        await cb.message.edit_text("Введите название товара:")
        await cb.answer()

    @dp.message(AdminProductCreateFSM.name)
    async def adm_prod_create_name(message: Message, state: FSMContext):
        await state.update_data(name=message.text)
        await state.set_state(AdminProductCreateFSM.description)
        await message.answer("Введите описание товара:")

    @dp.message(AdminProductCreateFSM.description)
    async def adm_prod_create_desc(message: Message, state: FSMContext):
        await state.update_data(description=message.text)
        await state.set_state(AdminProductCreateFSM.price)
        await message.answer("Введите цену (только число):")

    @dp.message(AdminProductCreateFSM.price)
    async def adm_prod_create_price(message: Message, state: FSMContext):
        try:
            price = int(message.text)
        except Exception:
            await message.answer("Некорректная цена. Введите число без пробелов:")
            return
        await state.update_data(price=price)
        db = next(get_db())
        try:
            cats = CategoryRepository.get_all_active(db)
        finally:
            db.close()
        ib = InlineKeyboardBuilder()
        for c in cats:
            ib.button(text=c.title, callback_data=f"adm_prod:create_cat:{c.id}")
        ib.adjust(1)
        await state.set_state(AdminProductCreateFSM.category)
        await message.answer("Выберите категорию:", reply_markup=ib.as_markup())

    @dp.callback_query(AdminProductCreateFSM.category, F.data.startswith("adm_prod:create_cat:"))
    async def adm_prod_create_pick_cat(cb: CallbackQuery, state: FSMContext):
        cat_id = int(cb.data.split(":")[2])
        await state.update_data(category_id=cat_id)
        await state.set_state(AdminProductCreateFSM.sizes)
        await cb.message.edit_text("Введите размеры через запятую (напр. S,M,L,XL):")
        await cb.answer()

    @dp.message(AdminProductCreateFSM.sizes)
    async def adm_prod_create_sizes(message: Message, state: FSMContext):
        sizes = [s.strip() for s in message.text.split(",") if s.strip()]
        await state.update_data(sizes=sizes)
        await state.set_state(AdminProductCreateFSM.images)
        await message.answer("Отправьте до 5 фото. Когда закончите — напишите 'Готово'.")

    @dp.message(AdminProductCreateFSM.images, F.photo)
    async def adm_prod_create_images(message: Message, state: FSMContext, bot: Bot):
        data = await state.get_data()
        images = data.get("images", [])
        if len(images) >= 5:
            await message.answer("Максимум 5 фото. Напишите 'Готово'.")
            return
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_path = file.file_path
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"product_{ts}_{len(images)}.jpg"
        save_dir = Path("product_images"); save_dir.mkdir(exist_ok=True)
        save_path = save_dir / fname
        await bot.download_file(file_path, save_path)
        images.append(str(save_path))
        await state.update_data(images=images)
        await message.answer(f"Фото сохранено ({len(images)}/5). Добавьте ещё или напишите 'Готово'.")

    @dp.message(AdminProductCreateFSM.images, F.text.lower() == "готово")
    async def adm_prod_create_preview(message: Message, state: FSMContext):
        data = await state.get_data()
        text = (
            "📋 *Превью товара:*\n"
            f"Название: {data['name']}\n"
            f"Описание: {data['description']}\n"
            f"Цена: {data['price']} ₽\n"
            f"Размеры: {', '.join(data['sizes'])}\n"
            f"Фото: {len(data.get('images', []))}\n\n"
            "Сохранить товар?"
        )
        ib = InlineKeyboardBuilder()
        ib.button(text="✅ Сохранить", callback_data="adm_prod:create_save")
        ib.button(text="❌ Отмена", callback_data="adm_prod:create_cancel")
        ib.adjust(2)
        await state.set_state(AdminProductCreateFSM.confirm)
        await message.answer(text, parse_mode="Markdown", reply_markup=ib.as_markup())

    @dp.callback_query(AdminProductCreateFSM.confirm, F.data == "adm_prod:create_cancel")
    async def adm_prod_create_cancel(cb: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        for p in data.get("images", []):
            try:
                os.remove(p)
            except Exception:
                pass
        await state.clear()
        await cb.message.edit_text("Добавление товара отменено.", reply_markup=admin_products_menu_kb())
        await cb.answer()

    @dp.callback_query(AdminProductCreateFSM.confirm, F.data == "adm_prod:create_save")
    async def adm_prod_create_save(cb: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        db = next(get_db())
        try:
            category = db.query(Category).filter(Category.id == data["category_id"]).first()
            count = db.query(Product).filter(Product.category_id == category.id).count()
            product_code = f"{category.key}_{count + 1:03d}"
            product = Product(
                category_id=category.id,
                product_id=product_code,
                name=data["name"],
                description=data["description"],
                price=data["price"],
                sizes=data["sizes"],
                images=data.get("images", []),
            )
            db.add(product)
            db.commit()
        except Exception as e:
            db.rollback()
            await cb.message.edit_text(f"Ошибка сохранения: {e}")
            await cb.answer()
            return
        finally:
            db.close()
        await state.clear()
        await cb.message.edit_text("✅ Товар сохранён!", reply_markup=admin_products_menu_kb())
        await cb.answer()


    @dp.callback_query(F.data.startswith("adm_prod:list:"))
    async def adm_prod_list(cb: CallbackQuery):
        if cb.from_user.id not in ADMIN_CHAT_IDS:
            await cb.answer("Нет доступа", show_alert=True)
            return
        page = int(cb.data.split(":")[2])
        db = next(get_db())
        try:
            products = db.query(Product).order_by(Product.id.desc()).all()
        finally:
            db.close()
        slice_, total = paginate(products, page, per_page=10)
        if not slice_:
            await cb.answer("Нет товаров", show_alert=True)
            return
        text_lines = ["🗂 *Товары (страница %d)*" % (page + 1)]
        ib = InlineKeyboardBuilder()
        for p in slice_:
            text_lines.append(f"• {p.id}. {p.name} — {p.price} ₽")
            ib.button(text=f"✏️ {p.id}", callback_data=f"adm_prod:edit:{p.id}")
            ib.button(text=f"🗑 {p.id}", callback_data=f"adm_prod:del:{p.id}")
        # Пагинация
        nav = InlineKeyboardBuilder()
        if page > 0:
            nav.button(text="⬅️", callback_data=f"adm_prod:list:{page-1}")
        if (page + 1) * 10 < total:
            nav.button(text="➡️", callback_data=f"adm_prod:list:{page+1}")
        nav.button(text="⬅️ Назад", callback_data="adm:products")
        nav.adjust(2, 1)
        await cb.message.edit_text("\n".join(text_lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[ib.export()[0] if ib.export() else [], *nav.export()]))
        await cb.answer()

    @dp.callback_query(F.data.startswith("adm_prod:del:"))
    async def adm_prod_delete(cb: CallbackQuery):
        if cb.from_user.id not in ADMIN_CHAT_IDS:
            await cb.answer("Нет доступа", show_alert=True)
            return
        pid = int(cb.data.split(":")[2])
        db = next(get_db())
        try:
            product = db.query(Product).filter(Product.id == pid).first()
            if not product:
                await cb.answer("Товар не найден", show_alert=True)
                return
            for p in product.images or []:
                try:
                    os.remove(p)
                except Exception:
                    pass
            db.delete(product)
            db.commit()
        finally:
            db.close()
        await cb.answer("Удалено")
        await adm_prod_list(cb)

    @dp.callback_query(F.data.startswith("adm_prod:edit:"))
    async def adm_prod_edit_menu(cb: CallbackQuery, state: FSMContext):
        if cb.from_user.id not in ADMIN_CHAT_IDS:
            await cb.answer("Нет доступа", show_alert=True)
            return
        pid = int(cb.data.split(":")[2])
        db = next(get_db())
        try:
            product = db.query(Product).filter(Product.id == pid).first()
        finally:
            db.close()
        if not product:
            await cb.answer("Товар не найден", show_alert=True)
            return
        text = (
            f"📦 *{product.name}* (ID {product.id})\n"
            f"Цена: {product.price} ₽\n"
            f"Размеры: {', '.join(product.sizes)}\n"
            f"Категория: {product.category.title}\n"
        )
        ib = InlineKeyboardBuilder()
        ib.button(text="✏️ Название", callback_data=f"adm_prod:edit_field:{pid}:name")
        ib.button(text="📝 Описание", callback_data=f"adm_prod:edit_field:{pid}:description")
        ib.button(text="💰 Цена", callback_data=f"adm_prod:edit_field:{pid}:price")
        ib.button(text="📏 Размеры", callback_data=f"adm_prod:edit_field:{pid}:sizes")
        ib.button(text="🖼 Добавить фото", callback_data=f"adm_prod:add_photo:{pid}")
        ib.button(text="⬅️ К списку", callback_data="adm_prod:list:0")
        ib.adjust(2, 2, 1, 1)
        await state.update_data(edit_product_id=pid)
        await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=ib.as_markup())
        await cb.answer()

    @dp.callback_query(F.data.startswith("adm_prod:edit_field:"))
    async def adm_prod_edit_field(cb: CallbackQuery, state: FSMContext):
        _, _, pid, field = cb.data.split(":")
        await state.update_data(edit_product_id=int(pid), edit_field=field)
        await state.set_state(AdminProductEditFSM.new_value)
        prompts = {
            "name": "Введите новое название:",
            "description": "Введите новое описание:",
            "price": "Введите новую цену (число):",
            "sizes": "Введите новые размеры через запятую:",
        }
        await cb.message.edit_text(prompts.get(field, "Введите значение:"))
        await cb.answer()

    @dp.message(AdminProductEditFSM.new_value)
    async def adm_prod_apply_edit(message: Message, state: FSMContext):
        data = await state.get_data()
        pid = data["edit_product_id"]
        field = data["edit_field"]
        db = next(get_db())
        try:
            product = db.query(Product).filter(Product.id == pid).first()
            if not product:
                await message.answer("Товар не найден.")
                await state.clear()
                return
            if field == "price":
                try:
                    product.price = int(message.text)
                except Exception:
                    await message.answer("Некорректная цена. Попробуйте ещё раз или /cancel")
                    return
            elif field == "sizes":
                product.sizes = [s.strip() for s in message.text.split(",") if s.strip()]
            elif field == "name":
                product.name = message.text
            elif field == "description":
                product.description = message.text
            db.commit()
        finally:
            db.close()
        await message.answer("✅ Сохранено.")
        await state.clear()

    @dp.callback_query(F.data.startswith("adm_prod:add_photo:"))
    async def adm_prod_add_photo_start(cb: CallbackQuery, state: FSMContext):
        pid = int(cb.data.split(":")[2])
        await state.update_data(edit_product_id=pid)
        await state.set_state(AdminProductEditFSM.add_photo)
        await cb.message.edit_text("Отправьте фото для добавления (одно). /cancel — отмена")
        await cb.answer()

    @dp.message(AdminProductEditFSM.add_photo, F.photo)
    async def adm_prod_add_photo(message: Message, state: FSMContext, bot: Bot):
        data = await state.get_data()
        pid = data["edit_product_id"]
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_dir = Path("product_images"); save_dir.mkdir(exist_ok=True)
        fname = f"product_{pid}_{ts}.jpg"
        save_path = save_dir / fname
        await bot.download_file(file.file_path, save_path)
        db = next(get_db())
        try:
            product = db.query(Product).filter(Product.id == pid).first()
            imgs = product.images or []
            imgs.append(str(save_path))
            product.images = imgs
            db.commit()
        finally:
            db.close()
        await message.answer("Фото добавлено ✅")
        await state.clear()

    @dp.callback_query(F.data.startswith("adm_order:list:"))
    async def adm_order_list(cb: CallbackQuery):
        page = int(cb.data.split(":")[2])
        await _render_orders(cb, page=page, status=None)

    @dp.callback_query(F.data.startswith("adm_order:filter:"))
    async def adm_order_filter(cb: CallbackQuery):
        _, _, status, page = cb.data.split(":")
        await _render_orders(cb, page=int(page), status=status)

    async def _render_orders(cb: CallbackQuery, page: int, status: Optional[str]):
        if cb.from_user.id not in ADMIN_CHAT_IDS:
            await cb.answer("Нет доступа", show_alert=True)
            return
        db = next(get_db())
        try:
            q = db.query(Order).order_by(Order.created_at.desc())
            if status:
                q = q.filter(Order.status == status)
            orders = q.all()
        finally:
            db.close()
        slice_, total = paginate(orders, page, per_page=10)
        if not slice_:
            await cb.message.edit_text("Заказы не найдены", reply_markup=admin_orders_menu_kb())
            await cb.answer()
            return
        ib = InlineKeyboardBuilder()
        text_lines = ["🧾 *Список заказов* (стр. %d)" % (page + 1)]
        for o in slice_:
            text_lines.append(
                f"• {o.id} | №{o.order_number} | {o.status} | {o.total_amount} ₽ | {o.created_at.strftime('%d.%m %H:%M')}"
            )
            ib.button(text=f"🔎 {o.id}", callback_data=f"adm_order:view:{o.id}")
        nav = InlineKeyboardBuilder()
        if page > 0:
            nav.button(text="⬅️", callback_data=(f"adm_order:filter:{status}:{page-1}" if status else f"adm_order:list:{page-1}"))
        if (page + 1) * 10 < total:
            nav.button(text="➡️", callback_data=(f"adm_order:filter:{status}:{page+1}" if status else f"adm_order:list:{page+1}"))
        nav.button(text="⬅️ Назад", callback_data="adm:orders")
        nav.adjust(2, 1)
        await cb.message.edit_text("\n".join(text_lines), parse_mode="Markdown",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[ib.export()[0] if ib.export() else [], *nav.export()]))
        await cb.answer()

    @dp.callback_query(F.data.startswith("adm_order:view:"))
    async def adm_order_view(cb: CallbackQuery):
        if cb.from_user.id not in ADMIN_CHAT_IDS:
            await cb.answer("Нет доступа", show_alert=True)
            return
        oid = int(cb.data.split(":")[2])
        db = next(get_db())
        try:
            order = db.query(Order).filter(Order.id == oid).first()
        finally:
            db.close()
        if not order:
            await cb.answer("Заказ не найден", show_alert=True)
            return
        buyer = mention_user(order.user.telegram_id, order.user.username, order.user.first_name, order.user.last_name)
        lines = [
            f"🧾 *Заказ №{order.order_number}*",
            f"Статус: *{order.status}*",
            f"Дата: {order.created_at.strftime('%d.%m.%Y %H:%M')}",
            f"Клиент: {buyer}",
            f"Телефон: {order.phone}",
            f"Итого: {order.total_amount} ₽",
            "",
            "Товары:",
        ]
        for it in order.items:
            lines.append(f"• {it.product_name} — {it.size} × {it.quantity} = {it.total} ₽")
        if order.delivery_type == "cdek":
            lines.append("")
            lines.append("Доставка: CDEK (ПВЗ)")
            lines.append(f"Город: {order.delivery_address.get('city')}")
            lines.append(f"ПВЗ: {order.delivery_address.get('pvz')}")
        else:
            lines.append("")
            lines.append("Доставка: Курьер")
            lines.append(f"Адрес: {order.delivery_address.get('address')}")
        # Кнопки действий
        ib = InlineKeyboardBuilder()
        ib.button(text="✅ Одобрить", callback_data=f"adm_order:set_status:{oid}:confirmed")
        ib.button(text="❌ Отменить", callback_data=f"adm_order:set_status:{oid}:cancelled")
        ib.button(text="🔒 Закрыть", callback_data=f"adm_order:set_status:{oid}:closed")
        ib.button(text="✏️ Изменить статус", callback_data=f"adm_order:status_menu:{oid}")
        ib.button(text="⬅️ К списку", callback_data="adm_order:list:0")
        ib.adjust(3, 1, 1)
        await cb.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=ib.as_markup())
        await cb.answer()

    @dp.callback_query(F.data.startswith("adm_order:status_menu:"))
    async def adm_order_status_menu(cb: CallbackQuery, state: FSMContext):
        oid = int(cb.data.split(":")[2])
        await state.update_data(edit_order_id=oid)
        await state.set_state(AdminOrderEditFSM.waiting_status)
        await cb.message.edit_text("Выберите новый статус:", reply_markup=order_status_kb(oid))
        await cb.answer()

    @dp.callback_query(F.data.startswith("adm_order:set_status:"))
    async def adm_order_set_status(cb: CallbackQuery):
        parts = cb.data.split(":")
        oid = int(parts[2]); new_status = parts[3]
        db = next(get_db())
        try:
            order = db.query(Order).filter(Order.id == oid).first()
            if not order:
                await cb.answer("Заказ не найден", show_alert=True)
                return
            order.status = new_status
            db.commit()
        finally:
            db.close()
        await cb.answer("Статус обновлён")
        await adm_order_view(cb)

    @dp.callback_query(F.data.startswith("adm_sup:list:"))
    async def adm_support_list(cb: CallbackQuery):
        _, _, status, page_str = cb.data.split(":")
        page = int(page_str)
        tickets = [t for t in _load_support() if t.get("status") == status]
        slice_, total = paginate(tickets, page, per_page=10)
        if not slice_:
            await cb.message.edit_text("Заявок нет", reply_markup=admin_support_menu_kb())
            await cb.answer()
            return
        lines = [f"🆘 Заявки ({'открытые' if status=='open' else 'закрытые'}) стр. {page+1}"]
        ib = InlineKeyboardBuilder()
        for t in slice_:
            user_tag = mention_user(t["user_id"], t.get("username"), t.get("first_name"), t.get("last_name"))
            lines.append(f"• {t['id']} | {t['created_at']} | {user_tag}")
            ib.button(text=f"🔎 {t['id']}", callback_data=f"adm_sup:view:{t['id']}")
        nav = InlineKeyboardBuilder()
        if page > 0:
            nav.button(text="⬅️", callback_data=f"adm_sup:list:{status}:{page-1}")
        if (page + 1) * 10 < total:
            nav.button(text="➡️", callback_data=f"adm_sup:list:{status}:{page+1}")
        nav.button(text="⬅️ Назад", callback_data="adm:support")
        nav.adjust(2, 1)
        await cb.message.edit_text("\n".join(lines), parse_mode="Markdown",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[ib.export()[0] if ib.export() else [], *nav.export()]))
        await cb.answer()

    @dp.callback_query(F.data.startswith("adm_sup:view:"))
    async def adm_support_view(cb: CallbackQuery):
        tid = cb.data.split(":")[2]
        tickets = _load_support()
        ticket = next((t for t in tickets if t["id"] == tid), None)
        if not ticket:
            await cb.answer("Тикет не найден", show_alert=True)
            return
        user_tag = mention_user(ticket["user_id"], ticket.get("username"), ticket.get("first_name"), ticket.get("last_name"))
        text = (
            f"🎫 *Тикет {ticket['id']}*\n"
            f"Статус: {ticket['status']}\n"
            f"Создан: {ticket['created_at']}\n"
            f"Автор: {user_tag}\n\n"
            f"Запрос:\n{ticket['text']}"
        )
        await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=ticket_actions_kb(ticket["id"]))
        await cb.answer()

    @dp.callback_query(F.data.startswith("adm_sup:reply:"))
    async def adm_support_reply_start(cb: CallbackQuery, state: FSMContext):
        tid = cb.data.split(":")[2]
        await state.update_data(reply_ticket_id=tid)
        await state.set_state(SupportAdminReplyFSM.waiting_text)
        await cb.message.edit_text("Введите текст ответа пользователю:")
        await cb.answer()

    @dp.message(SupportAdminReplyFSM.waiting_text)
    async def adm_support_reply_send(message: Message, state: FSMContext, bot: Bot):
        data = await state.get_data()
        tid = data["reply_ticket_id"]
        tickets = _load_support()
        ticket = next((t for t in tickets if t["id"] == tid), None)
        if not ticket:
            await message.answer("Тикет не найден.")
            await state.clear()
            return
        try:
            await bot.send_message(ticket["user_id"], f"💬 Ответ поддержки по тикету {tid}:\n\n{message.text}")
        except Exception:
            await message.answer("Не удалось отправить пользователю. Возможно, он не писал боту.")
        await message.answer("Ответ отправлен ✅")
        await state.clear()

    @dp.callback_query(F.data.startswith("adm_sup:close:"))
    async def adm_support_close(cb: CallbackQuery):
        tid = cb.data.split(":")[2]
        tickets = _load_support()
        for t in tickets:
            if t["id"] == tid:
                t["status"] = "closed"
        _save_support(tickets)
        ticket = next((t for t in tickets if t["id"] == tid), None)
        if ticket:
            try:
                await cb.message.bot.send_message(ticket["user_id"], f"🔒 Ваш тикет {tid} закрыт. Если вопрос не решён — создайте новый.")
            except Exception:
                pass
        await cb.message.edit_text(f"Тикет {tid} закрыт.", reply_markup=admin_support_menu_kb())
        await cb.answer()


def register_support(dp: Dispatcher, bot: Bot):
    """Хендлеры для пользовательской техподдержки: команды и кнопки."""

    @dp.message(Command("support"))
    async def user_support_start(message: Message, state: FSMContext):
        await state.set_state(SupportUserFSM.waiting_text)
        await message.answer("Опишите вашу проблему одним сообщением. Мы ответим вам сюда.")


    @dp.message(SupportUserFSM.waiting_text)
    async def user_support_collect(message: Message, state: FSMContext):
        text = message.text
        user = message.from_user
        tid = f"SUP-{datetime.now().strftime('%Y%m%d')}-{str(user.id)[-4:]}-{int(datetime.now().timestamp())%10000:04d}"
        ticket = {
            "id": tid,
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "text": text,
            "status": "open",
            "created_at": datetime.now().strftime('%Y-%m-%d %H:%M'),
        }
        tickets = _load_support()
        tickets.insert(0, ticket)
        _save_support(tickets)
        await message.answer(f"🎫 Ваш запрос зарегистрирован: {tid}. Мы свяжемся с вами здесь.")
        await state.clear()
        user_tag = mention_user(user.id, user.username, user.first_name, user.last_name)
        payload = (
            "🆘 *Новая заявка в техподдержку!*\n"
            f"ID: {tid}\n"
            f"От: {user_tag}\n"
            f"Дата: {ticket['created_at']}\n\n"
            f"Текст:\n{text}"
        )
        for chat_id in ADMIN_CHAT_IDS:
            try:
                await message.bot.send_message(chat_id, payload, parse_mode="Markdown", reply_markup=ticket_actions_kb(tid))
            except Exception:
                pass