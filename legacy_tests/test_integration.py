"""Real PostgreSQL tests, isolated schemas. Never reads the production DATABASE_URL."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import os
import gzip
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock

import asyncpg
from domain import RuleError, now, quote
from settings import load
from store import Store

TEST_URL = os.getenv('TEST_DATABASE_URL', '')


@unittest.skipUnless(TEST_URL, 'Set TEST_DATABASE_URL for PostgreSQL integration tests')
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_intake_' + uuid.uuid4().hex
        self.settings = replace(load(), database_url=TEST_URL, admin_ids=(1, 3))
        self.store = await Store.connect(self.settings, self.schema)
        self.start = (now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.catalog = {item['code']: item for item in self.settings.content['services']}

    async def asyncTearDown(self):
        await self.store.pool.close()
        db = await asyncpg.connect(TEST_URL)
        try:
            await db.execute(f'DROP SCHEMA {self.schema} CASCADE')
        finally:
            await db.close()

    async def ready(self, actor=2, **changes):
        token = await self.store.begin(actor)
        data = dict(start=self.start.isoformat(), hours=2, guests=4, children=0,
                    cart={'birch': 2}, name='Иван', phone='89812440050', consent=True,
                    rules_hash=self.settings.rules_hash)
        data.update(changes)
        data['quote'] = quote(data, self.catalog, {})
        await self.store.save(actor, await self.store.draft(actor), 'review', data)
        return token

    async def test_submission_is_atomic_and_contains_full_order(self):
        token = await self.ready()
        ident, created = await self.store.submit(2, token, 'ivan')
        self.assertTrue(created)
        row = await self.store.get_order(2, ident)
        self.assertEqual(json.loads(row['data'])['phone'], '+79812440050')
        self.assertEqual(json.loads(row['estimate'])['items'][0]['quantity'], 2)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM customers'), 1)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM deliveries'), 2)
        text = await self.store.pool.fetchval('SELECT text FROM deliveries LIMIT 1')
        self.assertIn('Иван', text)
        self.assertIn('Телефон: +79812440050', text)

    async def test_concurrent_submit_creates_one_order(self):
        token = await self.ready()
        results = await asyncio.gather(*(self.store.submit(2, token) for _ in range(8)))
        self.assertEqual(len({ident for ident, _ in results}), 1)
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM orders'), 1)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM deliveries'), 2)

    async def test_same_time_accepts_requests_without_reserving_it(self):
        one, two = await self.ready(2), await self.ready(4)
        results = await asyncio.gather(self.store.submit(2, one), self.store.submit(4, two))
        self.assertTrue(all(created for _, created in results))
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM orders'), 2)

    async def test_permissions(self):
        token = await self.ready()
        ident, _ = await self.store.submit(2, token)
        with self.assertRaises(RuleError):
            await self.store.get_order(4, ident)
        with self.assertRaises(RuleError):
            await self.store.orders(4, True)
        with self.assertRaises(RuleError):
            await self.store.submit(4, token)
        self.assertEqual((await self.store.get_order(1, ident))['id'], ident)
        self.assertEqual(await self.store.orders(4), ([], False))

    async def test_stale_draft_and_optimistic_lock(self):
        old = await self.ready()
        draft = await self.store.draft(2)
        await self.store.save(2, draft, 'review', draft['data'])
        with self.assertRaises(RuleError):
            await self.store.save(2, draft, 'review', draft['data'])
        await self.store.begin(2)
        with self.assertRaises(RuleError):
            await self.store.submit(2, old)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM orders'), 0)

    async def test_missing_consent_or_updated_rules_blocks_every_write(self):
        for changes in ({'consent': False}, {'rules_hash': 'outdated'}):
            token = await self.ready(**changes)
            with self.assertRaises(RuleError):
                await self.store.submit(2, token)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM customers'), 0)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM orders'), 0)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM deliveries'), 0)

    async def test_changed_price_and_expired_date_block_submission(self):
        token = await self.ready()
        self.settings.content['services'][0]['price'] += 100
        try:
            with self.assertRaises(RuleError):
                await self.store.submit(2, token)
        finally:
            self.settings.content['services'][0]['price'] -= 100
        with self.assertRaises(RuleError):
            await self.store.submit(2, token, clock=self.start)

    async def test_outbox_survives_restart_and_retries(self):
        from bot import deliver_once
        token = await self.ready()
        ident, _ = await self.store.submit(2, token)
        bot = AsyncMock()
        bot.send_message.side_effect = OSError('temporary network failure')
        await deliver_once(bot, self.store)
        pending = await self.store.delivery_status(1)
        self.assertEqual(pending, {'pending': 2, 'errors': 1})
        self.assertEqual((await self.store.get_order(2, ident))['delivery_count'], 0)
        await self.store.pool.close()
        self.store = await Store.connect(self.settings, self.schema)
        await self.store.pool.execute("UPDATE deliveries SET due_at=now()-interval '1 second'")
        bot.send_message.side_effect = None
        await deliver_once(bot, self.store)
        await deliver_once(bot, self.store)
        self.assertFalse(await deliver_once(bot, self.store))
        self.assertEqual((await self.store.get_order(2, ident))['delivery_count'], 2)
        self.assertEqual((await self.store.delivery_status(1))['pending'], 0)

    async def test_queue_workers_claim_different_messages(self):
        token = await self.ready()
        await self.store.submit(2, token)
        rows = await asyncio.gather(self.store.next_delivery(), self.store.next_delivery())
        self.assertNotEqual(rows[0]['id'], rows[1]['id'])
        self.assertIsNone(await self.store.next_delivery())

    async def test_concurrent_cart_updates_are_not_lost(self):
        token = await self.ready()
        d = await self.store.draft(2)
        d['data']['cart'] = {}
        await self.store.save(2, d, 'services', d['data'])
        await asyncio.gather(*(self.store.adjust_service(2, token, 'birch', 1) for _ in range(10)))
        self.assertEqual((await self.store.draft(2))['data']['cart']['birch'], 10)
        await self.store.adjust_service(2, token, 'oak', 1)
        await self.store.adjust_service(2, token, 'oak', -1)
        self.assertNotIn('oak', (await self.store.draft(2))['data']['cart'])

    async def test_queue_failure_rolls_back_order_and_customer(self):
        token = await self.ready()
        await self.store.pool.execute('ALTER TABLE deliveries ADD CONSTRAINT reject_test CHECK(admin_id<0)')
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.store.submit(2, token)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM orders'), 0)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM customers'), 0)
        self.assertEqual((await self.store.draft(2))['state'], 'review')

    async def test_snapshot_includes_order_and_delivery_without_connection_secrets(self):
        from backup import snapshot
        token = await self.ready()
        ident, _ = await self.store.submit(2, token)
        with tempfile.TemporaryDirectory() as folder:
            path = await snapshot(TEST_URL, folder, self.schema)
            with gzip.open(path, 'rt', encoding='utf-8') as handle:
                result = json.load(handle)
            self.assertEqual(result['tables']['orders'][0]['id'], ident)
            self.assertEqual(len(result['tables']['deliveries']), 2)
            self.assertNotIn(TEST_URL, json.dumps(result))

    async def test_postgres_instance_lock_prevents_second_poller(self):
        async with self.store.pool.acquire() as first:
            async with self.store.pool.acquire() as second:
                self.assertTrue(await first.fetchval('SELECT pg_try_advisory_lock(7319842)'))
                try:
                    self.assertFalse(await second.fetchval('SELECT pg_try_advisory_lock(7319842)'))
                finally:
                    await first.execute('SELECT pg_advisory_unlock(7319842)')


if __name__ == '__main__':
    unittest.main()
