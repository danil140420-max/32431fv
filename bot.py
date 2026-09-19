# -*- coding: utf-8 -*-
"""
Бот для продажи Telegram Stars ⭐
Цена: 1 звезда = 1.5 руб.

Требования:
    pip install aiogram==3.13.1 python-dotenv

Запуск:
    export BOT_TOKEN="ваш_токен_от_BotFather"
    export ADMIN_ID="ваш_telegram_id"       # куда приходят чеки на проверку
    export CARD_NUMBER="0000 0000 0000 0000"
    export CARD_HOLDER="IVAN IVANOV"
    export CARD_BANK="Т-Банк"
    python stars_bot.py

КАК РАБОТАЕТ ОПЛАТА:
1. Покупатель выбирает заказ → бот показывает реквизиты карты и номер заказа.
2. Покупатель переводит деньги и присылает в бот скриншот/фото чека.
3. Чек с деталями заказа уходит администратору (ADMIN_ID) с кнопками
   "Подтвердить" / "Отклонить".
4. После подтверждения покупатель получает уведомление, и именно в этот
   момент нужно реально отправить звёзды/подарок — место отмечено
   комментарием "# TODO: STARS DELIVERY" (например, через Fragment API
   или вашего поставщика звёзд).

Заказы хранятся в памяти процесса (словарь ORDERS) — при перезапуске
бота они теряются. Для продакшена стоит перенести это в базу данных
(sqlite/postgres), особенно на Railway, где процесс может перезапускаться.
"""

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ------------------------------------------------------------------ #
#                              НАСТРОЙКИ                              #
# ------------------------------------------------------------------ #

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # ваш telegram id для приёма заявок

PRICE_PER_STAR = 1.5  # руб. за 1 звезду
MIN_USERNAME_STARS = 50  # минимум звёзд при покупке "по юзернейму"

# реквизиты для перевода — впишите свои
CARD_NUMBER = os.getenv("CARD_NUMBER", "0000 0000 0000 0000")
CARD_HOLDER = os.getenv("CARD_HOLDER", "IVAN IVANOV")
CARD_BANK = os.getenv("CARD_BANK", "Т-Банк")

ORDER_TIMEOUT_MINUTES = int(os.getenv("ORDER_TIMEOUT_MINUTES", "30"))

# фиолетовая палитра — используем кружки/квадраты как акцент в кнопках
DOT = "🟣"
ACCENT = "💜"

logging.basicConfig(level=logging.INFO)

# ------------------------------------------------------------------ #
#                      ХРАНИЛИЩЕ ЗАКАЗОВ (в памяти)                  #
# ------------------------------------------------------------------ #
# Для продакшена лучше вынести в БД (sqlite/postgres) — при перезапуске
# процесса этот словарь очищается.


@dataclass
class Order:
    order_id: str
    buyer_id: int
    buyer_chat_id: int
    buyer_username: Optional[str]
    description: str  # что покупают (подарок / юзернейм + кол-во звёзд)
    stars: int
    total_rub: float
    status: str = "awaiting_receipt"  # awaiting_receipt -> pending_review -> confirmed/rejected/expired/cancelled
    created_at: datetime = field(default_factory=datetime.now)


ORDERS: dict[str, Order] = {}
ORDER_TIMEOUT_TASKS: dict[str, asyncio.Task] = {}

STATUS_LABELS = {
    "awaiting_receipt": "⏳ Ожидает оплаты",
    "pending_review": "🔍 Проверяется",
    "confirmed": "✅ Выполнен",
    "rejected": "❌ Отклонён",
    "expired": "⌛ Истёк срок",
    "cancelled": "🚫 Отменён",
}

# ------------------------------------------------------------------ #
#                         ПОДАРОЧНЫЕ ПАКЕТЫ                          #
# ------------------------------------------------------------------ #


@dataclass
class GiftOption:
    key: str
    title: str
    stars: int


GIFT_CATALOG = [
    GiftOption("bear", "🧸 Мишка", 15),
    GiftOption("heart", "❤️ Сердце", 15),
    GiftOption("rose", "🌹 Роза", 25),
    GiftOption("gift", "🎁 Подарок", 25),
    GiftOption("rocket", "🚀 Ракета", 50),
    GiftOption("cake", "🎂 Торт", 50),
    GiftOption("diamond", "💎 Алмаз", 100),
    GiftOption("ring", "💍 Кольцо", 100),
]
GIFTS_BY_KEY = {g.key: g for g in GIFT_CATALOG}


def price_for(stars: int) -> float:
    return round(stars * PRICE_PER_STAR, 2)


# ------------------------------------------------------------------ #
#                         КЛАВИАТУРЫ (ИНЛАЙН)                         #
# ------------------------------------------------------------------ #


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{DOT} Купить звёзды ⭐", callback_data="buy_stars")],
            [InlineKeyboardButton(text=f"{DOT} 📦 Мои заказы", callback_data="my_orders")],
            [InlineKeyboardButton(text=f"{DOT} Цена и условия", callback_data="info")],
            [InlineKeyboardButton(text=f"{DOT} ❓ FAQ", callback_data="faq")],
        ]
    )


def buy_mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{ACCENT} 🎁 Подарком", callback_data="mode_gift")],
            [InlineKeyboardButton(text=f"{ACCENT} 👤 По юзернейму", callback_data="mode_username")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]
    )


def gift_menu_kb() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for g in GIFT_CATALOG:
        row.append(
            InlineKeyboardButton(
                text=f"{DOT} {g.title} — {g.stars}⭐",
                callback_data=f"gift_{g.key}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="buy_stars")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb(payload: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{ACCENT} 💳 Перейти к оплате", callback_data=f"pay_{payload}")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_main")],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_main")]]
    )


def receipt_wait_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отменить заказ", callback_data="back_main")]]
    )


def admin_review_kb(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_ok_{order_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_no_{order_id}"),
            ]
        ]
    )


# ------------------------------------------------------------------ #
#                                FSM                                  #
# ------------------------------------------------------------------ #


class BuyByUsername(StatesGroup):
    waiting_username = State()
    waiting_amount = State()


class ReceiptWait(StatesGroup):
    waiting_receipt = State()


# ------------------------------------------------------------------ #
#                              ТЕКСТЫ                                 #
# ------------------------------------------------------------------ #

WELCOME_TEXT = (
    f"{ACCENT} <b>Магазин Telegram Stars</b> {ACCENT}\n\n"
    f"Цена: <b>1 ⭐ = {PRICE_PER_STAR} ₽</b>\n\n"
    "Выберите действие в меню ниже 👇"
)

INFO_TEXT = (
    f"{ACCENT} <b>Цена и условия</b>\n\n"
    f"— 1 звезда = {PRICE_PER_STAR} ₽\n"
    f"— Покупка подарком: фиксированные наборы (см. раздел «Подарком»)\n"
    f"— Покупка по юзернейму: от {MIN_USERNAME_STARS} звёзд\n"
    "— После оплаты звёзды/подарок зачисляются в течение нескольких минут"
)

FAQ_TEXT = (
    f"{ACCENT} <b>Частые вопросы</b>\n\n"
    "<b>Сколько ждать зачисления после оплаты?</b>\n"
    "Обычно до 15–30 минут после подтверждения чека администратором.\n\n"
    "<b>Что делать, если чек не приняли?</b>\n"
    "Проверьте, что скриншот читаемый и сумма совпадает с заказом, "
    "и отправьте чек заново через /start.\n\n"
    f"<b>Что если не успел оплатить вовремя?</b>\n"
    f"Заказ автоматически отменяется через {ORDER_TIMEOUT_MINUTES} мин. ожидания "
    "— просто оформите новый через /start.\n\n"
    "<b>Куда смотреть статус заказа?</b>\n"
    "В разделе «📦 Мои заказы» в главном меню.\n\n"
    "<b>Можно ли вернуть деньги?</b>\n"
    "Если оплата не была подтверждена и звёзды не зачислены — напишите "
    "администратору, средства вернём."
)

router = Router()


# ------------------------------------------------------------------ #
#                              ХЕНДЛЕРЫ                               #
# ------------------------------------------------------------------ #


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    order_id = data.get("order_id")
    if order_id:
        order = ORDERS.get(order_id)
        if order and order.status == "awaiting_receipt":
            order.status = "cancelled"
        task = ORDER_TIMEOUT_TASKS.pop(order_id, None)
        if task:
            task.cancel()

    await state.clear()
    await call.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb())
    await call.answer()


@router.callback_query(F.data == "info")
async def cb_info(call: CallbackQuery) -> None:
    await call.message.edit_text(INFO_TEXT, reply_markup=main_menu_kb())
    await call.answer()


@router.callback_query(F.data == "faq")
async def cb_faq(call: CallbackQuery) -> None:
    await call.message.edit_text(FAQ_TEXT, reply_markup=main_menu_kb())
    await call.answer()


@router.callback_query(F.data == "my_orders")
async def cb_my_orders(call: CallbackQuery) -> None:
    user_orders = sorted(
        (o for o in ORDERS.values() if o.buyer_id == call.from_user.id),
        key=lambda o: o.created_at,
        reverse=True,
    )[:15]

    if not user_orders:
        text = f"{ACCENT} У вас пока нет заказов.\n\nОформите первый через «Купить звёзды»."
    else:
        lines = [f"{ACCENT} <b>Ваши последние заказы</b>\n"]
        for o in user_orders:
            label = STATUS_LABELS.get(o.status, o.status)
            lines.append(
                f"<code>{o.order_id}</code> — {o.description}\n"
                f"{o.stars}⭐ · {o.total_rub}₽ · {label}\n"
                f"{o.created_at.strftime('%d.%m %H:%M')}\n"
            )
        text = "\n".join(lines)

    await call.message.edit_text(text, reply_markup=main_menu_kb())
    await call.answer()


@router.callback_query(F.data == "buy_stars")
async def cb_buy_stars(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(
        f"{ACCENT} <b>Как хотите купить звёзды?</b>",
        reply_markup=buy_mode_kb(),
    )
    await call.answer()


# ---------------------- Покупка подарком ---------------------- #


@router.callback_query(F.data == "mode_gift")
async def cb_mode_gift(call: CallbackQuery) -> None:
    await call.message.edit_text(
        f"{ACCENT} <b>Выберите подарок</b>\n\nЦена = количество ⭐ × {PRICE_PER_STAR} ₽",
        reply_markup=gift_menu_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("gift_"))
async def cb_gift_selected(call: CallbackQuery) -> None:
    key = call.data.removeprefix("gift_")
    gift = GIFTS_BY_KEY.get(key)
    if not gift:
        await call.answer("Такого подарка нет", show_alert=True)
        return

    total = price_for(gift.stars)
    text = (
        f"{ACCENT} <b>Ваш заказ</b>\n\n"
        f"Подарок: {gift.title}\n"
        f"Звёзд: {gift.stars} ⭐\n"
        f"К оплате: <b>{total} ₽</b>\n\n"
        "Получатель — вы сами. Подтвердите оплату."
    )
    await call.message.edit_text(text, reply_markup=confirm_kb(f"gift_{gift.key}"))
    await call.answer()


# -------------------- Покупка по юзернейму -------------------- #


@router.callback_query(F.data == "mode_username")
async def cb_mode_username(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BuyByUsername.waiting_username)
    await call.message.edit_text(
        f"{ACCENT} <b>Покупка по юзернейму</b>\n\n"
        f"Минимум: {MIN_USERNAME_STARS} ⭐\n\n"
        "Введите юзернейм получателя (например, @username):",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(StateFilter(BuyByUsername.waiting_username))
async def msg_get_username(message: Message, state: FSMContext) -> None:
    username = message.text.strip()
    if not username.startswith("@") or len(username) < 4:
        await message.answer(
            "Похоже, это не юзернейм. Введите в формате @username:",
            reply_markup=cancel_kb(),
        )
        return

    await state.update_data(username=username)
    await state.set_state(BuyByUsername.waiting_amount)
    await message.answer(
        f"Юзернейм: {username}\n\n"
        f"Сколько звёзд купить? (минимум {MIN_USERNAME_STARS})",
        reply_markup=cancel_kb(),
    )


@router.message(StateFilter(BuyByUsername.waiting_amount))
async def msg_get_amount(message: Message, state: FSMContext) -> None:
    raw = message.text.strip().replace(" ", "")
    if not raw.isdigit():
        await message.answer("Введите число, например 50", reply_markup=cancel_kb())
        return

    amount = int(raw)
    if amount < MIN_USERNAME_STARS:
        await message.answer(
            f"Минимум для покупки по юзернейму — {MIN_USERNAME_STARS} ⭐. Введите число ещё раз:",
            reply_markup=cancel_kb(),
        )
        return

    data = await state.get_data()
    username = data["username"]
    total = price_for(amount)

    text = (
        f"{ACCENT} <b>Ваш заказ</b>\n\n"
        f"Получатель: {username}\n"
        f"Звёзд: {amount} ⭐\n"
        f"К оплате: <b>{total} ₽</b>\n\n"
        "Подтвердите оплату."
    )
    payload = f"user_{username.lstrip('@')}_{amount}"
    await message.answer(text, reply_markup=confirm_kb(payload))
    await state.clear()


# ------------------------------ Оплата ------------------------------ #


async def expire_order(order_id: str, bot: Bot) -> None:
    """Автоматически отменяет заказ, если чек не пришёл вовремя."""
    await asyncio.sleep(ORDER_TIMEOUT_MINUTES * 60)

    order = ORDERS.get(order_id)
    if not order or order.status != "awaiting_receipt":
        return  # уже оплачен/отменён/чек отправлен

    order.status = "expired"
    ORDER_TIMEOUT_TASKS.pop(order_id, None)

    try:
        await bot.send_message(
            order.buyer_chat_id,
            f"{ACCENT} Заказ <code>{order.order_id}</code> отменён — "
            f"истекло время ожидания оплаты ({ORDER_TIMEOUT_MINUTES} мин).\n\n"
            "Оформите новый заказ через /start.",
        )
    except Exception:
        logging.exception("Не удалось уведомить покупателя об истёкшем заказе %s", order_id)


def _parse_payload(payload: str) -> tuple[str, int]:
    """Возвращает (описание заказа, количество звёзд) по payload из confirm_kb."""
    if payload.startswith("gift_"):
        key = payload[len("gift_") :]
        gift = GIFTS_BY_KEY[key]
        return f"Подарок: {gift.title}", gift.stars

    if payload.startswith("user_"):
        rest = payload[len("user_") :]
        username, amount_str = rest.rsplit("_", 1)
        return f"По юзернейму: @{username}", int(amount_str)

    raise ValueError(f"Неизвестный payload: {payload}")


@router.callback_query(F.data.startswith("pay_"))
async def cb_pay(call: CallbackQuery, state: FSMContext) -> None:
    payload = call.data.removeprefix("pay_")
    description, stars = _parse_payload(payload)
    total = price_for(stars)

    order_id = uuid.uuid4().hex[:8]
    order = Order(
        order_id=order_id,
        buyer_id=call.from_user.id,
        buyer_chat_id=call.message.chat.id,
        buyer_username=call.from_user.username,
        description=description,
        stars=stars,
        total_rub=total,
    )
    ORDERS[order_id] = order
    ORDER_TIMEOUT_TASKS[order_id] = asyncio.create_task(expire_order(order_id, call.bot))

    await state.set_state(ReceiptWait.waiting_receipt)
    await state.update_data(order_id=order_id)

    text = (
        f"{ACCENT} <b>Реквизиты для перевода</b>\n\n"
        f"Заказ: {description}\n"
        f"Сумма: <b>{total} ₽</b>\n\n"
        f"💳 Карта: <code>{CARD_NUMBER}</code>\n"
        f"👤 Получатель: {CARD_HOLDER}\n"
        f"🏦 Банк: {CARD_BANK}\n\n"
        f"Номер заказа: <code>{order_id}</code>\n\n"
        f"⏱ Заказ отменится автоматически, если не оплатить в течение "
        f"{ORDER_TIMEOUT_MINUTES} мин.\n\n"
        "После перевода отправьте сюда <b>скриншот или фото чека</b> — "
        "заявка уйдёт на проверку."
    )
    await call.message.edit_text(text, reply_markup=receipt_wait_kb())
    await call.answer()


@router.message(StateFilter(ReceiptWait.waiting_receipt), F.photo | F.document)
async def msg_receipt_received(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    order_id = data.get("order_id")
    order = ORDERS.get(order_id)

    if not order:
        await message.answer("Заказ не найден, начните заново из главного меню /start")
        await state.clear()
        return

    if order.status == "expired":
        await message.answer(
            f"{ACCENT} Этот заказ уже отменён по истечении времени ожидания.\n"
            "Оформите новый через /start.",
        )
        await state.clear()
        return

    task = ORDER_TIMEOUT_TASKS.pop(order_id, None)
    if task:
        task.cancel()

    order.status = "pending_review"

    caption = (
        f"{ACCENT} <b>Новый чек на проверку</b>\n\n"
        f"Заказ: <code>{order.order_id}</code>\n"
        f"От: @{order.buyer_username or order.buyer_id}\n"
        f"Детали: {order.description}\n"
        f"Звёзд: {order.stars} ⭐\n"
        f"Сумма: {order.total_rub} ₽"
    )

    if ADMIN_ID:
        if message.photo:
            await message.bot.send_photo(
                ADMIN_ID,
                photo=message.photo[-1].file_id,
                caption=caption,
                reply_markup=admin_review_kb(order.order_id),
            )
        else:
            await message.bot.send_document(
                ADMIN_ID,
                document=message.document.file_id,
                caption=caption,
                reply_markup=admin_review_kb(order.order_id),
            )

    await message.answer(
        f"{ACCENT} Чек получен ✅\n\n"
        "Заявка отправлена на проверку. Как только оплата подтвердится, "
        "звёзды/подарок будут отправлены.",
    )
    await state.clear()


@router.message(StateFilter(ReceiptWait.waiting_receipt))
async def msg_receipt_wrong_type(message: Message) -> None:
    await message.answer(
        "Пришлите, пожалуйста, скриншот или фото чека об оплате.",
        reply_markup=receipt_wait_kb(),
    )


# --------------------------- Проверка админом ------------------------- #


@router.callback_query(F.data.startswith("adm_ok_"))
async def cb_admin_confirm(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("Недостаточно прав", show_alert=True)
        return

    order_id = call.data.removeprefix("adm_ok_")
    order = ORDERS.get(order_id)
    if not order:
        await call.answer("Заказ не найден", show_alert=True)
        return

    order.status = "confirmed"

    # TODO: STARS DELIVERY
    # Здесь вызывается функция, которая реально покупает и отправляет
    # звёзды/подарок получателю (например, через Fragment API или
    # вашего поставщика звёзд), используя order.description / order.stars.

    await call.bot.send_message(
        order.buyer_chat_id,
        f"{ACCENT} Оплата по заказу <code>{order.order_id}</code> подтверждена ✅\n"
        "Звёзды/подарок будут зачислены в ближайшее время.",
    )
    await call.message.edit_caption(
        caption=call.message.caption + "\n\n✅ ПОДТВЕРЖДЕНО",
        reply_markup=None,
    )
    await call.answer("Подтверждено")


@router.callback_query(F.data.startswith("adm_no_"))
async def cb_admin_reject(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("Недостаточно прав", show_alert=True)
        return

    order_id = call.data.removeprefix("adm_no_")
    order = ORDERS.get(order_id)
    if not order:
        await call.answer("Заказ не найден", show_alert=True)
        return

    order.status = "rejected"

    await call.bot.send_message(
        order.buyer_chat_id,
        f"{ACCENT} По заказу <code>{order.order_id}</code> оплата не подтверждена ❌\n"
        "Свяжитесь с поддержкой или отправьте корректный чек ещё раз через /start.",
    )
    await call.message.edit_caption(
        caption=call.message.caption + "\n\n❌ ОТКЛОНЕНО",
        reply_markup=None,
    )
    await call.answer("Отклонено")


# ------------------------- Админ: /orders ------------------------- #

ORDERS_FILTERS = {
    "pending": ("pending_review",),
    "awaiting": ("awaiting_receipt",),
    "confirmed": ("confirmed",),
    "rejected": ("rejected",),
    "expired": ("expired",),
    "cancelled": ("cancelled",),
    "all": None,
}


@router.message(Command("orders"))
async def cmd_orders(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return  # обычным пользователям команда не отвечает

    args = message.text.split(maxsplit=1)
    filter_key = args[1].strip().lower() if len(args) > 1 else "pending"

    if filter_key not in ORDERS_FILTERS:
        await message.answer(
            "Неизвестный фильтр. Доступные: "
            + ", ".join(f"<code>{k}</code>" for k in ORDERS_FILTERS)
        )
        return

    statuses = ORDERS_FILTERS[filter_key]
    orders = sorted(
        (o for o in ORDERS.values() if statuses is None or o.status in statuses),
        key=lambda o: o.created_at,
        reverse=True,
    )[:30]

    if not orders:
        await message.answer(f"{ACCENT} Заказов по фильтру «{filter_key}» нет.")
        return

    lines = [f"{ACCENT} <b>Заказы</b> — фильтр: {filter_key} (последние {len(orders)})\n"]
    for o in orders:
        label = STATUS_LABELS.get(o.status, o.status)
        lines.append(
            f"<code>{o.order_id}</code> · @{o.buyer_username or o.buyer_id}\n"
            f"{o.description} · {o.stars}⭐ · {o.total_rub}₽ · {label}\n"
            f"{o.created_at.strftime('%d.%m %H:%M')}\n"
        )

    # Telegram режет длинные сообщения — разбиваем блоками
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk)


# ------------------------------------------------------------------ #
#                                MAIN                                 #
# ------------------------------------------------------------------ #


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
