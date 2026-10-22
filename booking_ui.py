"""Native Telegram conversation. Persistent drafts; no Mini App."""
from settings import EXTRAS_ENABLED
from datetime import datetime,date,timedelta
from pathlib import Path
import secrets
from aiogram import Router
from aiogram.types import ReplyKeyboardMarkup,KeyboardButton,InlineKeyboardMarkup,InlineKeyboardButton,FSInputFile
from aiogram.exceptions import TelegramBadRequest
from booking_domain import TZ,SLOTS,LABELS,RuleError,estimate,card,can_cancel,validate_start,clean_name,normalize_phone,horizon,rub

MENU=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=a),KeyboardButton(text=b)] for a,b in [('Забронировать','Цены'),('Мои бронирования','Правила'),('Контакты','Помощь')]],resize_keyboard=True,is_persistent=True)
def kb(rows):return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t,**({'url':v} if v.startswith('https://') else {'callback_data':v})) for t,v in row] for row in rows])
def nav():return [[('Главное меню','home')]]

class BookingUI:
    def __init__(self,settings,store):
        self.settings,self.store=settings,store;self.catalog={i['code']:i for i in settings.content['services']}
        self.router=Router();self.router.message.register(self.message);self.router.callback_query.register(self.callback)

    async def say(self,m,text,rows=None,photo=None,edit=False):
        markup=kb(rows if rows is not None else nav())
        if edit and len(text)<=1000:
            try:
                if m.photo:await m.edit_caption(caption=text,reply_markup=markup)
                else:await m.edit_text(text,reply_markup=markup)
                return
            except TelegramBadRequest as e:
                if 'message is not modified' in str(e):return
                if not any(s in str(e).lower() for s in ('message to edit not found',"message can't be edited")):raise
        file=Path(__file__).with_name('assets')/f'{photo}.jpg' if photo else None
        if file and file.exists():
            if len(text)<=1000:await m.answer_photo(FSInputFile(file),caption=text,reply_markup=markup);return
            await m.answer_photo(FSInputFile(file))
        for pos in range(0,len(text),3500):await m.answer(text[pos:pos+3500],reply_markup=markup if pos+3500>=len(text) else None)

    async def home(self,m):await m.answer('Пихта · баня на дровах\nВыберите действие в нижнем меню.',reply_markup=MENU)
    async def info(self,m,section):
        c=self.settings.content
        if section=='Контакты':await self.say(m,c['name']+'\n'+c['phone']+'\nПосёлок Приладожское',[[('Маршрут · Яндекс Карты',c['map_url'])],[('Позвонить / связаться','contact')]]+nav(),photo='home');return
        if section=='Правила':await self.say(m,self.settings.rules);return
        if section in ('Цены','Цены и услуги'):
            text='Аренда на 2 часа:\nПН–ПТ до 19:00 — 5000 ₽\nПН–ПТ с 19:00 и выходные — 6000 ₽\nПраздники, кроме Нового года — 6500 ₽\nСледующий час по согласованию: 2300 / 2800 / 3000 ₽.\nСвыше 4 гостей с 7 лет: 250 ₽/час за гостя. Максимум 8 вместе с детьми.\nПростыня на каждого гостя включена; полотенца платные.\n\n'+('\n'.join(f"{i['name']} — {rub(i['price'])}/{i.get('unit','шт.')}" for i in self.catalog.values()) if EXTRAS_ENABLED else 'Заказ дополнительных услуг в боте временно недоступен.')
            await self.say(m,text,[[('Забронировать','begin')],[('Связаться с администратором','contact')]]+nav(),photo='home');return
        await self.say(m,'Черновик можно менять. Отправленная заявка занимает время и ждёт подтверждения. После отправки изменения невозможны — только отмена и новая бронь.\nОплата полностью на месте до начала.\nКонтакт администратора: '+c['phone'],nav())

    async def start(self,m,actor):
        token=await self.store.begin(actor);d=await self.store.draft(actor,token)
        if d['state'].startswith('profile_'):
            await self.store.save(actor,d,'date',d['data']);d=await self.store.draft(actor,token)
        await self.render(m,actor,d)

    async def render(self,m,actor,d,edit=False):
        self.catalog,holidays=await self.store.pricing()
        state=d['state'];data=d['data'];
        if not EXTRAS_ENABLED:
            changed=bool(data.get('cart')) or state in ('services','product')
            data['cart']={}
            if state in ('services','product'):state='review'
            if changed:
                data['consent']=False
                await self.store.save(actor,d,state,data)
        t=d['token'];b=lambda a,v='':f'{a}:{t}:{v}'
        if state=='date':
            today=datetime.now(TZ).date();offset=data.get('offset',0);days=(horizon(today)-today).days+1
            rows=[[((today+timedelta(days=i)).strftime('%d.%m.%Y'),b('day',(today+timedelta(days=i)).isoformat()))] for i in range(offset,min(days,offset+7))]
            page=[]
            if offset:page.append(('← Раньше',b('page',str(max(0,offset-7)))))
            if offset+7<days:page.append(('Позже →',b('page',str(offset+7))))
            if page:rows.append(page)
            await self.say(m,'Выберите дату. Не более двух активных броней на разные даты. Запись минимум за час, максимум на два месяца.',rows+nav(),photo='date',edit=edit)
        elif state=='slot':
            day=date.fromisoformat(data['day']);rows=[]
            for hour in SLOTS:
                start=datetime.combine(day,datetime.min.time(),TZ)+timedelta(hours=hour)
                try:available=await self.store.free(start)
                except RuleError:continue
                if available:rows.append([(f'{hour:02}:00–{(hour+2)%24:02}:00',b('slot',str(hour)))])
            await self.say(m,'Свободные двухчасовые сеансы на '+day.strftime('%d.%m.%Y') if rows else 'Свободных сеансов на эту дату нет.',rows+[[('Нужны 3–5 часов · к менеджеру','contact')],[('Назад к датам',b('back','date'))]]+nav(),photo='time',edit=edit)
        elif state=='guests':
            paid=data['guests']-data['children'];kids=data['children']
            text=f'Количество гостей: {data["guests"]} из 8\nС 7 лет: {paid}\nДети до 6 лет включительно: {kids}\nПростыни включены: {data["guests"]} шт.\nПолотенца — платная аренда.'
            rows=[[('−',b('guest','paid-')),(f'С 7 лет: {paid}',b('noop')),('+',b('guest','paid+'))],[('−',b('guest','kid-')),(f'До 6 вкл.: {kids}',b('noop')),('+',b('guest','kid+'))],[('Далее',b('next','name'))],[('Назад ко времени',b('back','slot'))]]
            await self.say(m,text,rows+nav(),photo='guests',edit=edit)
        elif state in ('name','phone'):
            text='Отправьте имя сообщением.' if state=='name' else 'Отправьте телефон сообщением в формате +7XXXXXXXXXX.'
            key='name' if state=='name' else 'phone';rows=[]
            if data.get(key):rows.append([(f'Оставить: {data[key]}',b('keep',key))])
            await self.say(m,text,rows+[[('Назад',b('back','guests' if state=='name' else 'name'))]]+nav(),photo='booking')
        elif state=='services':
            start=datetime.fromisoformat(data['start']);text=f'Ваше бронирование\n{start:%d.%m.%Y %H:%M} · 2 часа\nКол-во гостей: {data["guests"]}\nПростыни включены: {data["guests"]} шт.\nВыберите дополнительные услуги или продолжите без них.'
            rows=[[(i['name'],b('product',code))] for code,i in self.catalog.items() if i.get('available',True)]
            rows += [[('Ваш заказ · итог',b('review'))],[('Изменить контакты',b('back','name'))]]
            await self.say(m,text,rows+nav(),photo='extras',edit=edit)
        elif state=='product':
            code=data['product'];item=self.catalog[code];qty=data['quantity'];unit=item.get('unit','шт.')
            text=f"{item['name']}\nЦена: {rub(item['price'])}/{unit}\nКол-во: {qty} {unit}\nСумма: {rub(item['price']*qty)}"
            if code=='sheet':text+='\nОдна простыня на каждого гостя уже включена.'
            if code in ('eucalyptus','lavender'):text+='\nМаксимум одно аромапарение. Выбор заменит другой аромат в черновике.'
            await self.say(m,text,[[('−',b('qty','-1')),(f'{qty} {unit}',b('noop')),('+',b('qty','1'))],[('Добавить в заказ' if qty else 'Убрать из заказа',b('add',data['item_nonce']))],[('Назад к услугам',b('back','services'))]]+nav(),photo=code,edit=edit)
        elif state=='review':
            q=estimate(data,self.catalog,holidays)
            if data.get('quote')!=q or data.get('rules_hash')!=self.settings.rules_hash:data['consent']=False
            data.update(quote=q,rules_hash=self.settings.rules_hash);await self.store.save(actor,d,'review',data)
            start=datetime.fromisoformat(data['start']);end=start+timedelta(hours=2)
            row=dict(id='черновик',data=data,estimate=q,starts_at=start,ends_at=end,status='draft',paid=0)
            text=card(row)+'\n\nПока ждёт подтверждения — отмена в любое время. После подтверждения — минимум за 24 часа. Без подтверждения заявка автоматически отменится по окончании сеанса.'
            if start-datetime.now(TZ)<timedelta(hours=24):text+='\nДо начала менее суток: после подтверждения отмена только через администратора.'
            rows=[[('Прочитать правила','info:Правила')],[(('☑' if data.get('consent') else '□')+' С правилами ознакомлен и согласен',b('consent'))]]
            if data.get('consent'):rows.append([('Отправить на подтверждение',b('submit'))])
            if EXTRAS_ENABLED:rows.append([('Изменить услуги в черновике',b('back','services'))])
            rows += [[('Изменить контакты',b('back','name'))],[('Изменить дату и гостей',b('back','date'))]]
            await self.say(m,text,rows+nav(),photo='booking')

    async def listing(self,m,actor,history=False,offset=0):
        rows=await self.store.listing(actor,history,offset=offset);buttons=[]
        for r in rows[:20]:buttons.append([(f"{r['starts_at'].astimezone(TZ):%d.%m %H:%M} · {LABELS[r['status']]}",'open:'+r['id'])])
        if len(rows)>20:buttons.append([('Далее',f'list:{int(history)}:{offset+20}')])
        if offset:buttons.append([('← Назад',f'list:{int(history)}:{max(0,offset-20)}')])
        if history and not offset:
            for r in await self.store.legacy(actor):buttons.append([('Прежняя заявка № '+r['id'],'legacy:'+r['id'])])
        buttons += [[('Предстоящие','list:0:0'),('История','list:1:0')],[('Мои данные','profile')],[('Забронировать','begin')]]
        await self.say(m,('История' if history else 'Мои бронирования')+('\nЗаписей нет.' if not rows else ''),buttons+nav(),photo='booking')

    async def show(self,m,actor,ident):
        r=await self.store.get(actor,ident);rows=[]
        if can_cancel(r['status'],r['starts_at'],datetime.now(TZ)):rows.append([('Отменить бронь','cancelask:'+ident)])
        rows += [[('Связаться с администратором','contact')],[('Мои бронирования','list:0:0')]]
        await self.say(m,card(r),rows+nav(),photo='booking')

    async def admin_link(self,m,actor):
        self.store.admin(actor)
        if not self.settings.web_url:await self.say(m,'Веб-панель ещё настраивается.');return
        token=await self.store.login_token(actor)
        await self.say(m,'Личная ссылка действует 5 минут и используется один раз. Не пересылайте её.',[[('Открыть веб-панель',self.settings.web_url+'/login#'+token)]])

    async def message(self,m):
        if m.chat.type!='private':return
        actor=m.from_user.id;text=(m.text or '').strip()
        try:
            if text in ('/start','Главное меню'):await self.home(m);return
            if text=='/id':await m.answer(str(actor));return
            if text=='/admin':await self.admin_link(m,actor);return
            if text=='Забронировать':await self.start(m,actor);return
            if text=='Мои бронирования':await self.listing(m,actor);return
            if text in ('Правила','Контакты','Цены','Цены и услуги','Помощь'):await self.info(m,text);return
            d=await self.store.draft(actor);data=d['data']
            if d['state']=='name':data['name']=clean_name(text);state='phone'
            elif d['state']=='phone':data['phone']=normalize_phone(text);state='services' if EXTRAS_ENABLED else 'review'
            elif d['state']=='profile_name':
                data['profile_name']=clean_name(text);await self.store.save(actor,d,'profile_phone',data);await self.say(m,'Теперь отправьте телефон для будущих заказов.');return
            elif d['state']=='profile_phone':
                await self.store.profile(actor,data['profile_name'],normalize_phone(text));await self.store.save(actor,d,'date',data);await self.say(m,'Данные обновлены. Уже отправленные брони не изменены.');return
            else:await self.render(m,actor,d);return
            data['consent']=False;await self.store.save(actor,d,state,data);await self.render(m,actor,await self.store.draft(actor))
        except RuleError as e:await self.say(m,str(e))

    async def callback(self,c):
        if not c.message or c.message.chat.type!='private':return
        await c.answer();actor=c.from_user.id;m=c.message;raw=c.data or '';parts=raw.split(':');a=parts[0]
        try:
            if a=='home':await self.home(m);return
            if a=='begin':await self.start(m,actor);return
            if a=='staffweb':await self.admin_link(m,actor);return
            if a=='contact':await self.say(m,'Администратор: '+self.settings.content['phone'],[[('Маршрут',self.settings.content['map_url'])]]+nav());return
            if a=='info':await self.info(m,parts[1]);return
            if a=='list':await self.listing(m,actor,bool(int(parts[1])),int(parts[2]));return
            if a=='open':await self.show(m,actor,parts[1]);return
            if a=='legacy':
                r=await self.store.legacy(actor,parts[1])
                if not r:raise RuleError('Прежняя заявка недоступна.')
                d=r['data'];text='Прежняя заявка № '+r['id']+'\n'+str(d.get('start',''))+'\nГостей: '+str(d.get('guests',''))+'\nИмя: '+str(d.get('name',''))+'\nТелефон: '+str(d.get('phone',''))+'\nИтог: '+rub(r['estimate']['total'])+'\nСохранена из прежней версии. Эта заявка сама по себе не закрепляла время.'
                await self.say(m,text,[[('История','list:1:0')]]+nav());return
            if a=='cancelask':
                r=await self.store.get(actor,parts[1]);await self.say(m,'Отменить эту бронь?\n'+card(r)+'\nВосстановление не гарантируется.',[[('Да, отменить','cancel:'+parts[1])],[('Нет, оставить','open:'+parts[1])]]);return
            if a=='cancel':await self.store.transition(actor,parts[1],'cancel');await self.show(m,actor,parts[1]);return
            if a=='profile':
                p=await self.store.profile(actor);await self.say(m,('Мои данные\n'+p['name']+'\n'+p['phone'] if p else 'Контактные данные пока не сохранены.')+'\nИзменения только для будущих заказов.',[[('Изменить данные','profileedit')]]+nav());return
            if a=='profileedit':
                token=await self.store.begin(actor);d=await self.store.draft(actor,token)
                await self.store.save(actor,d,'profile_name',d['data']);await self.say(m,'Введите имя для будущих заказов. Текущий черновик сохранён.');return
            if a=='submit':
                ident,created=await self.store.submit(actor,parts[1]);await self.say(m,'Заявка принята. Время занято за вами.' if created else 'Эта заявка уже сохранена.');await self.show(m,actor,ident);return
            if len(parts)!=3:raise RuleError('Кнопка устарела. Откройте главное меню.')
            _,token,v=parts;d=await self.store.draft(actor,token);data=d['data'];state=d['state'];new=state
            if not EXTRAS_ENABLED and (a in ('product','qty','add','review') or (a=='back' and v=='services')):
                await self.say(m,'Заказ дополнительных услуг временно недоступен. Продолжите бронирование.',[[('Продолжить бронирование','begin')]]+nav());return
            if a=='noop':return
            if a=='page' and state=='date':data['offset']=max(0,min(63,int(v)))
            elif a=='day' and state=='date':
                day=date.fromisoformat(v)
                if not datetime.now(TZ).date()<=day<=horizon(datetime.now(TZ).date()):raise RuleError('Выберите доступную дату.')
                data['day']=v;new='slot'
            elif a=='slot' and state=='slot':
                if int(v) not in SLOTS:raise RuleError('Выберите предложенный сеанс.')
                start=datetime.combine(date.fromisoformat(data['day']),datetime.min.time(),TZ)+timedelta(hours=int(v));validate_start(start,datetime.now(TZ))
                if not await self.store.free(start):raise RuleError('Время уже занято. Обновите список сеансов.')
                data['start']=start.isoformat();new='guests'
            elif a=='guest' and state=='guests':
                paid=data['guests']-data['children'];kids=data['children'];delta=1 if v.endswith('+') else -1
                if v.startswith('paid'):paid+=delta
                elif v.startswith('kid'):kids+=delta
                else:raise RuleError('Откройте количество гостей.')
                if paid<0 or kids<0 or not 1<=paid+kids<=8:raise RuleError('Всего от 1 до 8 гостей, включая детей.')
                data.update(guests=paid+kids,children=kids)
            elif a=='next' and state=='guests' and v=='name':new='name'
            elif a=='keep' and ((v=='name' and state=='name') or (v=='phone' and state=='phone')):
                if not data.get(v):raise RuleError('Сначала укажите данные.')
                new='phone' if v=='name' else ('services' if EXTRAS_ENABLED else 'review')
            elif a=='product' and state=='services':
                if v not in self.catalog:raise RuleError('Услуга недоступна.')
                data.update(product=v,quantity=data.get('cart',{}).get(v,0),item_nonce=secrets.token_hex(4));new='product'
            elif a=='qty' and state=='product':data['quantity']=max(0,min(1 if data['product'] in ('eucalyptus','lavender') else 100,data['quantity']+int(v)))
            elif a=='add' and state=='product' and v==data['item_nonce']:
                code=data['product'];cart=data.setdefault('cart',{});qty=data['quantity']
                if code in ('eucalyptus','lavender') and qty:
                    cart.pop('eucalyptus',None);cart.pop('lavender',None)
                if qty:cart[code]=qty
                else:cart.pop(code,None)
                new='services'
            elif a=='review' and state=='services':new='review'
            elif a=='consent' and state=='review':data['consent']=not data.get('consent',False)
            elif a=='back':
                required={'date':(),'slot':('day',),'guests':('start',),'name':('start','guests'),'services':('name','phone','start')}
                if v not in required or any(k not in data for k in required[v]):raise RuleError('Сначала заполните предыдущие шаги.')
                new=v
            else:raise RuleError('Эта кнопка уже обработана. Продолжите текущий черновик.')
            if a!='consent':data['consent']=False
            await self.store.save(actor,d,new,data);await self.render(m,actor,await self.store.draft(actor),edit=a in ('qty','guest','page','consent'))
        except (RuleError,ValueError,IndexError,KeyError) as e:await self.say(m,str(e) if isinstance(e,RuleError) else 'Кнопка устарела. Откройте главное меню.')
