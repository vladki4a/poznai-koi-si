"""Photo identity party game. One worker; ephemeral rooms, no accounts."""
import asyncio
import json
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
import urllib.request
from urllib.parse import quote

ROOT = Path(__file__).parent
CARDS = json.loads((ROOT / 'cards.json').read_text(encoding='utf-8'))
CARD_MAP = {c['id']: c for c in CARDS}
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
rooms = {}
limits = defaultdict(deque)
image_cache = {}
image_locks = defaultdict(asyncio.Lock)
TTL = 6 * 3600


def fail(message, code=400):
    raise HTTPException(code, message)


def clean_name(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 24:
        fail('Името трябва да е между 1 и 24 знака.')
    return value.strip()


def selected_cards(config):
    categories = config.get('categories', [])
    if not isinstance(categories, list):
        fail('Избери категории.')
    pool = [c for c in CARDS if c['category'] in categories]
    custom = config.get('custom', [])
    if not isinstance(custom, list) or len(custom) > 80:
        fail('Позволени са до 80 собствени карти.')
    for i, c in enumerate(custom):
        if not isinstance(c, dict) or not isinstance(c.get('name'), str) or not 1 <= len(c['name'].strip()) <= 60:
            fail('Невалидна собствена карта.')
        image = c.get('image', '')
        if not isinstance(image, str) or len(image) > 190000 or (image and not image.startswith(('data:image/jpeg;base64,', 'data:image/png;base64,', 'data:image/webp;base64,'))):
            fail('Невалидна снимка.')
        pool.append(dict(id='custom-' + str(i), name=c['name'].strip(), category='custom', image=image))
    unique = {c['name'].casefold(): c for c in pool}
    return list(unique.values())


def player(name):
    return dict(id=secrets.token_hex(8), token=secrets.token_urlsafe(32), name=clean_name(name), card=None, rank=None, seen=time.time())


def public_card(c, photos=True):
    if not c:
        return None
    return dict(name=c['name'], image=(c.get('image') or '/image/' + c['id']) if photos else '', category=c['category'])


def get_room(code):
    r = rooms.get(code.upper())
    if not r or time.time() - r['updated'] > TTL:
        fail('Стаята не съществува или е изтекла.', 404)
    return r


def auth(r, token):
    p = next((p for p in r['players'] if secrets.compare_digest(p['token'], token)), None)
    if not p:
        fail('Влез отново в стаята.', 403)
    p['seen'] = time.time()
    return p


def view(r, me):
    return dict(code=r['code'], phase=r['phase'], host=r['host'] == me['id'], me=me['id'], turn=r.get('turn'),
                photos=r['photos'], event=r.get('event', ''), revision=r['revision'],
                players=[dict(id=p['id'], name=p['name'], rank=p['rank'], online=time.time()-p['seen'] < 30,
                              card=public_card(p['card'], r['photos']) if p['id'] != me['id'] or p['rank'] is not None or r['phase'] == 'finished' else None)
                         for p in r['players']])


def advance(r):
    active = [p for p in r['players'] if p['rank'] is None]
    if not active:
        r.update(phase='finished', turn=None)
        return
    ids = [p['id'] for p in r['players']]
    start = ids.index(r['turn'])
    for step in range(1, len(ids)+1):
        p = r['players'][(start+step) % len(ids)]
        if p['rank'] is None:
            r['turn'] = p['id']
            return


@app.middleware('http')
async def security(request: Request, call_next):
    if request.url.path.startswith('/api/') and request.method == 'POST':
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Невалиден източник.'}, status_code=403)
        if int(request.headers.get('content-length', '0')) > 3500000:
            return JSONResponse({'detail': 'Снимките са твърде големи.'}, status_code=413)
        ip = request.client.host if request.client else 'unknown'
        q = limits[ip]
        now = time.time()
        while q and q[0] < now-60:
            q.popleft()
        if len(q) > 160:
            return JSONResponse({'detail': 'Твърде много заявки. Изчакай малко.'}, status_code=429)
        q.append(now)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['X-Frame-Options'] = 'DENY'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/health')
def health():
    return {'ok': True, 'game': 'poznai-koi-si', 'version': 1}


@app.get('/api/catalog')
def catalog():
    return dict(cards=[dict(id=c['id'], name=c['name'], category=c['category'], image='/image/'+c['id']) for c in CARDS],
                ads=dict(client=os.getenv('ADSENSE_CLIENT', ''), approved=os.getenv('ADS_APPROVED') == '1', horizontal='7473811583', side='5554225257'))


@app.post('/api/create')
async def create(request: Request):
    data = await request.json()
    for k in list(rooms):
        if time.time() - rooms[k]['updated'] > TTL:
            del rooms[k]
    if len(rooms) >= 150:
        fail('Всички стаи са заети. Опитай по-късно.', 503)
    pool = selected_cards(data)
    if len(pool) < 3:
        fail('Нужни са поне 3 различни карти.')
    p = player(data.get('name'))
    code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(6))
    while code in rooms:
        code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(6))
    r = dict(code=code, phase='lobby', players=[p], host=p['id'], pool=pool, photos=data.get('photos', True) is True,
             updated=time.time(), revision=0, used=[], turn=None)
    rooms[code] = r
    return dict(token=p['token'], state=view(r, p))


@app.post('/api/join')
async def join(request: Request):
    data = await request.json()
    r = get_room(str(data.get('code', '')))
    if r['phase'] != 'lobby':
        fail('Играта вече е започнала.')
    if len(r['players']) >= 8:
        fail('Стаята е пълна (до 8 играчи).')
    p = player(data.get('name'))
    if any(x['name'].casefold() == p['name'].casefold() for x in r['players']):
        fail('Това име вече се използва в стаята.')
    r['players'].append(p)
    r.update(updated=time.time(), revision=r['revision']+1)
    return dict(token=p['token'], state=view(r, p))


@app.get('/api/room/{code}')
def state(code: str, request: Request):
    r = get_room(code)
    p = auth(r, request.headers.get('authorization', '').removeprefix('Bearer '))
    r['updated'] = time.time()
    return view(r, p)


@app.post('/api/action/{code}')
async def action(code: str, request: Request):
    data = await request.json()
    r = get_room(code)
    p = auth(r, request.headers.get('authorization', '').removeprefix('Bearer '))
    a = data.get('action')
    if a == 'start':
        if p['id'] != r['host']:
            fail('Само домакинът може да започне.', 403)
        if r['phase'] not in ('lobby', 'finished'):
            fail('Рундът вече е започнал.')
        n = len(r['players'])
        if n < 3:
            fail('Нужни са поне 3 играчи.')
        available = [c for c in r['pool'] if c['name'] not in r['used']]
        if len(available) < n:
            available = r['pool'][:]
            r['used'] = []
        if len(available) < n:
            fail('Няма достатъчно различни карти за всички.')
        cards = secrets.SystemRandom().sample(available, n)
        for x, c in zip(r['players'], cards):
            x.update(card=c, rank=None)
        r['used'].extend(c['name'] for c in cards)
        r.update(phase='playing', turn=secrets.choice(r['players'])['id'], event='Нов рунд!')
    elif a in ('yes', 'no', 'solved'):
        if r['phase'] != 'playing':
            fail('Няма активен рунд.')
        if data.get('revision') != r['revision']:
            fail('Ходът вече е променен. Опитай отново.', 409)
        if p['id'] == r['turn']:
            fail('Отговорът се потвърждава от друг играч.', 403)
        current = next(x for x in r['players'] if x['id'] == r['turn'])
        r['event'] = current['name'] + {'yes': ' получи „Да“ и продължава.', 'no': ' получи „Не“. Следващ играч!', 'solved': ' позна самоличността си!'}[a]
        if a == 'solved':
            current['rank'] = sum(x['rank'] is not None for x in r['players'])+1
            advance(r)
        elif a == 'no':
            advance(r)
    elif a == 'leave':
        if r['phase'] == 'playing':
            fail('По време на рунд можеш да се върнеш със същия браузър. Напусни след края.')
        r['players'].remove(p)
        if r['host'] == p['id'] and r['players']:
            r['host'] = r['players'][0]['id']
        r.update(revision=r['revision']+1, updated=time.time())
        return {'left': True}
    else:
        fail('Непознато действие.')
    r.update(revision=r['revision']+1, updated=time.time())
    return view(r, p)


@app.get('/image/{card_id}')
async def image(card_id: str):
    c = CARD_MAP.get(card_id)
    if not c:
        fail('Липсваща карта.', 404)
    if c.get('photo'):
        return RedirectResponse(c['photo'])
    async with image_locks[card_id]:
        cached = image_cache.get(card_id)
        if cached and time.time()-cached['at'] < (86400 if cached.get('url') else 120):
            url = cached.get('url')
        else:
            url = None
            try:
                def resolve_image():
                    req = urllib.request.Request('https://en.wikipedia.org/api/rest_v1/page/summary/' + quote(c['wiki']), headers={'User-Agent':'KoiSamAz/1.0 (photo party game)'})
                    with urllib.request.urlopen(req, timeout=12) as response:
                        return json.load(response).get('thumbnail', {}).get('source')
                url = await asyncio.to_thread(resolve_image)
            except (OSError, ValueError):
                pass
            image_cache[card_id] = {'at': time.time(), 'url': url}
    if url and url.startswith('https://upload.wikimedia.org/'):
        return RedirectResponse(url, headers={'Cache-Control':'public, max-age=86400'})
    return Response('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300"><rect width="400" height="300" fill="#d9e8dc"/><text x="200" y="170" text-anchor="middle" font-size="70" fill="#254d40">?</text></svg>', media_type='image/svg+xml', headers={'Cache-Control':'public, max-age=60'})


@app.get('/ads.txt')
def ads_txt():
    return Response('google.com, pub-4652878874110312, DIRECT, f08c47fec0942fa0\n', media_type='text/plain')


@app.get('/')
def index():
    return FileResponse(ROOT / 'index.html', headers={'Cache-Control':'no-cache'})


@app.get('/{filename}')
def assets(filename: str):
    if filename not in ('app.js', 'style.css', 'icon.svg', 'manifest.webmanifest'):
        fail('Страницата не съществува.', 404)
    return FileResponse(ROOT / filename)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('PORT', '3003')), proxy_headers=True)
