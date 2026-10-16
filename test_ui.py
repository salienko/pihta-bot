"""Offline Telegram dispatch tests. PostgreSQL uses a disposable test schema."""
from dataclasses import replace
from datetime import timedelta
import json
import os
import unittest
import uuid

import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Contact, Message, Update, User

from bot import OrderBot, deliver_once
from domain import now
from settings import load
from store import Store


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(message_id=len(self.calls), date=now(),
                           chat=Chat(id=int(method.chat_id), type='private'),
                           from_user=User(id=42, is_bot=True, first_name='Пихта'),
                           text=method.text).as_(bot)
        return True

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b''


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL for dispatcher tests')
class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_ui_' + uuid.uuid4().hex
        self.settings = replace(load(), database_url=os.environ['TEST_DATABASE_URL'], admin_ids=(1,))
        self.store = await Store.connect(self.settings, self.schema)
        self.session = RecordingSession()
        self.bot = Bot('42:TEST_TOKEN_FOR_OFFLINE_TESTS', session=self.session)
        self.app = OrderBot(self.settings, self.store)
        self.dp = Dispatcher(events_isolation=SimpleEventIsolation())
        self.dp.include_router(self.app.router)
        self.number = 0

    async def asyncTearDown(self):
        await self.bot.session.close()
        await self.store.pool.close()
        db = await asyncpg.connect(os.environ['TEST_DATABASE_URL'])
        try:
            await db.execute(f'DROP SCHEMA {self.schema} CASCADE')
        finally:
            await db.close()

    async def send(self, text='', callback=None, actor=2, contact=None):
        self.number += 1
        user = User(id=actor, is_bot=False, first_name='Иван', username='ivan')
        message = Message(message_id=self.number, date=now(), chat=Chat(id=actor, type='private'),
                          from_user=user, text=text or None, contact=contact)
        if callback:
            update = Update(update_id=self.number, callback_query=CallbackQuery(
                id=str(self.number), from_user=user, chat_instance='test', message=message, data=callback))
        else:
            update = Update(update_id=self.number, message=message)
        await self.dp.feed_update(self.bot, update)

    def texts(self):
        return '\n'.join(method.text for method in self.session.calls if isinstance(method, (SendMessage, EditMessageText)))

    async def to_services(self):
        await self.send('Оставить заказ')
        token = (await self.store.draft(2))['token']
        day = (now() + timedelta(days=1)).date().isoformat()
        for action, value in (('day', day), ('slot', '10'), ('hours', '2')):
            await self.send(callback=self.app.button(action, token, value))
        await self.send('8')
        await self.send('2')
        self.assertEqual((await self.store.draft(2))['state'], 'services')
        return token

    async def to_review(self):
        token = await self.to_services()
        await self.send(callback=self.app.button('plus', token, 'birch'))
        await self.send(callback=self.app.button('plus', token, 'birch'))
        await self.send(callback=self.app.button('contacts', token))
        await self.send('Иван Иванов')
        await self.send('8 (981) 244-00-50')
        await self.send(callback=self.app.button('phoneyes', token))
        self.assertEqual((await self.store.draft(2))['state'], 'review')
        return token

    async def test_full_guest_flow_admin_delivery_and_duplicate_click(self):
        await self.send('/start')
        token = await self.to_review()
        await self.send(callback=self.app.button('consent', token))
        await self.send(callback=self.app.button('submit', token))
        await self.send(callback=self.app.button('submit', token))
        self.assertIn('Заказ сохранён и ожидает передачи', self.texts())
        self.assertIn('повторная заявка не создавалась', self.texts())
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM orders'), 1)
        await deliver_once(self.bot, self.store)
        admin_messages = [m for m in self.session.calls if isinstance(m, SendMessage) and m.chat_id == 1]
        self.assertEqual(len(admin_messages), 1)
        self.assertIn('Иван Иванов', admin_messages[0].text)
        self.assertIn('Веник берёзовый × 2', admin_messages[0].text)
        await self.send('/admin', actor=1)
        self.assertIn('Заказы для обработки', self.texts())
        for method in self.session.calls:
            markup = getattr(method, 'reply_markup', None)
            if hasattr(markup, 'inline_keyboard'):
                for row in markup.inline_keyboard:
                    for button in row:
                        self.assertLessEqual(len(button.callback_data.encode()), 64)

    async def test_consent_required_and_contact_ownership(self):
        token = await self.to_services()
        await self.send(callback=self.app.button('contacts', token))
        await self.send('Иван')
        await self.send(contact=Contact(phone_number='89812440050', first_name='Другой', user_id=99))
        self.assertEqual((await self.store.draft(2))['state'], 'phone')
        self.assertIn('ваш собственный контакт', self.texts())
        await self.send(contact=Contact(phone_number='89812440050', first_name='Иван', user_id=2))
        await self.send(callback=self.app.button('phoneyes', token))
        await self.send(callback=self.app.button('submit', token))
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM orders'), 0)
        self.assertIn('подтвердите согласие', self.texts())

    async def test_info_does_not_need_draft_and_has_no_payment_actions(self):
        for text in ('О бане', 'Цены', 'Услуги', 'Правила', 'Контакты', 'Как это работает'):
            await self.send(text)
        await self.send('/admin', actor=2)
        self.assertIn('Нет доступа', self.texts())
        self.assertIn('Баня на дровах', self.texts())
        for method in self.session.calls:
            markup = getattr(method, 'reply_markup', None)
            if hasattr(markup, 'inline_keyboard'):
                for row in markup.inline_keyboard:
                    self.assertFalse(any('оплат' in b.text.lower() for b in row))

    async def test_old_button_cannot_change_new_order_and_forged_slot_rejected(self):
        token = await self.to_services()
        await self.send('Оставить заказ')
        await self.send(callback=self.app.button('plus', token, 'birch'))
        self.assertIn('из старого заказа', self.texts())
        token = (await self.store.draft(2))['token']
        day = (now() + timedelta(days=1)).date().isoformat()
        await self.send(callback=self.app.button('day', token, day))
        await self.send(callback=self.app.button('slot', token, '34'))
        self.assertEqual((await self.store.draft(2))['state'], 'slot')


if __name__ == '__main__':
    unittest.main()
