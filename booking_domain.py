"""Booking policies. Money is integer kopecks; all dates are Moscow-aware."""
from datetime import datetime, timedelta
from domain import TZ, RuleError, horizon, participants, clean_name, normalize_phone, rub

SLOTS = (10, 13, 16, 19, 22)
ACTIVE = ('pending', 'confirmed')
LABELS = {'draft':'Черновик', 'pending':'Ждёт подтверждения', 'confirmed':'Подтверждено',
          'cancelled':'Отменено', 'expired':'Отменено автоматически',
          'rejected':'Отказано', 'completed':'Завершено'}

def validate_start(start, clock):
    if start.tzinfo is None or clock.tzinfo is None:
        raise RuleError('Нужен часовой пояс.')
    start, clock = start.astimezone(TZ), clock.astimezone(TZ)
    if start.hour not in SLOTS or start.minute or start.second or start.microsecond:
        raise RuleError('Выберите стандартный двухчасовой сеанс. Другое время — через администратора.')
    if start < clock + timedelta(hours=1) or start.date() > horizon(clock.date()):
        raise RuleError('Запись минимум за час и максимум на два календарных месяца.')
    return start + timedelta(hours=2)

def estimate(data, catalog, holidays):
    start = datetime.fromisoformat(data['start']).astimezone(TZ)
    if data.get('hours') != 2:
        raise RuleError('Нестандартное время согласовывается с администратором.')
    participants(data['guests'], data['children'])
    if start.month == 1 and start.day == 1 and start.date().isoformat() not in holidays:
        raise RuleError('Новогодний тариф уточните у администратора.')
    aroma = sum(data.get('cart', {}).get(code, 0) for code in ('eucalyptus','lavender'))
    if aroma > 1:
        raise RuleError('Выберите одно аромапарение: эвкалипт или лаванда.')
    rent = 0
    for offset in range(2):
        hour = start + timedelta(hours=offset)
        special = holidays.get(hour.date().isoformat())
        rent += (special['base'] if special else 600000 if hour.weekday() >= 5 or hour.hour >= 19 else 500000)//2
    extra = max(0, data['guests']-data['children']-4)*25000*2
    items=[]
    for code, qty in sorted(data.get('cart', {}).items()):
        if code not in catalog or type(qty) is not int or not 1 <= qty <= 100 or not catalog[code].get('available', True):
            raise RuleError('Проверьте доступность и количество услуг.')
        item=catalog[code]
        items.append(dict(code=code,name=item['name'],quantity=qty,unit_price=item['price'],total=item['price']*qty))
    return dict(rent=rent,guest_extra=extra,items=items,total=rent+extra+sum(i['total'] for i in items))

def can_cancel(status, start, clock):
    return status == 'pending' or (status == 'confirmed' and start-clock >= timedelta(hours=24))

def card(row, personal=True):
    d,q=row['data'],row['estimate']
    start=row['starts_at'].astimezone(TZ);end=row['ends_at'].astimezone(TZ)
    lines=[f"Бронь № {row['id']}",LABELS[row['status']],f'{start:%d.%m.%Y %H:%M}–{end:%H:%M}',
           f"Кол-во гостей: {d['guests']}",f"Гости с 7 лет: {d['guests']-d['children']}",
           f"Дети до 6 лет включительно: {d['children']}",f"Простыни включены: {d['guests']} шт.",
           f"Аренда: {rub(q['rent'])}",f"Доплата за гостей: {rub(q['guest_extra'])}"]
    lines += [f"{i['name']} × {i['quantity']} — {rub(i['total'])}" for i in q['items']]
    if not q['items']:lines.append('Дополнительные услуги не выбраны.')
    lines += [f"Итого по брони: {rub(q['total'])}",f"Оплачено: {rub(row.get('paid',0))}"]
    actual=row.get('actual_total');total=actual if actual is not None else q['total']
    if actual is not None:lines.append('Фактический расчёт отдельно: '+rub(actual))
    lines.append('Остаток: '+rub(max(0,total-row.get('paid',0))))
    if row.get('paid',0)>total:lines.append('Переплата: '+rub(row['paid']-total)+' — обратитесь к администратору.')
    if row.get('reason'):lines.append('Причина: '+row['reason'])
    if row.get('no_show'):lines.append('Администратор отметил неявку.')
    if row['status']=='confirmed' and start<=datetime.now(TZ)<end:lines.append('Сеанс идёт. Окончание по исходной брони.')
    if personal:lines += [f"Имя: {d['name']}",f"Телефон: {d['phone']}"]
    lines += ['После отправки бронь не редактируется.','Оплата на месте до начала сеанса.']
    return '\n'.join(lines)
