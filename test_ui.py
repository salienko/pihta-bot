import unittest
from unittest.mock import AsyncMock
from aiogram.types import Message, Chat, User, CallbackQuery
from datetime import datetime
import bot

class UITests(unittest.IsolatedAsyncioTestCase):
    def message(self,text,user=2):
        message=Message(message_id=1,date=datetime.now(),chat=Chat(id=user,type='private'),from_user=User(id=user,is_bot=False,first_name='Test'),text=text)
        object.__setattr__(message,'answer',AsyncMock())
        return message
    async def asyncSetUp(self):self.previous=bot.db
    async def asyncTearDown(self):bot.db=self.previous
    async def test_menu_without_database(self):
        bot.db=None
        for text in ['/start','/id','Цены','Правила','О бане','Связаться с нами','Забронировать']:
            with self.subTest(text=text):
                message=self.message(text);await bot.message_handler(message);self.assertTrue(message.answer.called)
    async def test_guests_validation(self):
        bot.db=AsyncMock();bot.db.session.return_value=('guests',{})
        message=self.message('9');await bot.message_handler(message)
        bot.db.set_session.assert_not_called()
        self.assertIn('1 до 8',message.answer.call_args.args[0])
    async def test_children_validation(self):
        bot.db=AsyncMock();bot.db.session.return_value=('children',{'guests':4})
        message=self.message('5');await bot.message_handler(message)
        bot.db.set_session.assert_not_called()
    async def test_phone_validation(self):
        bot.db=AsyncMock();bot.db.session.return_value=('phone',{})
        message=self.message('----------');await bot.message_handler(message)
        bot.db.set_session.assert_not_called()
    async def test_pending_link_input(self):
        bot.db=AsyncMock();bot.db.session.return_value=('sendlink',{'request':'r'})
        bot.db.send_link.return_value='b'
        old=bot.show_order;bot.show_order=AsyncMock()
        try:
            message=self.message('https://example.com/pay',1);await bot.message_handler(message)
            bot.db.send_link.assert_awaited_once_with(1,'r','https://example.com/pay')
        finally:bot.show_order=old
    async def test_callback_size(self):
        for action in ['order','pay','services','sendlink','cancelok','rejectask']:
            markup=bot.kb([[('Test',action+':'+('a'*32))]])
            self.assertLessEqual(len(markup.inline_keyboard[0][0].callback_data.encode()),64)

if __name__=='__main__':unittest.main()
