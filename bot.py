"""Pihta: polling bot, persistent jobs and authenticated administration."""
import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot,Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.exceptions import TelegramRetryAfter,TelegramForbiddenError
from settings import load
from booking_store import BookingStore
from booking_ui import BookingUI,kb
from booking_domain import rub
from admin_web import make_app
from bot_features import configured_features

async def deliver_once(bot,store):
    row=await store.next_delivery()
    if not row:return False
    try:
        owner=await store.pool.fetchval('SELECT telegram_id FROM bookings WHERE id=$1',row['booking_id'])
        if row['recipient']!=owner and row['recipient'] not in store.settings.admin_ids:
            await store.failed(row['id'],'recipient_access_removed',permanent=True);return True
        if row['event'].startswith('reminder:'):
            current=await store.pool.fetchval('SELECT status FROM bookings WHERE id=$1',row['booking_id'])
            expected='pending' if ':pending:' in row['event'] else 'confirmed'
            if current!=expected:await store.delivered(row['id']);return True
        buttons=[[('Открыть веб-панель','staffweb')]] if row['recipient'] in store.settings.admin_ids else [[('Открыть бронь','open:'+row['booking_id'])]]
        payment=await store.payment_delivery(row['event'])
        if row['event'].startswith(('payreq:','paylink:','payreported:')) and not payment:
            await store.delivered(row['id']);return True
        if payment:
            if row['event'].startswith('paylink:') and payment['url']:
                automatic=bool(await store.pool.fetchval('SELECT 1 FROM paykeeper_invoices WHERE request_id=$1',payment['id']))
                buttons=[[('🔗 Перейти к оплате',payment['url'])],[('Проверить оплату '+rub(payment['amount']) if automatic else '✅ Уже оплатил',('paycheck:' if automatic else 'payreport:')+payment['id'])],
                         [('Открыть бронь','open:'+row['booking_id'])]]
            elif row['recipient'] in store.settings.admin_ids:
                buttons=[[('✅ Получено '+rub(payment['amount']),'paystaff:yes:'+payment['id'])],
                         [('❌ Деньги не поступили','paystaff:no:'+payment['id'])]]+buttons
        if row['event'].startswith(('pkreceipt:','pkreceiptadmin:','pkfinal:','pkfinalstaff:')) or (row['event'].startswith('pkpaid:') and 'доступен по кнопке' in row['text']):
            url=await store.receipt_delivery_url(row['event'])
            if url:buttons=[[('Открыть чек',url)]]+buttons
        sent=await bot.send_message(row['recipient'],row['text'],reply_markup=kb(buttons))
        if payment and not row['event'].startswith('paylink:') and row['recipient'] in store.settings.admin_ids:
            await store.payment_message_delivered(payment['id'],row['recipient'],sent.message_id)
    except TelegramRetryAfter as e:await store.failed(row['id'],'rate_limit',e.retry_after)
    except TelegramForbiddenError:await store.failed(row['id'],'blocked_by_recipient',permanent=True)
    except Exception as e:await store.failed(row['id'],type(e).__name__,min(3600,5*2**min(row['attempts'],9)))
    else:await store.delivered(row['id'])
    return True

async def worker(bot,store):
    last_backup=0
    while True:
        try:
            await store.tick()
            for _ in range(20):
                if not await deliver_once(bot,store):break
            if asyncio.get_running_loop().time()-last_backup>=21600:
                from backup import snapshot
                await snapshot(store.settings.database_url,os.getenv('BACKUP_DIR','/app/data/backups'))
                last_backup=asyncio.get_running_loop().time()
        except Exception as e:logging.error('Background job failed: %s',type(e).__name__)
        await asyncio.sleep(10)

async def provider_worker(store):
    while True:
        try:
            await store.invoice_once()
            await store.settlement_once()
            for _ in range(4):
                if not await store.poll_once():break
            for _ in range(4):
                if not await store.receipt_once():break
        except Exception as e:logging.error('Payment background job failed: %s',type(e).__name__)
        await asyncio.sleep(2)

async def main():
    settings=load()
    if not settings.token or not settings.admin_ids or not settings.database_url:raise SystemExit('Configure BOT_TOKEN, DATABASE_URL and ADMIN_TELEGRAM_IDS or OWNER_TELEGRAM_ID.')
    store=await BookingStore.connect(settings);lock=None;task=None;payment_task=None;runner=None
    try:
        lock=await store.pool.acquire()
        if not await lock.fetchval('SELECT pg_try_advisory_lock(7319842)'):raise SystemExit('Another bot instance is running.')
        features=configured_features(settings,store)
        ui=BookingUI(settings,store,features);dp=Dispatcher(events_isolation=SimpleEventIsolation());features.mount(dp,ui.router)
        async with Bot(settings.token) as bot:
            await bot.get_me()
            await bot.delete_webhook(drop_pending_updates=False)
            await bot.set_my_commands(features.commands())
            if settings.web_url:
                runner=web.AppRunner(make_app(store,settings),access_log=None);await runner.setup()
                await web.TCPSite(runner,'0.0.0.0',int(os.getenv('PORT','8080'))).start()
            task=asyncio.create_task(worker(bot,store))
            if settings.paykeeper_enabled:payment_task=asyncio.create_task(provider_worker(store))
            logging.info('Pihta rental v3 ready; web=%s',bool(settings.web_url))
            await dp.start_polling(bot,allowed_updates=['message','callback_query'])
    finally:
        if payment_task:
            payment_task.cancel()
            try:await payment_task
            except asyncio.CancelledError:pass
        if task:
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
        if runner:await runner.cleanup()
        if lock:
            await lock.execute('SELECT pg_advisory_unlock(7319842)');await store.pool.release(lock)
        await store.pool.close()

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s')
    logging.getLogger('aiogram').setLevel(logging.WARNING)
    try:asyncio.run(main())
    except Exception as e:raise SystemExit('Startup failed: '+type(e).__name__+'. Check configuration and database.') from None
