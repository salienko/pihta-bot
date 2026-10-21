import asyncio
from dataclasses import replace
from datetime import datetime,timedelta
import os
import json
import secrets
import unittest
from unittest.mock import AsyncMock
from aiohttp.test_utils import TestClient,TestServer
from settings import load
from booking_domain import TZ,RuleError,estimate,validate_start,can_cancel
from booking_store import BookingStore
from booking_ui import BookingUI
from admin_web import make_app,money

class Policies(unittest.TestCase):
    def setUp(self):
        self.clock=datetime(2026,10,21,10,tzinfo=TZ);self.settings=load();self.catalog={i['code']:i for i in self.settings.content['services']}
        self.data=dict(start='2026-10-28T13:00:00+03:00',hours=2,guests=6,children=2,cart={})
    def test_child_and_sheet_rules(self):self.assertEqual(estimate(self.data,self.catalog,{})['total'],500000)
    def test_extra_guests(self):self.data['children']=0;self.assertEqual(estimate(self.data,self.catalog,{})['guest_extra'],100000)
    def test_capacity(self):
        self.data['guests']=9
        with self.assertRaises(RuleError):estimate(self.data,self.catalog,{})
    def test_aroma_mutual_exclusion(self):
        self.data['cart']={'eucalyptus':1,'lavender':1}
        with self.assertRaises(RuleError):estimate(self.data,self.catalog,{})
    def test_double_aroma(self):
        self.data['cart']={'lavender':2}
        with self.assertRaises(RuleError):estimate(self.data,self.catalog,{})
    def test_evening(self):self.data['start']='2026-10-28T19:00:00+03:00';self.assertEqual(estimate(self.data,self.catalog,{})['rent'],600000)
    def test_holiday(self):self.assertEqual(estimate(self.data,self.catalog,{'2026-10-28':{'base':650000}})['rent'],650000)
    def test_exact_cancel_boundary(self):self.assertTrue(can_cancel('confirmed',self.clock+timedelta(hours=24),self.clock));self.assertFalse(can_cancel('confirmed',self.clock+timedelta(hours=24)-timedelta(microseconds=1),self.clock))
    def test_pending_cancel_anytime(self):self.assertTrue(can_cancel('pending',self.clock-timedelta(hours=1),self.clock))
    def test_exact_hour(self):validate_start(self.clock+timedelta(hours=3),self.clock+timedelta(hours=2))
    def test_late_submission(self):
        with self.assertRaises(RuleError):validate_start(self.clock,self.clock)
    def test_nonstandard(self):
        with self.assertRaises(RuleError):validate_start(self.clock+timedelta(hours=2),self.clock)
    def test_money(self):self.assertEqual(money('100.50'),10050)
    def test_money_fraction_and_nan(self):
        for v in ['1.001','NaN','Infinity','-1']:
            with self.assertRaises(RuleError):money(v)

URL=os.getenv('TEST_DATABASE_URL','')
@unittest.skipUnless(URL,'TEST_DATABASE_URL is required for PostgreSQL tests')
class Database(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema='test_booking_'+secrets.token_hex(8);self.settings=replace(load(),database_url=URL,admin_ids=(1,),web_url='https://pihta.test')
        self.store=await BookingStore.connect(self.settings,self.schema)
        self.start=(datetime.now(TZ)+timedelta(days=4)).replace(hour=13,minute=0,second=0,microsecond=0)
        self.catalog={i['code']:i for i in self.settings.content['services']}
    async def asyncTearDown(self):
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE');await self.store.pool.close()
    async def ready(self,actor=2,delta=0):
        t=await self.store.begin(actor);d=await self.store.draft(actor,t)
        data=dict(start=(self.start+timedelta(days=delta)).isoformat(),hours=2,guests=2,children=0,cart={'oak':1},name='Иван',phone='+79812440050',consent=True,rules_hash=self.settings.rules_hash)
        data['quote']=estimate(data,self.catalog,{})
        await self.store.save(actor,d,'review',data);return t
    async def book(self,actor=2,delta=0):return (await self.store.submit(actor,await self.ready(actor,delta)))[0]
    async def test_concurrent_slot_only_one_winner(self):
        a,b=await self.ready(2),await self.ready(3)
        results=await asyncio.gather(self.store.submit(2,a),self.store.submit(3,b),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,tuple) for r in results),1);self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM bookings'),1)
    async def test_duplicate_submit(self):
        t=await self.ready();a,b=await asyncio.gather(self.store.submit(2,t),self.store.submit(2,t));self.assertEqual(a[0],b[0]);self.assertNotEqual(a[1],b[1])
    async def test_two_limit(self):
        await self.book(2,0);await self.book(2,1);t=await self.ready(2,2)
        with self.assertRaises(RuleError):await self.store.submit(2,t)
    async def test_same_date(self):
        await self.book();t=await self.ready();d=await self.store.draft(2,t);data=d['data'];data['start']=(self.start+timedelta(hours=3)).isoformat();data['quote']=estimate(data,self.catalog,{});await self.store.save(2,d,'review',data)
        with self.assertRaises(RuleError):await self.store.submit(2,t)
    async def test_cancel_release_and_history(self):
        ident=await self.book();await self.store.transition(2,ident,'cancel');self.assertTrue(await self.store.free(self.start));self.assertEqual((await self.store.get(2,ident))['status'],'cancelled');self.assertEqual(len(await self.store.listing(2,True)),1)
    async def test_confirmed_inside_day_rejects_cancel(self):
        ident=await self.book();await self.store.transition(1,ident,'confirm',staff=True)
        with self.assertRaises(RuleError):await self.store.transition(2,ident,'cancel',clock=self.start-timedelta(hours=23))
    async def test_pending_inside_day_can_cancel(self):ident=await self.book();await self.store.transition(2,ident,'cancel',clock=self.start-timedelta(minutes=30))
    async def test_expire_cannot_confirm(self):
        ident=await self.book()
        with self.assertRaises(RuleError):await self.store.transition(1,ident,'confirm',clock=self.start+timedelta(hours=2),staff=True)
        # Exception rolls back its transaction; the scheduled expiry persists separately.
        async with self.store.tx() as db:await self.store.expire(db,self.start+timedelta(hours=2))
        self.assertEqual((await self.store.get(2,ident))['status'],'expired')
    async def test_confirm_cancel_race_no_revival(self):
        ident=await self.book();await self.store.transition(2,ident,'cancel')
        with self.assertRaises(RuleError):await self.store.transition(1,ident,'confirm',staff=True)
    async def test_other_user_cannot_read_or_cancel(self):
        ident=await self.book()
        with self.assertRaises(RuleError):await self.store.get(3,ident)
        with self.assertRaises(RuleError):await self.store.transition(3,ident,'cancel')
    async def test_admin_reason_required(self):
        ident=await self.book()
        with self.assertRaises(RuleError):await self.store.transition(1,ident,'cancel',staff=True)
    async def test_snapshot_immutable(self):
        ident=await self.book()
        with self.assertRaises(Exception):await self.store.pool.execute("UPDATE bookings SET data='{}' WHERE id=$1",ident)
    async def test_payment_idempotent_and_refund(self):
        ident=await self.book();await self.store.accounting(1,ident,'same','payment',amount=10000,note='наличные');await self.store.accounting(1,ident,'same','payment',amount=10000,note='наличные')
        self.assertEqual((await self.store.get(2,ident))['paid'],10000)
        with self.assertRaises(RuleError):await self.store.accounting(1,ident,'refund','refund',amount=20000,note='возврат')
    async def test_actual_preserves_snapshot_and_minimum(self):
        ident=await self.book();original=(await self.store.get(2,ident))['data']
        with self.assertRaises(RuleError):await self.store.accounting(1,ident,'actual1','actual',guests=1,total=1,note='меньше')
        await self.store.accounting(1,ident,'actual2','actual',guests=3,total=700000,note='фактический расчёт');self.assertEqual((await self.store.get(2,ident))['data'],original)
    async def test_profile_does_not_change_booking(self):
        ident=await self.book();await self.store.profile(2,'Пётр','+79810000000');self.assertEqual((await self.store.get(2,ident))['data']['name'],'Иван')
    async def test_stale_draft_cannot_edit_submitted(self):
        t=await self.ready();d=await self.store.draft(2,t);await self.store.submit(2,t)
        with self.assertRaises(RuleError):await self.store.save(2,d,'services',d['data'])
    async def test_login_one_time_and_role(self):
        t=await self.store.login_token(1);session=await self.store.login(t);self.assertEqual((await self.store.session(session))['actor'],1)
        with self.assertRaises(RuleError):await self.store.login(t)
        with self.assertRaises(RuleError):await self.store.login_token(2)
    async def test_web_security(self):
        client=TestClient(TestServer(make_app(self.store,self.settings,secure=False)));await client.start_server()
        try:
            r=await client.get('/',allow_redirects=False);self.assertEqual(r.status,302)
            token=await self.store.login_token(1);r=await client.post('/login',data={'token':token},headers={'Origin':'https://evil.test'},allow_redirects=False);self.assertEqual(r.status,403)
            session=await self.store.login(token);client.session.cookie_jar.update_cookies({'pihta_session':session})
            r=await client.get('/');self.assertEqual(r.status,200);self.assertIn('no-store',r.headers['Cache-Control']);self.assertIn('frame-ancestors',r.headers['Content-Security-Policy'])
            ident=await self.book();r=await client.post('/booking/'+ident+'/confirm',data={'csrf':'wrong'},headers={'Origin':self.settings.web_url});self.assertEqual(r.status,403)
        finally:await client.close()
    async def test_unavailable_service_rechecked_on_submit(self):
        t=await self.ready();await self.store.pool.execute("INSERT INTO service_settings VALUES('oak',false)")
        with self.assertRaises(RuleError):await self.store.submit(2,t)
    async def test_price_change_requires_new_consent(self):
        t=await self.ready();await self.store.pool.execute('INSERT INTO holiday_rates VALUES($1,$2,$3,$4)',self.start.date(),'Праздник',650000,300000)
        with self.assertRaises(RuleError):await self.store.submit(2,t)
    async def test_cleanup_blocks_overlap_and_exact_boundary_allowed(self):
        ident=await self.book()
        self.assertFalse(await self.store.blocked(self.store.pool,self.start+timedelta(hours=3),self.start+timedelta(hours=5)))
        self.assertTrue(await self.store.blocked(self.store.pool,self.start+timedelta(hours=2),self.start+timedelta(hours=3)))
    async def test_notifications_survive_reconnect(self):
        ident=await self.book();count=await self.store.pool.fetchval('SELECT count(*) FROM outbox')
        other=await BookingStore.connect(self.settings,self.schema)
        try:self.assertEqual(await other.pool.fetchval('SELECT count(*) FROM outbox'),count)
        finally:await other.pool.close()
    async def test_unauthorized_staff_cannot_take_payment(self):
        ident=await self.book()
        with self.assertRaises(RuleError):await self.store.accounting(9,ident,'fake','payment',amount=10000,note='оплата')
    async def test_backup_consistent_and_no_web_tokens(self):
        import tempfile,gzip
        from backup import snapshot
        ident=await self.book();await self.store.login_token(1)
        with tempfile.TemporaryDirectory() as folder:
            path=await snapshot(URL,folder,self.schema)
            with gzip.open(path,'rt',encoding='utf-8') as f:data=json.load(f)
            self.assertEqual(data['tables']['bookings'][0]['id'],ident);self.assertNotIn('web_tokens',data['tables']);self.assertEqual(data['format'],2)
    async def test_message_does_not_edit_booking(self):
        ident=await self.book();original=(await self.store.get(2,ident))['data']
        await self.store.staff_note(1,ident,'note1','Уточните наличие по телефону.')
        await self.store.staff_note(1,ident,'note1','Уточните наличие по телефону.')
        self.assertEqual((await self.store.get(2,ident))['data'],original)
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM outbox WHERE event='note:note1'"),1)
    async def test_full_customer_path(self):
        from types import SimpleNamespace
        ui=BookingUI(self.settings,self.store)
        m=SimpleNamespace(chat=SimpleNamespace(type='private'),from_user=SimpleNamespace(id=2),answer=AsyncMock(),answer_photo=AsyncMock(),photo=None,edit_text=AsyncMock(),edit_caption=AsyncMock(),text='')
        def call(data):return SimpleNamespace(message=m,from_user=SimpleNamespace(id=2),data=data,answer=AsyncMock())
        await ui.callback(call('begin'));d=await self.store.draft(2);t=d['token']
        await ui.callback(call(f'day:{t}:'+self.start.date().isoformat()));await ui.callback(call(f'slot:{t}:13'))
        await ui.callback(call(f'guest:{t}:paid+'));await ui.callback(call(f'next:{t}:name'))
        m.text='Иван';await ui.message(m);m.text='+79812440050';await ui.message(m)
        await ui.callback(call(f'product:{t}:oak'));await ui.callback(call(f'qty:{t}:1'));d=await self.store.draft(2,t);await ui.callback(call(f'add:{t}:'+d['data']['item_nonce']))
        await ui.callback(call(f'review:{t}:'));d=await self.store.draft(2,t);self.assertFalse(d['data']['consent'])
        await ui.callback(call(f'consent:{t}:'));await ui.callback(call(f'submit:{t}:'))
        rows=await self.store.listing(2);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['data']['guests'],2)
        self.assertEqual(rows[0]['data']['cart'],{'oak':1});self.assertEqual(rows[0]['status'],'pending')
        await self.store.transition(1,rows[0]['id'],'confirm',staff=True);await ui.callback(call('cancel:'+rows[0]['id']));self.assertEqual((await self.store.get(2,rows[0]['id']))['status'],'cancelled')
    async def test_ui_product_changes_only_after_add(self):
        t=await self.ready();d=await self.store.draft(2,t);await self.store.save(2,d,'services',d['data']);ui=BookingUI(self.settings,self.store)
        from types import SimpleNamespace
        message=SimpleNamespace(chat=SimpleNamespace(type='private'),answer=AsyncMock(),answer_photo=AsyncMock(),photo=None,edit_text=AsyncMock())
        def call(data):return SimpleNamespace(message=message,from_user=SimpleNamespace(id=2),data=data,answer=AsyncMock())
        await ui.callback(call(f'product:{t}:birch'));await ui.callback(call(f'qty:{t}:1'));d=await self.store.draft(2,t);self.assertNotIn('birch',d['data']['cart'])
        await ui.callback(call(f'add:{t}:'+d['data']['item_nonce']));self.assertEqual((await self.store.draft(2,t))['data']['cart']['birch'],1)
