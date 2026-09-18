# -*- coding: utf-8 -*-
"""
Бот для продажи Telegram Stars ⭐
Цена: 1 звезда = 1.5 руб.

Требования:
    pip install aiogram==3.13.1 python-dotenv

Запуск:
    export BOT_TOKEN="ваш_токен_от_BotFather"
    python stars_bot.py

ВАЖНО (прочитайте перед использованием):
Этот файл даёт готовый интерфейс (меню, кнопки, расчёт цены, приём заявки).
Реального списания денег и реальной отправки звёзд/подарков здесь НЕТ —
для этого нужно подключить платёжный провайдер (Telegram Payments /
ЮKassa / CryptoBot и т.п.) и источник звёзд (например, Fragment API
или ваш поставщик). Место для интеграции отмечено комментарием
"# TODO: PAYMENT INTEGRATION" и "# TODO: STARS DELIVERY".
Без этого бот только формирует заказ и передаёт его администратору
для ручной обработки — это самый простой и часто используемый вариант
для таких ботов.
"""

import asyncio
import logging
import os
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, StateFilter
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

BOT_TOKEN = os.getenv("8632892271:AAGbIzSLqR1jIhXMRDCriFxxzEDu1c8kB44", "8632892271:AAGbIzSLqR1jIhXMRDCriFxxzEDu1c8kB44")
ADMIN_ID = int(os.getenv("8639114682", "8639114682"))  # ваш telegram id для приёма заявок

PRICE_PER_STAR = 1.5  # руб. за 1 звезду
MIN_USERNAME_STARS = 50  # минимум звёзд при покупке "по юзернейму"

# фиолетовая палитра — используем кружки/квадраты как акцент в кнопках
DOT = "🟣"
ACCENT = "💜"

logging.basicConfig(level=logging.INFO)

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
            [InlineKeyboardButton(text=f"{DOT} Цена и условия", callback_data="info")],
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
            [InlineKeyboardButton(text=f"{ACCENT} ✅ Оплатить", callback_data=f"pay_{payload}")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_main")],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_main")]]
    )


# ------------------------------------------------------------------ #
#                                FSM                                  #
# ------------------------------------------------------------------ #


class BuyByUsername(StatesGroup):
    waiting_username = State()
    waiting_amount = State()


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
    await state.clear()
    await call.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb())
    await call.answer()


@router.callback_query(F.data == "info")
async def cb_info(call: CallbackQuery) -> None:
    await call.message.edit_text(INFO_TEXT, reply_markup=main_menu_kb())
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


@router.callback_query(F.data.startswith("pay_"))
async def cb_pay(call: CallbackQuery) -> None:
    payload = call.data.removeprefix("pay_")

    # TODO: PAYMENT INTEGRATION
    # Здесь нужно выставить реальный счёт — например через
    # bot.send_invoice(...) для Telegram Payments, либо создать
    # платёж в ЮKassa/CryptoBot и подождать вебхук/колбэк об оплате.
    # После подтверждения оплаты — вызвать доставку звёзд ниже.

    await call.message.edit_text(
        f"{ACCENT} Заказ принят и передан на обработку.\n\n"
        "Как только оплата подтвердится, звёзды/подарок будут отправлены "
        "автоматически. Если возникнут вопросы — напишите в поддержку.",
    )

    if ADMIN_ID:
        # TODO: STARS DELIVERY
        # После реального подтверждения оплаты здесь вызывается функция,
        # которая покупает и отправляет звёзды/подарок получателю
        # (например, через Fragment API или вашего поставщика звёзд).
        await call.bot.send_message(
            ADMIN_ID,
            f"Новый заказ от @{call.from_user.username or call.from_user.id}\n"
            f"payload: {payload}",
        )

    await call.answer("Заказ отправлен на обработку ✅")


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
