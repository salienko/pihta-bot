"""Read local content and environment without contacting external services."""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import re

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: tuple[int, ...]
    database_url: str
    content: dict
    rules: str
    rules_hash: str
    web_url: str = ''


def load():
    content = json.loads((ROOT / 'content.json').read_text(encoding='utf-8'))
    rules = (ROOT / 'rules.txt').read_text(encoding='utf-8')
    codes = set()
    for item in content['services']:
        if not re.fullmatch(r'[a-z_]{1,20}', item['code']) or item['code'] in codes:
            raise ValueError('Проверьте коды услуг в content.json.')
        codes.add(item['code'])
        if not 1 <= len(item['name']) <= 80 or type(item['price']) is not int or not 0 <= item['price'] <= 100000000:
            raise ValueError('Проверьте названия и цены услуг в content.json.')
    if len(codes) > 20:
        raise ValueError('Поддерживается до 20 услуг в каталоге.')
    for day, rate in content.get('holidays', {}).items():
        from datetime import date
        date.fromisoformat(day)
        if any(type(rate[key]) is not int or rate[key] <= 0 for key in ('base', 'extra')) or rate['base'] % 2:
            raise ValueError('Проверьте праздничный тариф.')
        if not isinstance(rate.get('label'), str) or not 1 <= len(rate['label']) <= 80:
            raise ValueError('Проверьте название праздничного тарифа.')
    admin_values = ','.join(filter(None,(os.getenv('ADMIN_TELEGRAM_IDS', '').strip(),os.getenv('OWNER_TELEGRAM_ID', '').strip())))
    ids = tuple(dict.fromkeys(int(value.strip()) for value in admin_values.split(',') if value.strip()))
    if any(value <= 0 for value in ids):
        raise ValueError('Нужны положительные Telegram ID администраторов.')
    from urllib.parse import urlparse
    web_url=os.getenv('WEB_BASE_URL','').strip().rstrip('/')
    if web_url:
        parsed=urlparse(web_url)
        if parsed.scheme!='https' or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('WEB_BASE_URL must be an HTTPS origin')
    return Settings(os.getenv('BOT_TOKEN', '').strip(), ids,
                    os.getenv('DATABASE_URL', '').strip(),
                    content, rules, hashlib.sha256(rules.encode()).hexdigest(), web_url)
