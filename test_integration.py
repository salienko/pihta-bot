"""Run on a test DB only. Uses a separate schema; never uses DATABASE_URL."""
import asyncio, os, unittest, uuid
from datetime import timedelta
from pathlib import Path
import asyncpg
from domain import TZ, RuleError, now, price
from store import Store

@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'),'TEST_DATABASE_URL is not set')
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema='test_'+uuid.uuid4().hex
        self.admin=await asyncpg.connect(os.environ['TEST_DATABASE_URL'])
        await self.admin.execute(f'CREATE SCHEMA {self.schema}')
        self.pool=await asyncpg.create_pool(os.environ['TEST_DATABASE_URL'],min_size=1,max_size=5,server_settings={'search_path':self.schema})
        async with self.pool.acquire() as c:
            await c.execute(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
            await c.execute("INSERT INTO staff VALUES(1,'owner')")
            await c.execute("INSERT INTO service_catalog(code,name,price) VALUES('towel','Полотенце',10000)")
        self.db=Store(self.pool)
        self.start=(now()+timedelta(days=1)).replace(hour=10,minute=0,second=0,microsecond=0)
    async def asyncTearDown(self):
        await self.pool.close();await self.admin.execute(f'DROP SCHEMA {self.schema} CASCADE');await self.admin.close()
    def data(self,actor=2):
        return {'start':self.start.isoformat(),'hours':2,'guests':4,'children':0,'name':'Test','phone':'+79990000000','consent':True,'quote':price(self.start,2,4,0)[0],'guest_id':actor}
    async def create(self,actor=2):return await self.db.hold(actor,self.data(actor),'rules','hold:'+str(actor))
    async def test_concurrent_holds(self):
        results=await asyncio.gather(self.create(2),self.create(3),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,str) for x in results),1)
        self.assertEqual(sum(isinstance(x,RuleError) for x in results),1)
    async def test_claim_duplicate_and_receipt(self):
        ident=await self.create();claim=await self.db.claim(2,ident,200000)
        self.assertEqual(claim,await self.db.claim(2,ident,200000))
        await self.db.decide_payment(1,claim,True);await self.db.decide_payment(1,claim,True)
        self.assertEqual((await self.db.totals(ident))[1],200000)
        self.assertEqual((await self.db.get(ident,2))['status'],'confirmed')
    async def test_expire_and_overdue(self):
        ident=await self.create();await self.pool.execute("UPDATE booking SET release_at=now()-interval '1 second' WHERE id=$1",ident)
        await self.db.maintenance();self.assertEqual((await self.db.get(ident,2))['status'],'expired')
        ident=await self.create(3);await self.db.claim(3,ident,200000)
        await self.pool.execute("UPDATE booking SET verification_due=now()-interval '1 second' WHERE id=$1",ident)
        await self.db.maintenance();self.assertEqual((await self.db.get(ident,3))['status'],'review_overdue')
        with self.assertRaises(RuleError):await self.db.available(self.pool,self.start,self.start+timedelta(hours=3))
    async def test_cancel_separate_refund(self):
        ident=await self.create();claim=await self.db.claim(2,ident,200000);await self.db.decide_payment(1,claim,True)
        rid=await self.db.change(2,ident,'cancel')
        with self.assertRaises(RuleError):await self.db.available(self.pool,self.start,self.start+timedelta(hours=3))
        await self.db.decide_change(1,rid,True)
        await self.db.available(self.pool,self.start,self.start+timedelta(hours=3))
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM refund'),1)
    async def test_permissions(self):
        ident=await self.create()
        with self.assertRaises(RuleError):await self.db.get(ident,3)
        claim=await self.db.claim(2,ident,200000)
        with self.assertRaises(RuleError):await self.db.decide_payment(3,claim,True)
    async def test_link_lifecycle(self):
        ident=await self.create();rid=await self.db.request_link(2,ident)
        self.assertEqual(rid,await self.db.request_link(2,ident))
        with self.assertRaises(RuleError):await self.db.send_link(3,rid,'https://example.com/payment')
        await self.db.send_link(1,rid,'https://example.com/payment')
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM outbox WHERE kind='payment_link'"),1)
    async def test_services_do_not_move(self):
        ident=await self.create();await self.db.service(2,ident,'towel',3,'s1');await self.db.service(2,ident,'towel',3,'s1')
        service=await self.pool.fetchval('SELECT id FROM booking_service')
        await self.db.decide_service(1,service,True)
        self.assertEqual((await self.db.totals(ident))[0],self.data()['quote']+30000)
        self.assertEqual((await self.db.get(ident,2))['start_at'].astimezone(TZ),self.start)

    async def test_move_conflict_rolls_back_then_moves_atomically(self):
        ident=await self.create();claim=await self.db.claim(2,ident,200000)
        await self.db.decide_payment(1,claim,True)
        other=self.data(3);other['start']=(self.start+timedelta(hours=3)).isoformat()
        other['quote']=price(self.start+timedelta(hours=3),2,4,0)[0]
        await self.db.hold(3,other,'rules','hold-other')
        request=await self.db.change(2,ident,'move','13:00')
        with self.assertRaises(RuleError):await self.db.decide_change(1,request,True,self.start+timedelta(hours=3))
        self.assertEqual((await self.db.get(ident,2))['start_at'].astimezone(TZ),self.start)
        target=self.start+timedelta(hours=6)
        await self.db.decide_change(1,request,True,target)
        moved=await self.db.get(ident,2)
        self.assertEqual(moved['start_at'].astimezone(TZ),target)
        self.assertEqual(moved['revision'],2)
        await self.db.available(self.pool,self.start,self.start+timedelta(hours=3))

    async def test_payment_link_rejected_after_expiry(self):
        ident=await self.create();rid=await self.db.request_link(2,ident)
        await self.pool.execute("UPDATE booking SET release_at=now()-interval '1 second' WHERE id=$1",ident)
        with self.assertRaises(RuleError):await self.db.send_link(1,rid,'https://example.com/pay')

    async def test_backup_restore_in_empty_schema(self):
        import gzip,json,tempfile
        from backup import snapshot,TABLES
        ident=await self.create();await self.db.claim(2,ident,200000)
        other='restore_'+uuid.uuid4().hex
        await self.admin.execute(f'CREATE SCHEMA {other}')
        try:
            with tempfile.TemporaryDirectory() as folder:
                output=await snapshot(os.environ['TEST_DATABASE_URL'],folder,self.schema)
                with gzip.open(output,'rt',encoding='utf-8') as f:data=json.load(f)
            await self.admin.execute(f'SET search_path TO {other}')
            await self.admin.execute(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
            await self.admin.execute('TRUNCATE bath_resource')
            for table in TABLES:
                await self.admin.execute(f'INSERT INTO {table} SELECT * FROM jsonb_populate_recordset(NULL::{table},$1::jsonb)',json.dumps(data['tables'][table]))
            self.assertEqual(await self.admin.fetchval('SELECT status FROM booking WHERE id=$1',ident),'payment_review')
            self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM payment_claim'),1)
            self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM outbox'),2)
        finally:
            await self.admin.execute('SET search_path TO public')
            await self.admin.execute(f'DROP SCHEMA {other} CASCADE')

if __name__=='__main__':unittest.main()
