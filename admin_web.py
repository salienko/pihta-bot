"""Server-rendered admin panel. Telegram-issued, single-use authentication."""
from datetime import datetime,timedelta
from decimal import Decimal,InvalidOperation
import hashlib
import hmac
from html import escape
import secrets
from aiohttp import web
from booking_domain import TZ,RuleError,LABELS,card,rub

STYLE='''*{box-sizing:border-box}body{margin:0;background:#eff3ee;color:#213b2d;font:16px system-ui,sans-serif}header{background:#203d2e;color:white;padding:22px max(5vw,18px)}main{max-width:1100px;margin:auto;padding:24px 18px}h1{font-size:28px}h2{font-size:21px}a{color:#246448}header a{color:white;margin-right:20px}.card{padding:22px;background:white;border-radius:16px;margin:16px 0;box-shadow:0 2px 7px #1a39200b}pre{white-space:pre-wrap;font:inherit;line-height:1.65}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}input,select,textarea,button{font:inherit;padding:12px;border:1px solid #cbd6cc;border-radius:9px;max-width:100%}label{display:block;margin:12px 0}input,select,textarea{display:block;width:100%;margin-top:5px}button{background:#2c6145;color:white;cursor:pointer}button.danger{background:#864735}small,.muted{color:#617568}.badge{display:inline-block;background:#e7efe7;padding:5px 10px;border-radius:20px}table{width:100%;border-collapse:collapse}td,th{padding:12px 8px;text-align:left;border-bottom:1px solid #e4ebe4}.scroll{overflow:auto}nav{display:flex;gap:20px;flex-wrap:wrap}.notice{padding:16px;background:#fff4d6;border-radius:12px}.inline{display:inline}header form button{padding:6px 15px;background:transparent}.button{display:inline-block;background:#2c6145;color:white;text-decoration:none;padding:12px;border-radius:9px}'''
def e(v):return escape(str(v),quote=True)
def page(title,body):return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+e(title)+' · Пихта</title><link rel="stylesheet" href="/style.css"><header><strong>ПИХТА · Управление</strong></header><main>'+body+'</main></html>'
def money(value):
    try:
        amount=Decimal(value)*100
        if amount!=amount.to_integral_value() or not amount.is_finite() or amount<0 or amount>100000000:raise ValueError()
        return int(amount)
    except (InvalidOperation,ValueError,TypeError):raise RuleError('Введите сумму в рублях, не более двух знаков после точки.')

def make_app(store,settings,secure=True):
    @web.middleware
    async def security(request,handler):
        try:response=await handler(request)
        except RuleError as error:response=web.Response(status=400,text=page('Не выполнено','<div class="card"><h1>Действие не выполнено</h1><p>'+e(error)+'</p><a href="/">Вернуться</a></div>'),content_type='text/html')
        except web.HTTPException as error:response=error
        response.headers.update({'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Content-Security-Policy':"default-src 'none'; style-src 'self'; script-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'; connect-src 'self'; img-src 'self'"})
        if secure:response.headers['Strict-Transport-Security']='max-age=31536000'
        return response
    app=web.Application(middlewares=[security],client_max_size=16384)

    async def auth(request,post=False):
        s=await store.session(request.cookies.get('pihta_session'))
        if not s:raise web.HTTPFound('/login')
        if post:
            if request.headers.get('Origin')!=settings.web_url and not (not secure and request.headers.get('Origin')=='null'):raise web.HTTPForbidden(text='Неверный источник запроса.')
            data=await request.post()
            if not hmac.compare_digest(str(data.get('csrf','')),s['csrf']):raise web.HTTPForbidden(text='Обновите страницу и повторите действие.')
        return s
    def form(s,action,fields,label='Сохранить',danger=False):
        return f'<form method="post" action="{e(action)}"><input type="hidden" name="csrf" value="{e(s["csrf"])}"><input type="hidden" name="operation" value="{secrets.token_hex(16)}">'+fields+f'<button class="{"danger" if danger else ""}">{e(label)}</button></form>'
    def navigation(s):return '<nav><a href="/">Заявки и календарь</a><a href="/catalog">Услуги и праздники</a><a href="/archive">Прежние заявки</a><a href="/audit">Журнал действий</a></nav>'+form(s,'/logout','','Выйти')

    async def login_page(request):
        body='<div class="card"><h1>Вход администратора</h1><p>Получите личную одноразовую ссылку командой /admin в боте «Пихта».</p><form id="login" action="/login" method="post"><input type="hidden" id="token" name="token"><button id="enter" hidden>Войти</button></form><script src="/login.js" defer></script></div>'
        return web.Response(text=page('Вход',body),content_type='text/html')
    async def login_js(request):return web.Response(text="function takeToken(){const token=location.hash.slice(1);history.replaceState(null,'','/login');if(token){document.getElementById('token').value=token;document.getElementById('enter').hidden=false;}}takeToken();addEventListener('hashchange',takeToken);",content_type='text/javascript')
    async def login(request):
        if request.headers.get('Origin')!=settings.web_url and not (not secure and request.headers.get('Origin')=='null'):raise web.HTTPForbidden()
        data=await request.post();token=await store.login(str(data.get('token','')))
        response=web.HTTPFound('/');response.set_cookie('pihta_session',token,secure=secure,httponly=True,samesite='Strict',max_age=8*3600,path='/');return response
    async def logout(request):
        await auth(request,True);token=request.cookies['pihta_session']
        await store.pool.execute('DELETE FROM web_sessions WHERE hash=$1',hashlib.sha256(token.encode()).hexdigest())
        r=web.HTTPFound('/login');r.del_cookie('pihta_session');return r

    async def home(request):
        s=await auth(request);offset=max(0,int(request.query.get('offset','0')));rows=await store.listing(s['actor'],staff=True,offset=offset)
        day=date_value=request.query.get('day',datetime.now(TZ).date().isoformat())
        start=datetime.fromisoformat(day).replace(tzinfo=TZ);end=start+timedelta(days=1)
        calendar_rows=await store.pool.fetch('SELECT * FROM bookings WHERE starts_at<$2 AND blocked_until>$1 ORDER BY starts_at',start,end)
        body=navigation(s)+'<h1>Бронирования</h1><p>Подтверждение относится ко всему составу. Отправленный заказ не редактируется.</p>'
        from booking_store import unpack
        body+='<div class="card"><h2>Календарь</h2><form><label>Дата<input type="date" name="day" value="'+e(day)+'"></label><button>Показать</button></form>'
        for raw in calendar_rows:
            r=unpack(raw);body+=f'<p><a href="/booking/{e(r["id"])}">{r["starts_at"].astimezone(TZ):%H:%M}–{r["ends_at"].astimezone(TZ):%H:%M} · {e(LABELS[r["status"]])}</a> · уборка до {r["blocked_until"].astimezone(TZ):%H:%M}</p>'
        if not calendar_rows:body+='<p>Броней на дату нет.</p>'
        body+='</div><div class="card scroll"><table><tr><th>Дата</th><th>Бронь</th><th>Статус</th><th>Итог</th></tr>'
        for r in rows[:20]:body+=f'<tr><td>{r["starts_at"].astimezone(TZ):%d.%m.%Y %H:%M}</td><td><a href="/booking/{e(r["id"])}">{e(r["id"])}</a></td><td>{e(LABELS[r["status"]])}</td><td>{e(rub(r["estimate"]["total"]))}</td></tr>'
        body+='</table></div>'
        if offset:body+=f'<a href="/?offset={max(0,offset-20)}">← Назад</a> '
        if len(rows)>20:body+=f'<a href="/?offset={offset+20}">Далее →</a>'
        errors=await store.pool.fetchval('SELECT count(*) FROM outbox WHERE delivered_at IS NULL AND last_error IS NOT NULL')
        body+=f'<p>Уведомлений с ошибками доставки: {errors}. В Telegram необходимо открыть /start для получения сообщений.</p>'
        body+='<div class="card"><h2>Закрыть время для записи</h2>'+form(s,'/block','<label>Начало<input name="start" type="datetime-local" required></label><label>Окончание, включая уборку<input name="end" type="datetime-local" required></label><label>Причина<input name="reason" minlength="2" maxlength="500" required></label>','Закрыть интервал')+'</div>'
        blocks=await store.pool.fetch('SELECT * FROM calendar_blocks WHERE active ORDER BY starts_at DESC LIMIT 50')
        for b in blocks:body+='<div class="card">'+e(str(b['starts_at'].astimezone(TZ)))+' — '+e(str(b['ends_at'].astimezone(TZ)))+'<p>'+e(b['reason'])+'</p>'+form(s,'/unblock/'+str(b['id']),'','Открыть интервал')+'</div>'
        return web.Response(text=page('Календарь',body),content_type='text/html')

    async def detail(request):
        s=await auth(request);r=await store.get(s['actor'],request.match_info['id'],staff=True)
        body=navigation(s)+'<h1>Бронь № '+e(r['id'])+'</h1><div class="card"><pre>'+e(card(r))+'</pre></div>'
        if r['status']=='pending':body+='<div class="card">'+form(s,'/booking/'+r['id']+'/confirm','<p>Подтвердить время и все выбранные услуги?</p>','Подтвердить весь заказ')+'</div>'
        if r['status'] in ('pending','confirmed'):
            body+='<div class="card">'+form(s,'/booking/'+r['id']+'/cancel','<label>Причина отмены<textarea name="reason" minlength="2" maxlength="500" required></textarea></label>','Отменить с уведомлением',True)+'</div>'
            if r['status']=='pending':body+='<div class="card">'+form(s,'/booking/'+r['id']+'/reject','<label>Причина отказа<textarea name="reason" minlength="2" maxlength="500" required></textarea></label>','Отказать',True)+'</div>'
        body+='<div class="grid">'
        for action,title in [('payment','Фактически принято на месте'),('refund','Фактически возвращено вручную')]:
            body+='<div class="card"><h2>'+title+'</h2>'+form(s,'/booking/'+r['id']+'/'+action,'<label>Сумма, ₽<input name="amount" type="number" min="0.01" max="1000000" step="0.01" required></label><label>Комментарий<input name="note" minlength="2" maxlength="500" required></label>','Зафиксировать факт')+'</div>'
        body+='</div><div class="card"><h2>Фактическое посещение — отдельно от брони</h2><p>Исходная сумма и состав сохраняются. Меньшее число гостей не снижает стоимость.</p>'+form(s,'/booking/'+r['id']+'/actual','<label>Фактических гостей<input name="guests" type="number" min="1" max="8" required></label><label>Итог фактического расчёта, ₽<input name="total" type="number" min="0" step="0.01" required></label><label>Обоснование расчёта<textarea name="note" minlength="2" maxlength="500" required></textarea></label>')+'</div>'
        body+='<div class="card"><h2>Связаться с гостем</h2><p>Например, уточнить наличие услуги. Сообщение не меняет заказ и занятость.</p>'+form(s,'/booking/'+r['id']+'/message','<label>Сообщение<textarea name="text" minlength="2" maxlength="1000" required></textarea></label>','Отправить гостю')+'</div>'
        if r['status'] in ('confirmed','completed'):body+='<div class="card">'+form(s,'/booking/'+r['id']+'/no_show','<label>Комментарий<input name="note" minlength="2" maxlength="500" required></label>','Отметить неявку')+'</div>'
        events=await store.pool.fetch('SELECT * FROM audit WHERE booking_id=$1 ORDER BY id DESC LIMIT 100',r['id'])
        body+='<div class="card"><h2>История действий</h2>'
        for event in events:body+='<p>'+e(str(event['created_at'].astimezone(TZ)))+' · '+e(event['action'])+' · сотрудник '+e(event['actor'])+'</p>'
        return web.Response(text=page('Бронь',body+'</div>'),content_type='text/html')

    async def action(request):
        s=await auth(request,True);data=await request.post();ident=request.match_info['id'];kind=request.match_info['action']
        if kind in ('confirm','reject','cancel'):await store.transition(s['actor'],ident,kind,str(data.get('reason','')),staff=True)
        elif kind=='message':
            key=str(data.get('operation',''))
            if not re_full_token(key):raise RuleError('Обновите форму.')
            await store.staff_note(s['actor'],ident,key,str(data.get('text','')))
        else:
            key=str(data.get('operation',''))
            if not re_full_token(key):raise RuleError('Обновите форму.')
            await store.accounting(s['actor'],ident,key,kind,amount=money(data.get('amount','0')),guests=int(data['guests']) if data.get('guests') else None,total=money(data['total']) if data.get('total') else None,note=str(data.get('note','')))
        raise web.HTTPFound('/booking/'+ident)

    async def block(request):
        s=await auth(request,True);data=await request.post();start=datetime.fromisoformat(data['start']).replace(tzinfo=TZ);end=datetime.fromisoformat(data['end']).replace(tzinfo=TZ);reason=str(data.get('reason','')).strip()
        if not start<end or end-start>timedelta(days=90) or not 2<=len(reason)<=500:raise RuleError('Проверьте интервал и причину.')
        async with store.tx() as db:
            if await store.blocked(db,start,end):raise RuleError('В интервале есть бронь или блокировка. Сначала проверьте календарь.')
            await db.execute('INSERT INTO calendar_blocks(starts_at,ends_at,reason,actor) VALUES($1,$2,$3,$4)',start,end,reason,s['actor']);await store.audit(db,None,s['actor'],'block',dict(start=start.isoformat(),end=end.isoformat(),reason=reason))
        raise web.HTTPFound('/')
    async def unblock(request):
        s=await auth(request,True)
        async with store.tx() as db:
            await db.execute('UPDATE calendar_blocks SET active=false WHERE id=$1',int(request.match_info['id']));await store.audit(db,None,s['actor'],'unblock',{'id':request.match_info['id']})
        raise web.HTTPFound('/')
    async def archive(request):
        s=await auth(request);body=navigation(s)+'<h1>Прежние заявки</h1><p>Заявки старой версии не меняются и сами по себе не бронировали время. Проверьте их вручную перед началом работы с календарём.</p>'
        exists=await store.pool.fetchval("SELECT to_regclass('pihta_intake_v1.orders') IS NOT NULL")
        if exists:
            rows=await store.pool.fetch('SELECT id,data,created_at FROM pihta_intake_v1.orders ORDER BY created_at DESC LIMIT 200')
            import json
            for r in rows:
                d=json.loads(r['data']) if isinstance(r['data'],str) else r['data']
                body+='<div class="card"><h2>'+e(r['id'])+'</h2><pre>'+e('\n'.join(f'{k}: {v}' for k,v in d.items() if k in ('start','hours','guests','children','name','phone','cart')))+'</pre></div>'
        else:body+='<p>Прежних заявок нет.</p>'
        return web.Response(text=page('Архив',body),content_type='text/html')
    async def audit(request):
        s=await auth(request);rows=await store.pool.fetch('SELECT * FROM audit ORDER BY id DESC LIMIT 100');body=navigation(s)+'<h1>Журнал действий</h1>'
        for r in rows:body+='<div class="card">'+e(str(r['created_at'].astimezone(TZ)))+' · '+e(r['action'])+' · '+e(r['actor'])+'<pre>'+e(r['detail'])+'</pre></div>'
        return web.Response(text=page('Журнал',body),content_type='text/html')
    async def health(request):
        try:await store.pool.fetchval('SELECT 1')
        except Exception:return web.json_response({'status':'unavailable'},status=503)
        return web.json_response({'status':'ok'})
    async def catalog(request):
        s=await auth(request);items,holidays=await store.pricing();body=navigation(s)+'<h1>Услуги и праздничные даты</h1><p>Изменения доступности действуют для новых отправок. Состав отправленных броней остаётся прежним.</p>'
        for code,i in items.items():body+='<div class="card"><h2>'+e(i['name'])+'</h2><p>'+e(rub(i['price']))+' · '+('Доступно' if i.get('available',True) else 'Нет в наличии')+'</p>'+form(s,'/catalog/'+code,'<input type="hidden" name="available" value="'+('0' if i.get('available',True) else '1')+'">','Снять с продажи' if i.get('available',True) else 'Вернуть в каталог')+'</div>'
        body+='<div class="card"><h2>Праздничный тариф</h2><p>Укажите каждую дату отдельно. Новогодняя цена задаётся явно.</p>'+form(s,'/holiday','<label>Дата<input name="day" type="date" required></label><label>Название<input name="label" maxlength="80" required></label><label>Первые 2 часа, ₽<input name="base" type="number" min="1" step="1" value="6500" required></label><label>Следующий час, ₽<input name="extra" type="number" min="1" step="1" value="3000" required></label>','Сохранить тариф')+'</div>'
        for day,rate in sorted(holidays.items()):body+='<p>'+e(day)+' · '+e(rate['label'])+' · '+e(rub(rate['base']))+' / '+e(rub(rate['extra']))+'</p>'
        return web.Response(text=page('Каталог',body),content_type='text/html')
    async def availability(request):
        s=await auth(request,True);data=await request.post();code=request.match_info['code'];items,_=await store.pricing()
        if code not in items:raise RuleError('Услуга не найдена.')
        value=data.get('available')=='1'
        async with store.tx() as db:
            await db.execute('INSERT INTO service_settings VALUES($1,$2) ON CONFLICT(code) DO UPDATE SET available=$2',code,value);await store.audit(db,None,s['actor'],'availability',dict(code=code,available=value))
        raise web.HTTPFound('/catalog')
    async def holiday(request):
        s=await auth(request,True);data=await request.post()
        from datetime import date
        day=date.fromisoformat(data['day']);label=str(data.get('label','')).strip();base=money(data.get('base'));extra=money(data.get('extra'))
        if not 1<=len(label)<=80 or base<=0 or base%2 or extra<=0:raise RuleError('Проверьте тариф.')
        async with store.tx() as db:
            await db.execute('INSERT INTO holiday_rates VALUES($1,$2,$3,$4) ON CONFLICT(day) DO UPDATE SET label=$2,base=$3,extra=$4',day,label,base,extra);await store.audit(db,None,s['actor'],'holiday',dict(day=day.isoformat(),label=label,base=base,extra=extra))
        raise web.HTTPFound('/catalog')
    async def style(request):return web.Response(text=STYLE,content_type='text/css')
    app.add_routes([web.get('/style.css',style),web.get('/health',health),web.get('/login',login_page),web.get('/login.js',login_js),web.post('/login',login),web.post('/logout',logout),web.get('/',home),web.get('/booking/{id}',detail),web.post('/booking/{id}/{action}',action),web.post('/block',block),web.post('/unblock/{id}',unblock),web.get('/archive',archive),web.get('/audit',audit),web.get('/catalog',catalog),web.post('/catalog/{code}',availability),web.post('/holiday',holiday)])
    return app

def re_full_token(value):
    import re
    return bool(re.fullmatch('[a-f0-9]{32}',value))
