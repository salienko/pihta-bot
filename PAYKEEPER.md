# PayKeeper: implementation and activation

This release is for local review. Do not activate it on the existing bot until the owner authorises deployment and the fiscal parameters are agreed.

## Current temporary decision (25 October 2026)

The owner deferred settlement receipts and requested the existing merchant behaviour for now: one incoming-payment receipt with `full`, and a Telegram receipt link for each payment. `.env.example` documents this temporary profile (`none` tax, all three invoice types `full`) without enabling PayKeeper. This is an owner-selected temporary operation, not a conclusion of fiscal compliance for advance payments. No production settings were changed.

`PAYKEEPER_SETTLEMENT_ENABLED=0` is the default. It hides settlement forms and blocks creation, reconciliation and background submission/status work. Enable it only after separately resolving fiscal configuration and actual device/API compatibility. Existing settlement records, if any, are retained; turning this switch off is not a cancellation of a fiscal request already submitted to the provider. The settlement section below describes the deferred feature, not the current active mode.

## Behaviour

- Submitting a booking does not create an invoice. A guest requests deposit, full or balance payment from their booking card.
- No email prompt is shown in Telegram. The saved booking phone is sent as `client_phone`.
- Every invoice has an explicit receipt cart with item **Оплата аренды бани «Пихта»**, one unit and the requested amount. Technical IDs are not used as item names. The signed `orderid` identifies the immutable local invoice request.
- The invoice POST is preceded by a durable database record. Repeated clicks reuse the same pending request. A token-fetch failure can be retried; an interrupted invoice POST is marked uncertain and requires reconciliation, never an automatic second POST.
- The signed PayKeeper callback is `POST /payments/paykeeper/notify`. It checks the signature and the locally expected client, amount and request. No payment or receipt is created by this handler.
- A unique payment ledger, booking update, audit entry and notification outbox are committed in one database transaction under the booking lock. `OK` is returned after commit. Replays of the same payment do not add money again. Another payment, conflicting manual accounting or payment for a closed booking is stored for review without restoring a booking.
- Unrelated signed payments from the same merchant are stored in `paykeeper_unmatched`, acknowledged and reported to administrators. They do not affect any booking.
- A durable 20-minute fallback poll and the guest's **Проверить оплату** button use the same ledger as the callback. Guest checks have an ownership check and a 15-second throttle.
- Expiry of an invoice does not release a booking. Existing end-of-session expiry is preserved.
- Receipt search uses the actual payment ID, filters by exact `payment_id`, successful sale receipt, amount and receipt type, then constructs a URL from the trusted merchant origin and returned key. It does not require a receipt number copied from an administrator's browser.
- Guest notifications either combine payment acceptance and a ready receipt, or send acceptance immediately and an explanatory receipt message later. Messages and receipts survive restarts. Normal Bot API delivery has a residual duplicate-message window after an ambiguous Telegram response; this does not duplicate money.
- Receipts are visible only in the corresponding Telegram owner's booking card or an authenticated staff session. A phone number alone never grants access.
- Staff can create invoices in **Счета и чеки** with a phone, arbitrary positive amount, explicit fiscal payment type, optional email and optional booking. A standalone invoice is not assigned to a guest by phone. Staff copy the payment link and send it themselves. No WhatsApp/MAX integration or automatic external message is performed.
- Settlement receipts require an explicit authenticated staff action after the service ends; elapsed time alone never triggers fiscalisation. Correction/refund fiscal procedures are not implemented.

## Booking settlement

The booking card has **Услуга оказана — оформить итоговый чек**, with an explicit checkbox confirming actual service. It requires exact full payment by linked PayKeeper prepayments and their initial receipts. Cancelled bookings, no-shows, early submission, manual money, refunds, disputed payments and legacy `full` receipts are blocked. A separate `paykeeper_settlements` record snapshots the amount, tax, phone, source payment IDs and stable random receipt key; include it in backups. Accounting edits are blocked once requested and need a separate fiscal reconciliation procedure.

The API request creates a sale receipt with `payment_type=full`, the whole amount in `sum_advance`, and zero new cash/cashless money. It uses empty `payment_id` and relates multiple payments locally to the booking. The response is polled after two minutes; guest notification and history link appear only on validated success. The provider receipt key, contact, item, amount, tax and payment methods are checked. A lost submission response or interrupted sending becomes **Нужна сверка**, never a new receipt key or blind resubmission. Staff can attach an existing receipt ID only after API validation. Fiscal failure requires manual investigation; timeout continues status checks because fiscalisation may complete later.

This path has only been tested with a mocked provider and disposable PostgreSQL. Merchant/API-user permission, actual fiscal device compatibility and uncertain-result recovery must be verified with PayKeeper before activation. The merchant's time-based auto-settlement must not run in parallel. Configure new booking invoices as partial prepayment for deposit/balance and full prepayment for one-shot full price; this does not rewrite legacy receipts. Arbitrary standalone staff invoices are excluded from booking settlement.

## Configuration (hosting environment, never Git or chat)

Keep `PAYKEEPER_ENABLED=0` until activation is authorised. When enabled, all of these must be set:

```
PAYKEEPER_ENABLED=1
PAYKEEPER_URL=https://YOUR-MERCHANT.server.paykeeper.ru
PAYKEEPER_USER=PihtaBot
PAYKEEPER_PASSWORD=<set privately>
PAYKEEPER_SECRET=<set privately; at least 16 characters>
PAYKEEPER_TAX=<approved tax code>
PAYKEEPER_DEPOSIT_TYPE=<approved payment_type>
PAYKEEPER_FULL_TYPE=<approved payment_type>
PAYKEEPER_BALANCE_TYPE=<approved payment_type>
```

`WEB_BASE_URL` must be the already configured HTTPS origin of this bot's web server. Fiscal tax and payment type have no implicit enabled defaults. The merchant's existing global fiscal settings are never edited by this code. Agree the tax and `full` / `prepay` / `part_prepay` / `advance` choice for each payment scenario before enabling it; an item name does not replace those fields.

The PayKeeper account used during verification has administrator privileges off, refunds 0, and **Только выставление счетов** off. Removing that invoice-only restriction made the existing payment/receipt readable. Its broader read access is a deliberate owner-approved change; keep credentials private.

## Migration and backups

New tables: `paykeeper_invoices`, `paykeeper_payments`, `paykeeper_fiscal_receipts`, `paykeeper_unmatched`. Existing booking snapshots, sums and manual payment history are preserved. `outbox.booking_id` becomes nullable for staff notifications on standalone/unmatched payments; a partial unique index deduplicates these events. The new tables are included in JSON backups. Credentials are never stored in these tables.

Schema installation is idempotent and uses the existing schema transaction lock. Back up both existing schemas and code before an authorised deployment. Keep the feature off while checking the migration, then set the agreed environment values and restart one bot process. Set the callback URL and the same secret in PayKeeper only after the HTTPS endpoint is available. Test a signed invalid notification, a real payment, a replay, receipt arrival and a restart.

Rollback: stop the new process and restore the previous code/configuration. Do not drop provider tables or restore an old payment snapshot over newer payments. Reconcile any invoices issued by the new process before switching payment handling, and disable/repoint the merchant callback as part of rollback. With the feature off, legacy manual payment requests remain available; previously issued provider requests must still be reconciled.

## Unknown invoice creation outcome

The staff page displays the immutable request ID. Look up the invoice in PayKeeper by that order ID. For a found invoice, enter its number in **Проверить и привязать счёт**. The server validates order, client and amount using the API before attaching it. If no invoice exists, do not invent an invoice number; investigate the provider response before deciding how to retry.

## Validation

Run `python -m unittest discover -v` with `TEST_DATABASE_URL` pointing to a disposable PostgreSQL instance, never production. Tests create and remove only uniquely named test schemas. Successful unit/DB tests do not prove delivery from PayKeeper through the production HTTPS proxy, fiscal legality, or actual guest delivery; these are separate activation checks.

Official references: [invoices](https://docs.paykeeper.ru/dokumentatsiya-json-api/scheta/), [POST notifications](https://docs.paykeeper.ru/metody-integratsii/priyom-post-opoveshhenij/), [receipts](https://docs.paykeeper.ru/dokumentatsiya-json-api/rabota-s-chekami-54-fz/), [receipt cart](https://docs.paykeeper.ru/onlain-kassa-54-fz/tovary-v-cheke-54-fz-full/).


## Booking reserve and telephone bookings (2026-10-25)

A pending Telegram booking with no received payment gets one reminder after 15 minutes if no payment link has been delivered. The first successfully delivered invoice link starts a separate window: after 10 minutes send one warning, then allow another 10 minutes from successful warning delivery. Before cancellation reconcile every invoice with the provider. Errors, uncertain invoice creation and concurrent invoice changes hold the booking for review. Any positive recorded payment prevents reserve cancellation. Late payments are recorded for review and never reopen a cancelled slot. Existing delivered links do not receive retrospective deadlines.

Staff can select a date and free standard session, enter phone and guest count, preview the existing tariff calculation and create a booking atomically. Name is optional; children use existing pricing rules. Telephone bookings have no Telegram owner: staff copy payment links manually, and Telegram delivery deadlines do not apply. Do not infer Telegram identity from a matching phone. A shared registry of verified communication channels and multiple bots is explicitly deferred.
