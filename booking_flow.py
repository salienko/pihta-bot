"""Booking conversation policy, independent from Telegram and persistence.

Only rental belongs to this release. Submitted bookings remain immutable.
"""
from datetime import date, datetime
from booking_domain import RuleError, TZ, clean_name, normalize_phone, horizon

VERSION = 3
STEPS = ('name', 'phone', 'guests', 'date', 'slot', 'review')
TITLES = {'name': 'Имя', 'phone': 'Телефон', 'guests': 'Гости', 'date': 'Дата', 'slot': 'Время', 'review': 'Проверка'}


def prepare(state, data):
    """Migrate an unfinished old draft only; never touch a submitted snapshot."""
    data = dict(data)
    if data.get('flow_version') != VERSION:
        data.update(flow_version=VERSION, cart={}, consent=False)
        data.pop('quote', None)
        state = 'name'
    return state, data


def require(data, step):
    if step not in STEPS:
        raise RuleError('Откройте текущее бронирование.')
    if STEPS.index(step) >= 1: clean_name(data.get('name', ''))
    if STEPS.index(step) >= 2: normalize_phone(data.get('phone', ''))
    if STEPS.index(step) >= 3:
        guests, kids = data.get('guests'), data.get('children')
        if type(guests) is not int or type(kids) is not int or not 1 <= guests <= 8 or not 0 <= kids <= guests:
            raise RuleError('Укажите от 1 до 8 гостей вместе с детьми.')
    if STEPS.index(step) >= 4:
        try: day = date.fromisoformat(data['day'])
        except (KeyError, ValueError): raise RuleError('Выберите дату.')
        today = datetime.now(TZ).date()
        if not today <= day <= horizon(today): raise RuleError('Выберите доступную дату заново.')
    if step == 'review':
        try: start = datetime.fromisoformat(data['start'])
        except (KeyError, ValueError): raise RuleError('Выберите время.')
        if start.tzinfo is None or start.astimezone(TZ).date().isoformat() != data['day']:
            raise RuleError('Выберите время на указанную дату.')


def first_missing(data):
    for step in STEPS[1:]:
        try: require(data, step)
        except RuleError: return STEPS[STEPS.index(step)-1]
    return 'review'


def change_guests(data, category, delta):
    if category not in ('paid', 'kid') or delta not in (-1, 1):
        raise RuleError('Используйте кнопки количества гостей.')
    data = dict(data)
    paid = data.get('guests', 1) - data.get('children', 0)
    kids = data.get('children', 0)
    if category == 'paid': paid += delta
    else: kids += delta
    if paid < 0 or kids < 0 or not 1 <= paid + kids <= 8:
        raise RuleError('От 1 до 8 гостей вместе с детьми.')
    data.update(guests=paid+kids, children=kids, consent=False)
    return data


def callback(action, draft, value=''):
    result = f'{action}:{draft["token"]}:{draft["revision"]}:{value}'
    if len(result.encode()) > 64: raise ValueError('Callback exceeds Telegram limit')
    return result
