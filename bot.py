import asyncio
import json
import os
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Dict

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_IDS_RAW = os.getenv("ADMIN_CHAT_IDS", "").strip()
ADMIN_CHAT_IDS = []
if ADMIN_CHAT_IDS_RAW:
    for x in ADMIN_CHAT_IDS_RAW.split(","):
        x = x.strip()
        if not x:
            continue
        try:
            ADMIN_CHAT_IDS.append(int(x))
        except ValueError:
            pass

if not BOT_TOKEN:
    raise RuntimeError("Не указан BOT_TOKEN в .env")
if not ADMIN_CHAT_IDS:
    print("⚠️ ВНИМАНИЕ: Не указаны ADMIN_CHAT_IDS в .env. Заказы не будут отправляться админам!")
CATALOG: Dict[str, Dict] = {
    "women": {
        "title": "👗 Женская одежда",
        "collage_path": "media/women_collage.jpg", 
        "products": [
            {"id": "w001", "name": "Легинсы мрамор", "price": 3490, "sizes": ["XS", "S", "M", "L"]},
            {"id": "w002", "name": "Блуза мрамор", "price": 1990, "sizes": ["S", "M", "L", "XL"]},
        ]
    },
    "men": {
        "title": "🧥 Мужская одежда",
        "collage_path": "media/men_collage.jpg",
        "products": [
            {"id": "m001", "name": "Футболка", "price": 2990, "sizes": ["S", "M", "L", "XL"]},
            {"id": "m002", "name": "Худи", "price": 4490, "sizes": ["30", "31", "32", "33", "34"]},
        ]
    },
    "acc": {
        "title": "🧢 Аксессуары",
        "collage_path": "media/acc_collage.jpg",
        "products": [
            {"id": "a001", "name": "Кепка", "price": 1290, "sizes": ["one size"]},
            {"id": "a002", "name": "Шапка", "price": 2590, "sizes": ["S", "M", "L"]},
        ]
    }
}

@dataclass
class CartItem:
    product_id: str
    name: str
    size: str
    price: int
    qty: int

    @property
    def total(self) -> int:
        return self.price * self.qty

CARTS: Dict[int, List[CartItem]] = defaultdict(list)

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
    ib = InlineKeyboardBuilder()
    ib.button(text=CATALOG["women"]["title"], callback_data="cat:women")
    ib.button(text=CATALOG["men"]["title"], callback_data="cat:men")
    ib.button(text=CATALOG["acc"]["title"], callback_data="cat:acc")
    ib.adjust(1)
    return ib.as_markup()

def category_products_ikb(cat_key: str) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    for p in CATALOG[cat_key]["products"]:
        ib.button(text=f"{p['name']} — {p['price']} ₽", callback_data=f"prod:{cat_key}:{p['id']}")
    ib.button(text="⬅️ Назад к категориям", callback_data="back:cats")
    ib.adjust(1)
    return ib.as_markup()

def product_sizes_ikb(cat_key: str, product_id: str) -> InlineKeyboardMarkup:
    product = find_product(cat_key, product_id)
    ib = InlineKeyboardBuilder()
    for s in product["sizes"]:
        ib.button(text=s, callback_data=f"size:{cat_key}:{product_id}:{s}")
    ib.button(text="⬅️ Назад к товарам", callback_data=f"back:cat:{cat_key}")
    ib.adjust(4, 1)
    return ib.as_markup()

def qty_ikb(cat_key: str, product_id: str, size: str) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    for q in [1, 2, 3, 4, 5]:
        ib.button(text=str(q), callback_data=f"qty:{cat_key}:{product_id}:{size}:{q}")
    ib.button(text="⬅️ Назад к размерам", callback_data=f"back:size:{cat_key}:{product_id}")
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

def find_product(cat_key: str, product_id: str) -> Dict:
    for p in CATALOG[cat_key]["products"]:
        if p["id"] == product_id:
            return p
    raise KeyError("Товар не найден")

def format_cart(user_id: int) -> str:
    items = CARTS[user_id]
    if not items:
        return "Ваша корзина пуста."
    lines = ["🛒 *Корзина:*"]
    total = 0
    for it in items:
        lines.append(f"• {it.name} — {it.size} × {it.qty} = *{it.total} ₽*")
        total += it.total
    lines.append(f"\nИтого: *{total} ₽*")
    return "\n".join(lines)

def empty_cart(user_id: int):
    CARTS[user_id].clear()

def save_order(order: dict):
    path = "orders.json"
    data = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []
    data.append(order)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

async def notify_admins(bot: Bot, order: dict):
    text_lines = [
        "🆕 *Новый заказ!*",
        f"Дата/время: {order.get('created_at')}",
        f"Покупатель: {order.get('fullname')}",
        f"Телефон: {order.get('phone')}",
        "",
        "Товары:"
    ]
    for it in order.get("items", []):
        text_lines.append(f"• {it['name']} — {it['size']} × {it['qty']} = {it['total']} ₽")
    text_lines.append(f"\nИтого: *{order.get('total')} ₽*")
    text_lines.append("")
    text_lines.append(f"Доставка: {order.get('delivery_type_title')}")
    if order.get("delivery_type") == "cdek":
        text_lines.append(f"Город (CDEK): {order.get('cdek_city')}")
        text_lines.append(f"ПВЗ (код/адрес): {order.get('cdek_pvz')}")
    else:
        text_lines.append(f"Адрес: {order.get('address')}")
    text_lines.append(f"\nUser ID: {order.get('user_id')}")
    payload = "\n".join(text_lines)

    for chat_id in ADMIN_CHAT_IDS:
        try:
            await bot.send_message(chat_id, payload, parse_mode="Markdown")
        except Exception:
            pass

dp = Dispatcher(storage=MemoryStorage())
bot = Bot(token=BOT_TOKEN, parse_mode="Markdown")

@dp.message(CommandStart())
async def on_start(message: Message):
    await message.answer(
        "Привет! Это бот для заказов одежды Эмперадор.\n\nВыбери действие:",
        reply_markup=main_menu_kb()
    )

@dp.message(Command("help"))
@dp.message(F.text == "❓ Помощь")
async def on_help(message: Message):
    await message.answer(
        "Доступные команды:\n"
        "• /start — главное меню\n"
        "• /catalog — открыть каталог\n"
        "• /cart — показать корзину\n"
        "• /checkout — оформить заказ\n"
        "• /cancel — отменить текущее действие\n"
    )

@dp.message(Command("cancel"))
async def on_cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, отменил текущее действие.", reply_markup=main_menu_kb())

@dp.message(Command("catalog"))
@dp.message(F.text == "📸 Каталог")
async def on_catalog(message: Message):
    await message.answer("Выбери категорию:", reply_markup=ReplyKeyboardRemove())
    await message.answer("Категории:", reply_markup=ReplyKeyboardRemove())
    await message.answer(" ", reply_markup=ReplyKeyboardRemove(), reply_markup=None)
    await message.answer("↓ Нажми на нужную категорию", reply_markup=categories_ikb())


@dp.callback_query(F.data == "back:cats")
async def back_to_cats(cb: CallbackQuery):
    await cb.message.answer("Категории:", reply_markup=categories_ikb())
    await cb.answer()

@dp.callback_query(F.data.startswith("back:cat:"))
async def back_to_category(cb: CallbackQuery):
    _, _, cat_key = cb.data.split(":")
    await cb.message.answer(
        "Выбери товар:",
        reply_markup=category_products_ikb(cat_key)
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("prod:"))
async def on_product(cb: CallbackQuery):
    _, cat_key, product_id = cb.data.split(":")
    product = find_product(cat_key, product_id)
    text = f"*{product['name']}*\nЦена: *{product['price']} ₽*\n\nВыберите размер:"
    await cb.message.answer(text, reply_markup=product_sizes_ikb(cat_key, product_id))
    await cb.answer()

@dp.callback_query(F.data.startswith("back:size:"))
async def back_to_sizes(cb: CallbackQuery):
    _, _, cat_key, product_id = cb.data.split(":")
    product = find_product(cat_key, product_id)
    await cb.message.answer(
        f"*{product['name']}* — выберите размер:",
        reply_markup=product_sizes_ikb(cat_key, product_id)
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("size:"))
async def on_size(cb: CallbackQuery):
    _, cat_key, product_id, size = cb.data.split(":")
    product = find_product(cat_key, product_id)
    await cb.message.answer(
        f"{product['name']} — размер *{size}*\nВыберите количество:",
        reply_markup=qty_ikb(cat_key, product_id, size)
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("qty:"))
async def on_qty(cb: CallbackQuery):
    _, cat_key, product_id, size, qty_str = cb.data.split(":")
    qty = int(qty_str)
    product = find_product(cat_key, product_id)
    item = CartItem(
        product_id=product_id,
        name=product["name"],
        size=size,
        price=product["price"],
        qty=qty
    )
    CARTS[cb.from_user.id].append(item)
    await cb.message.answer(
        f"Добавлено: {item.name} — {item.size} × {item.qty} = *{item.total} ₽*\n\n{format_cart(cb.from_user.id)}",
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
    if not CARTS[message.from_user.id]:
        await message.answer("Корзина пуста. Добавьте товары перед оформлением.", reply_markup=main_menu_kb())
        return
    await message.answer("Введите *ФИО* полностью:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(OrderFSM.waiting_fullname)

@dp.message(OrderFSM.waiting_fullname)
async def fsm_fullname(message: Message, state: FSMContext):
    fullname = message.text.strip()
    if len(fullname.split()) < 2:
        await message.answer("Пожалуйста, укажите имя и фамилию (и, при желании, отчество).")
        return
    await state.update_data(fullname=fullname)
    await message.answer("Введите *номер телефона* (пример: +7 999 123-45-67):")
    await state.set_state(OrderFSM.waiting_phone)

@dp.message(OrderFSM.waiting_phone)
async def fsm_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not any(ch.isdigit() for ch in phone):
        await message.answer("Телефон должен содержать цифры. Введите корректный номер:")
        return
    await state.update_data(phone=phone)
    await message.answer(
        "Выберите способ доставки:",
        reply_markup=checkout_delivery_ikb()
    )
    await state.set_state(OrderFSM.waiting_delivery_type)

@dp.callback_query(OrderFSM.waiting_delivery_type, F.data.startswith("delivery:"))
async def fsm_delivery_type(cb: CallbackQuery, state: FSMContext):
    _, dtype = cb.data.split(":")
    await state.update_data(delivery_type=dtype)
    if dtype == "cdek":
        text = (
            "📦 Доставка *CDEK* (ПВЗ)\n"
            "1) Напишите *город получения*.\n"
            "2) Затем отправьте *код ПВЗ* или *адрес ПВЗ*.\n\n"
            "Подсказка: найдите пункт выдачи на карте CDEK и пришлите его код/адрес."
        )
        await cb.message.answer(text)
        await state.set_state(OrderFSM.waiting_cdek_city)
    else:
        await cb.message.answer("🚚 Доставка курьером. Укажите *полный адрес* (город, улица, дом, квартира, подъезд, домофон):")
        await state.set_state(OrderFSM.waiting_address)
    await cb.answer()

@dp.message(OrderFSM.waiting_cdek_city)
async def fsm_cdek_city(message: Message, state: FSMContext):
    city = message.text.strip()
    await state.update_data(cdek_city=city)
    await message.answer("Отлично. Теперь пришлите *код ПВЗ* или *точный адрес ПВЗ*:")
    await state.set_state(OrderFSM.waiting_cdek_pvz)

@dp.message(OrderFSM.waiting_cdek_pvz)
async def fsm_cdek_pvz(message: Message, state: FSMContext):
    pvz = message.text.strip()
    await state.update_data(cdek_pvz=pvz)
    await show_order_preview(message, state)

@dp.message(OrderFSM.waiting_address)
async def fsm_address(message: Message, state: FSMContext):
    address = message.text.strip()
    if len(address) < 8:
        await message.answer("Адрес слишком короткий. Уточните, пожалуйста:")
        return
    await state.update_data(address=address)
    await show_order_preview(message, state)

async def show_order_preview(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()

    items = CARTS[user_id]
    if not items:
        await state.clear()
        await message.answer("Корзина пуста, оформление прервано.", reply_markup=main_menu_kb())
        return

    total = sum(i.total for i in items)
    dtype = data.get("delivery_type")
    dtype_title = "CDEK (ПВЗ)" if dtype == "cdek" else "Курьер до двери"

    lines = ["Проверьте заказ перед подтверждением:", ""]
    lines.append(format_cart(user_id))
    lines.append("")
    lines.append(f"*Доставка:* {dtype_title}")
    if dtype == "cdek":
        lines.append(f"Город: {data.get('cdek_city')}")
        lines.append(f"ПВЗ: {data.get('cdek_pvz')}")
    else:
        lines.append(f"Адрес: {data.get('address')}")
    lines.append("")
    lines.append(f"*ФИО:* {data.get('fullname')}")
    lines.append(f"*Телефон:* {data.get('phone')}")
    lines.append("\nЕсли всё верно — подтвердите заказ.")

    await state.update_data(total=total, delivery_type_title=dtype_title)
    await message.answer("\n".join(lines), reply_markup=confirm_ikb())
    await state.set_state(OrderFSM.confirm)

@dp.callback_query(OrderFSM.confirm, F.data == "confirm:edit_address")
async def edit_address(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("delivery_type") == "cdek":
        await cb.message.answer("Введите *город CDEK* заново:")
        await state.set_state(OrderFSM.waiting_cdek_city)
    else:
        await cb.message.answer("Введите *полный адрес* заново:")
        await state.set_state(OrderFSM.waiting_address)
    await cb.answer()

@dp.callback_query(OrderFSM.confirm, F.data == "confirm:cancel")
async def cancel_order(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Оформление заказа отменено.", reply_markup=main_menu_kb())
    await cb.answer()

@dp.callback_query(OrderFSM.confirm, F.data == "confirm:yes")
async def confirm_order(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    data = await state.get_data()
    items = CARTS[user_id]
    if not items:
        await state.clear()
        await cb.message.answer("Корзина пуста. Нечего подтверждать.", reply_markup=main_menu_kb())
        await cb.answer()
        return

    order_items = []
    for it in items:
        order_items.append({
            "product_id": it.product_id,
            "name": it.name,
            "size": it.size,
            "price": it.price,
            "qty": it.qty,
            "total": it.total
        })

    order = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id,
        "username": cb.from_user.username,
        "fullname": data.get("fullname"),
        "phone": data.get("phone"),
        "delivery_type": data.get("delivery_type"),
        "delivery_type_title": data.get("delivery_type_title"),
        "cdek_city": data.get("cdek_city"),
        "cdek_pvz": data.get("cdek_pvz"),
        "address": data.get("address"),
        "items": order_items,
        "total": data.get("total"),
    }

    save_order(order)
    await notify_admins(cb.message.bot, order)


    await state.clear()
    empty_cart(user_id)

    await cb.message.answer("✅ Заказ принят! Мы свяжемся с вами для подтверждения деталей. Спасибо!",
                            reply_markup=main_menu_kb())
    await cb.answer()

@dp.message(Command("orders"))
async def admin_orders(message: Message):
    if message.from_user.id not in ADMIN_CHAT_IDS:
        await message.answer("Команда доступна только администраторам.")
        return
    path = "orders.json"
    if not os.path.exists(path):
        await message.answer("Пока нет заказов.")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        await message.answer("Не удалось прочитать orders.json")
        return

    if not data:
        await message.answer("Пока нет заказов.")
        return


    last = data[-5:]
    for o in last:
        lines = [
            f"🧾 *Заказ от {o.get('created_at')}*",
            f"Покупатель: {o.get('fullname')}  |  Тел: {o.get('phone')}",
            f"Доставка: {o.get('delivery_type_title')}",
        ]
        if o.get("delivery_type") == "cdek":
            lines.append(f"Город: {o.get('cdek_city')} | ПВЗ: {o.get('cdek_pvz')}")
        else:
            lines.append(f"Адрес: {o.get('address')}")
        lines.append("Товары:")
        for it in o.get("items", []):
            lines.append(f"• {it['name']} — {it['size']} × {it['qty']} = {it['total']} ₽")
        lines.append(f"Итого: *{o.get('total')} ₽*")
        await message.answer("\n".join(lines))
    return


@dp.message(Command("cats"))
async def cats_cmd(message: Message):
    await message.answer("Категории:", reply_markup=categories_ikb())

async def main():
    print("Bot starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped")
