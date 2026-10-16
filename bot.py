"""Telegram interface for collecting and forwarding Pihta orders."""
import asyncio
from datetime import date, datetime, timedelta
import json
import logging
import os

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove)

from domain import (RuleError, SLOTS, TZ, clean_name, horizon, normalize_phone,
                    now, order_card, participants, quote, rub, visit_end)
from settings import load
from store import Store

MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text='Оставить заказ'), KeyboardButton(text='Мои заказы')],
    [KeyboardButton(text='Цены'), KeyboardButton(text='Услуги')],
    [KeyboardButton(text='О бане'), KeyboardButton(text='Правила')],
    [KeyboardButton(text='Как это работает'), KeyboardButton(text='Контакты')],
], resize_keyboard=True)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=value) for text, value in row] for row in rows])


def nav():
    return [[('Главное меню', 'home')]]


class OrderBot:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.catalog = {item['code']: item for item in settings.content['services']}
        self.router = Router()
        self.router.message.register(self.message)
        self.router.callback_query.register(self.callback)

    @staticmethod
    def button(action, token, value=''):
        return f'{action}:{token}:{value}'

    async def say(self, message, text, rows=None, edit=False):
        markup = kb(rows if rows is not None else nav())
        if edit:
            from aiogram.exceptions import TelegramBadRequest
            try:
                await message.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest as exc:
                if 'message is not modified' in str(exc):
                    return
                if not any(reason in str(exc).lower() for reason in ('message to edit not found', "message can't be edited")):
                    raise
        for offset in range(0, len(text), 3500):
            await message.answer(text[offset:offset + 3500], reply_markup=markup if offset + 3500 >= len(text) else None)

    async def home(self, message, actor):
        await message.answer('Добро пожаловать в баню «Пихта»! Здесь можно узнать о бане и оставить заказ администратору.', reply_markup=MENU)
        if actor in self.settings.admin_ids:
            await self.say(message, 'Заказы для обработки: /admin', [[('Открыть заказы', 'admin:0')]])

    async def info(self, message, section):
        content = self.settings.content
        if section == 'О бане':
            text = content['about'] + '\n' + content['site']
        elif section == 'Контакты':
            text = 'Администратор: ' + content['phone'] + '\n' + content['site']
        elif section == 'Правила':
            text = self.settings.rules
        elif section == 'Услуги':
            text = 'Услуги к посещению:\n' + '\n'.join(f'• {item["name"]}: {rub(item["price"])}' for item in self.catalog.values())
            text += '\nУкажите нужные позиции в заказе. Доступность уточнит администратор.'
        elif section == 'Цены':
            text = ('Первые 2 часа / дополнительный час:\n'
                    'Будни до 19:00: 5000 / 2300 ₽\n'
                    'Будни с 19:00 и выходные: 6000 / 2800 ₽\n'
                    'Праздники: 6500 / 3000 ₽, если дата отмечена специальным тарифом.\n'
                    'Включены 4 платных гостя; сверх — 250 ₽/ч с человека. Дети до 6 лет включительно без доплаты, но входят в лимит 8 гостей.\n'
                    'При длительном посещении стоимость рассчитывается по часам с учётом перехода через 19:00. Итог в заявке предварительный.')
            holidays = content.get('holidays', {})
            if holidays:
                text += '\nСпециальные даты:\n' + '\n'.join(
                    f'{day}: {rate["label"]} — {rub(rate["base"])}/{rub(rate["extra"])}'
                    for day, rate in sorted(holidays.items()) if day >= now().date().isoformat())
        else:
            text = ('Выберите желаемую дату, сеанс и продолжительность, укажите гостей и услуги, имя и телефон. '
                    'Проверьте заказ и отправьте его. Бот сохранит информацию и передаст администратору. '
                    'Администратор свяжется с вами, уточнит свободное время и детали. '
                    'Отправка заказа сама по себе не закрепляет время.')
        await self.say(message, text)

    async def dates(self, message, actor, token, offset=0):
        d = await self.store.draft(actor, token)
        if d['state'] != 'date':
            raise RuleError('Откройте выбор даты в текущем оформлении.')
        today = now().date()
        count = (horizon(today) - today).days + 1
        offset = min(max(0, offset), max(0, ((count - 1) // 7) * 7))
        rows = [[((today + timedelta(days=i)).strftime('%d.%m.%Y'),
                  self.button('day', token, (today + timedelta(days=i)).isoformat()))]
                for i in range(offset, min(offset + 7, count))]
        paging = []
        if offset:
            paging.append(('← Раньше', self.button('dates', token, str(offset - 7))))
        if offset + 7 < count:
            paging.append(('Позже →', self.button('dates', token, str(offset + 7))))
        if paging:
            rows.append(paging)
        await self.say(message, 'На какую дату хотите оставить заказ? Время уточнит администратор.', rows + nav())

    async def slots(self, message, actor, token):
        d = await self.store.draft(actor, token)
        day = date.fromisoformat(d['data']['day'])
        rows = []
        for hour in SLOTS:
            start = datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=hour)
            try:
                visit_end(start, 2)
            except RuleError:
                continue
            rows.append([(f'{hour:02}:00–{(hour + 2) % 24:02}:00', self.button('slot', token, str(hour)))])
        await self.say(message, 'Выберите желаемый сеанс. Доступность проверит администратор.' if rows else 'На эту дату срок подачи заявки уже прошёл.',
                       rows + [[('Другие даты', self.button('back', token, 'date'))]] + nav())

    async def duration(self, message, actor, token):
        d = await self.store.draft(actor, token)
        rows = []
        for hours in (2, 3, 4, 5):
            try:
                visit_end(datetime.fromisoformat(d['data']['start']), hours)
            except RuleError:
                continue
            rows.append([(f'{hours} ч.' + (' — стандартный сеанс' if hours == 2 else ' — по согласованию'),
                          self.button('hours', token, str(hours)))])
        await self.say(message, 'Какую продолжительность указать в заказе?', rows + [[('Назад к сеансам', self.button('back', token, 'slot'))]] + nav())

    async def services(self, message, actor, token, edit=False):
        d = await self.store.draft(actor, token)
        rows = []
        for code, item in self.catalog.items():
            quantity = d['data'].get('cart', {}).get(code, 0)
            rows.append([(f'{item["name"]}: {rub(item["price"])} · {quantity} шт.', self.button('plus', token, code))])
            if quantity:
                rows.append([('−', self.button('minus', token, code)), ('+', self.button('plus', token, code))])
        rows.extend([[('Продолжить', self.button('contacts', token))],
                     [('Назад к участникам', self.button('back', token, 'guests'))]])
        total = sum(self.catalog[code]['price'] * value for code, value in d['data'].get('cart', {}).items())
        await self.say(message, 'Выберите услуги или продолжите без них. Нажатие на название добавляет одну единицу. '
                       'Кнопка «−» уменьшает количество и удаляет позицию при нуле.\nВыбрано услуг на ' + rub(total), rows + nav(), edit)

    async def phone(self, message, actor, d):
        await self.store.save(actor, d, 'phone', d['data'])
        await message.answer('Поделитесь своим номером кнопкой или введите номер для связи.',
                             reply_markup=ReplyKeyboardMarkup(keyboard=[
                                 [KeyboardButton(text='Поделиться номером', request_contact=True)],
                                 [KeyboardButton(text='Главное меню')]], resize_keyboard=True, one_time_keyboard=True))

    async def review(self, message, actor, token):
        d = await self.store.draft(actor, token)
        if d['state'] not in ('review', 'phone_confirm'):
            raise RuleError('Сначала заполните контакты.')
        data = d['data']
        estimate = quote(data, self.catalog, self.settings.content.get('holidays', {}))
        if data.get('quote') != estimate or data.get('rules_hash') != self.settings.rules_hash:
            data['consent'] = False
        data.update(quote=estimate, rules_hash=self.settings.rules_hash)
        await self.store.save(actor, d, 'review', data)
        text = order_card('черновик', data, estimate)
        text += '\n\nПроверьте данные. Имя, телефон и состав заказа будут сохранены и переданы администратору для обработки.'
        rows = [[('Прочитать правила', 'info:Правила')],
                [(('☑' if data.get('consent') else '□') + ' С правилами ознакомлен, согласен', self.button('consent', token))],
                [('Отправить заказ администратору', self.button('submit', token))],
                [('Изменить услуги', self.button('back', token, 'services')),
                 ('Изменить контакты', self.button('back', token, 'name'))]]
        await self.say(message, text, rows + nav())

    async def show_order(self, message, actor, ident):
        row = await self.store.get_order(actor, ident)
        text = order_card(ident, json.loads(row['data']), json.loads(row['estimate']), actor in self.settings.admin_ids)
        text += '\nЗаказ сохранён: ' + row['created_at'].astimezone(TZ).strftime('%d.%m.%Y %H:%M')
        text += '\nПередача: ' + ('карточка принята Telegram для администратора.' if row['delivery_count'] else 'ожидает отправки администратору.')
        await self.say(message, text)

    async def order_list(self, message, actor, admin=False, offset=0):
        rows, more = await self.store.orders(actor, admin, offset)
        buttons = []
        for row in rows:
            data = json.loads(row['data'])
            start = datetime.fromisoformat(data['start'])
            buttons.append([(f'№{row["id"]} · {start:%d.%m %H:%M}', 'order:' + row['id'])])
        action = 'admin' if admin else 'orders'
        paging = []
        if offset:
            paging.append(('← Назад', f'{action}:{max(0, offset - 10)}'))
        if more:
            paging.append(('Далее →', f'{action}:{offset + 10}'))
        if paging:
            buttons.append(paging)
        text = ('Заказы для обработки' if admin else 'Ваши отправленные заказы') if rows else 'Заказов пока нет.'
        if admin:
            status = await self.store.delivery_status(actor)
            text += f'\nОжидают доставки: {status["pending"]}; из них с ошибкой последней попытки: {status["errors"]}.'
        await self.say(message, text, buttons + nav())

    async def message(self, message: Message):
        if message.chat.type != 'private' or not message.from_user:
            return
        actor = message.from_user.id
        text = (message.text or '').strip()
        command = text.split('@')[0] if text.startswith('/') else text
        try:
            if command in ('/start', '/cancel', 'Главное меню'):
                await self.home(message, actor)
                return
            if command == '/id':
                await self.say(message, f'Ваш Telegram ID: {actor}')
                return
            if text in ('О бане', 'Цены', 'Услуги', 'Правила', 'Контакты', 'Как это работает'):
                await self.info(message, text)
                return
            if text == 'Оставить заказ':
                if not self.settings.admin_ids:
                    raise RuleError('Приём заказов пока настраивается. Администратор: ' + self.settings.content['phone'])
                token = await self.store.begin(actor)
                await self.dates(message, actor, token)
                return
            if text == 'Мои заказы':
                await self.order_list(message, actor)
                return
            if command == '/admin':
                await self.order_list(message, actor, True)
                return
            d = await self.store.draft(actor)
            data, state = d['data'], d['state']
            if state == 'guests':
                guests = int(text)
                participants(guests, 0)
                data['guests'] = guests
                await self.store.save(actor, d, 'children', data)
                await self.say(message, 'Сколько среди них детей до 6 лет включительно? Введите 0, если детей нет.',
                               [[('Назад', self.button('back', d['token'], 'guests'))]] + nav())
            elif state == 'children':
                children = int(text)
                participants(data['guests'], children)
                data['children'] = children
                await self.store.save(actor, d, 'services', data)
                await self.services(message, actor, d['token'])
            elif state == 'name':
                data['name'] = clean_name(text)
                await self.store.save(actor, d, state, data)
                await self.phone(message, actor, await self.store.draft(actor, d['token']))
            elif state == 'phone':
                if message.contact and message.contact.user_id != actor:
                    raise RuleError('Нужен ваш собственный контакт, а не контакт другого человека.')
                data['phone'] = normalize_phone(message.contact.phone_number if message.contact else text)
                await self.store.save(actor, d, 'phone_confirm', data)
                await message.answer('Номер получен.', reply_markup=ReplyKeyboardRemove())
                await self.say(message, 'Использовать номер ' + data['phone'] + '?',
                               [[('Да', self.button('phoneyes', d['token'])), ('Другой номер', self.button('phoneother', d['token']))]] + nav())
            elif state == 'review':
                await self.review(message, actor, d['token'])
            elif state == 'submitted':
                await self.say(message, 'Этот заказ уже сохранён. Откройте «Мои заказы» или начните новый.', [[('Мои заказы', 'orders:0')]] + nav())
            else:
                await self.say(message, 'Продолжите оформление кнопками или выберите «Оставить заказ» в главном меню.')
        except RuleError as exc:
            await self.say(message, str(exc))
        except ValueError:
            await self.say(message, 'Проверьте ввод. Введите целое число для количества.')
        except Exception as exc:
            logging.error('Order input error: %s', type(exc).__name__)
            await self.say(message, 'Не удалось завершить действие. Сохранённые заказы доступны в «Мои заказы». Связь: ' + self.settings.content['phone'])

    async def callback(self, call: CallbackQuery):
        if not isinstance(call.message, Message) or call.message.chat.type != 'private':
            await call.answer('Откройте /start в личном чате с ботом.')
            return
        await call.answer()
        message, actor = call.message, call.from_user.id
        parts = (call.data or '').split(':', 2)
        action = parts[0]
        try:
            if action == 'home':
                await self.home(message, actor)
                return
            if action == 'info':
                await self.info(message, parts[1])
                return
            if action in ('orders', 'admin'):
                await self.order_list(message, actor, action == 'admin', max(0, int(parts[1])))
                return
            if action == 'order':
                await self.show_order(message, actor, parts[1])
                return
            _, token, value = parts
            # A submitted draft can be replayed safely even after a later draft was started.
            if action == 'submit':
                ident, created = await self.store.submit(actor, token, call.from_user.username or '')
                await self.say(message, ('Заказ сохранён и ожидает передачи администратору. Администратор свяжется с вами, чтобы уточнить время и детали.'
                                        if created else 'Этот заказ уже сохранён; повторная заявка не создавалась.') + '\n№' + ident,
                               [[('Посмотреть заказ', 'order:' + ident)]] + nav())
                return
            d = await self.store.draft(actor, token)
            data, state = d['data'], d['state']
            if action == 'dates' and state == 'date':
                await self.dates(message, actor, token, int(value))
            elif action == 'day' and state == 'date':
                day = date.fromisoformat(value)
                if not now().date() <= day <= horizon(now().date()):
                    raise RuleError('Выберите дату в пределах двух месяцев.')
                data.update(day=day.isoformat(), consent=False)
                await self.store.save(actor, d, 'slot', data)
                await self.slots(message, actor, token)
            elif action == 'slot' and state == 'slot':
                if int(value) not in SLOTS:
                    raise RuleError('Выберите один из предложенных сеансов.')
                start = datetime.combine(date.fromisoformat(data['day']), datetime.min.time(), TZ) + timedelta(hours=int(value))
                visit_end(start, 2)
                data.update(start=start.isoformat(), consent=False)
                await self.store.save(actor, d, 'duration', data)
                await self.duration(message, actor, token)
            elif action == 'hours' and state == 'duration':
                hours = int(value)
                visit_end(datetime.fromisoformat(data['start']), hours)
                data.update(hours=hours, consent=False)
                await self.store.save(actor, d, 'guests', data)
                await self.say(message, 'Сколько всего гостей, включая детей? Введите число от 1 до 8.',
                               [[('Назад', self.button('back', token, 'duration'))]] + nav())
            elif action in ('plus', 'minus'):
                await self.store.adjust_service(actor, token, value, 1 if action == 'plus' else -1)
                await self.services(message, actor, token, edit=True)
            elif action == 'contacts' and state == 'services':
                await self.store.save(actor, d, 'name', data)
                await self.say(message, 'Укажите Ваше имя для брони', [[('Назад к услугам', self.button('back', token, 'services'))]] + nav())
            elif action == 'phoneother' and state == 'phone_confirm':
                await self.phone(message, actor, d)
            elif action == 'phoneyes' and state == 'phone_confirm':
                await self.review(message, actor, token)
            elif action == 'consent' and state == 'review':
                data['consent'] = not data.get('consent', False)
                await self.store.save(actor, d, state, data)
                await self.review(message, actor, token)
            elif action == 'back' and state != 'submitted':
                required = {'date': (), 'slot': ('day',), 'duration': ('start',), 'guests': ('hours',),
                            'services': ('guests', 'children'), 'name': ('guests', 'children')}
                if value not in required or any(key not in data for key in required[value]):
                    raise RuleError('Сначала заполните предыдущие шаги.')
                data['consent'] = False
                await self.store.save(actor, d, value, data)
                if value == 'date':
                    await self.dates(message, actor, token)
                elif value == 'slot':
                    await self.slots(message, actor, token)
                elif value == 'duration':
                    await self.duration(message, actor, token)
                elif value == 'services':
                    await self.services(message, actor, token)
                else:
                    await self.say(message, 'Введите общее число гостей от 1 до 8.' if value == 'guests' else 'Укажите Ваше имя для брони')
            else:
                raise RuleError('Эта кнопка уже обработана. Продолжите текущий заказ или начните новый.')
        except (RuleError, ValueError, IndexError) as exc:
            await self.say(message, str(exc) if isinstance(exc, RuleError) else 'Кнопка устарела. Откройте главное меню.')
        except Exception as exc:
            logging.error('Order callback error: %s', type(exc).__name__)
            await self.say(message, 'Не удалось завершить действие. Проверьте «Мои заказы». Связь: ' + self.settings.content['phone'])


async def deliver_once(bot, store):
    row = await store.next_delivery()
    if not row:
        return False
    try:
        await bot.send_message(row['admin_id'], row['text'], reply_markup=kb([[('Открыть заказ', 'order:' + row['order_id'])]]))
    except TelegramRetryAfter as exc:
        await store.failed(row['id'], 'TelegramRetryAfter', exc.retry_after)
    except Exception as exc:
        await store.failed(row['id'], type(exc).__name__)
        logging.warning('Order delivery postponed: %s', type(exc).__name__)
    else:
        await store.delivered(row['id'])
    return True


async def delivery_worker(bot, store):
    while True:
        try:
            for _ in range(20):
                if not await deliver_once(bot, store):
                    break
        except Exception as exc:
            logging.error('Delivery queue error: %s', type(exc).__name__)
        await asyncio.sleep(3)


async def main():
    settings = load()
    if not settings.token:
        raise SystemExit('Задайте BOT_TOKEN в переменных окружения.')
    if not settings.admin_ids:
        raise SystemExit('Задайте ADMIN_TELEGRAM_IDS. Каждый администратор должен открыть /start в боте.')
    store = await Store.connect(settings)
    application = OrderBot(settings, store)
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.include_router(application.router)
    task = None
    lock = None
    try:
        lock = await store.pool.acquire()
        if not await lock.fetchval('SELECT pg_try_advisory_lock(7319842)'):
            raise SystemExit('Другой экземпляр бота уже работает с этой базой. Сначала остановите его.')
        async with Bot(settings.token) as bot:
            await bot.get_me()
            await bot.delete_webhook(drop_pending_updates=False)
            await bot.set_my_commands([
                BotCommand(command='start', description='Главное меню'),
                BotCommand(command='id', description='Мой Telegram ID'),
                BotCommand(command='admin', description='Заказы для администратора')])
            task = asyncio.create_task(delivery_worker(bot, store))
            logging.info('Order intake bot is ready')
            await dispatcher.start_polling(bot)
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if lock:
            await lock.execute('SELECT pg_advisory_unlock(7319842)')
            await store.pool.release(lock)
        await store.pool.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    logging.getLogger('aiogram').setLevel(logging.CRITICAL)
    try:
        asyncio.run(main())
    except Exception as exc:
        raise SystemExit('Бот остановлен: ' + type(exc).__name__ + '. Проверьте настройки и доступ к базе.') from None
