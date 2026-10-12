"""Telegram-бот семейной бани «Пихта». Запуск: python bot.py."""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.token import TokenValidationError

SITE_URL = "https://banyapihta.ru/"
dp = Dispatcher()
MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="О бане")],
        [KeyboardButton(text="Забронировать")],
        [KeyboardButton(text="Связаться с нами")],
    ],
    resize_keyboard=True,
)


def site_button(text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, url=SITE_URL)]]
    )


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Добро пожаловать в баню «Пихта»! 🌲\n"
        "Выберите раздел в меню.\n" + SITE_URL,
        reply_markup=MENU,
    )


@dp.message(F.text == "О бане")
async def about(message: Message) -> None:
    await message.answer(
        "«Пихта» — семейная русская баня на дровах. 🌲\n"
        "Подробнее о бане, услугах и условиях отдыха — на нашем сайте:\n"
        + SITE_URL,
        reply_markup=site_button("Открыть сайт"),
    )


@dp.message(F.text == "Забронировать")
async def book(message: Message) -> None:
    await message.answer(
        "Чтобы выбрать время и забронировать баню, перейдите на сайт "
        "и воспользуйтесь указанными там способами бронирования.\n"
        + SITE_URL,
        reply_markup=site_button("Перейти к бронированию"),
    )


@dp.message(F.text == "Связаться с нами")
async def contact(message: Message) -> None:
    await message.answer(
        "Актуальные контакты бани «Пихта» указаны на нашем сайте:\n"
        + SITE_URL,
        reply_markup=site_button("Посмотреть контакты"),
    )


@dp.message()
async def fallback(message: Message) -> None:
    await message.answer("Выберите раздел в меню или отправьте /start.", reply_markup=MENU)


async def main() -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Задайте BOT_TOKEN в переменных окружения хостинга.")
    try:
        bot = Bot(token=token)
    except TokenValidationError:
        raise SystemExit("BOT_TOKEN имеет неверный формат. Проверьте его в хостинге.") from None
    try:
        async with bot:
            identity = await bot.get_me()
            await bot.delete_webhook(drop_pending_updates=False)
            await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
            logging.info("Бот @%s готов к работе", identity.username)
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception:
        # Не выводим исключения, которые могут содержать URL с токеном.
        raise SystemExit("Ошибка запуска бота. Проверьте BOT_TOKEN и доступ к Telegram.") from None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # Сообщения библиотеки при сетевых ошибках могут содержать секреты.
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    asyncio.run(main())
