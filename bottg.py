import json
import re
import sqlite3
from urllib.parse import urlparse
import cloudscraper
from bs4 import BeautifulSoup
import telebot
from telebot import types

# ==============================================================================
# НАСТРОЙКИ И БАЗА ДАННЫХ
# ==============================================================================
TOKEN = '8922084961:AAEsofBAFeqY8TrZNJR-gjtabC_UaLmZ1mE'

bot = telebot.TeleBot(TOKEN)

# Инициализация базы данных SQLite
def init_db():
    conn = sqlite3.connect('rent_bot.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            price TEXT,
            czynsz TEXT,
            url TEXT,
            description TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def save_listing(user_id, title, price, czynsz, url, description):
    conn = sqlite3.connect('rent_bot.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM listings WHERE user_id = ? AND url = ?', (user_id, url))
    cursor.execute('''
        INSERT INTO listings (user_id, title, price, czynsz, url, description)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, title, price, czynsz, url, description))
    item_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return item_id

def delete_listing(item_id, user_id):
    conn = sqlite3.connect('rent_bot.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM listings WHERE id = ? AND user_id = ?', (item_id, user_id))
    conn.commit()
    conn.close()

def get_user_listings(user_id):
    conn = sqlite3.connect('rent_bot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT id, title, price, czynsz, url FROM listings WHERE user_id = ? ORDER BY id DESC', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

# ==============================================================================
# SCRAPER & COOKIES
# ==============================================================================
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

COOKIES = {
    'l_obu': '1',
    'ora_captcha': '0',
    'data_protection_consent': 'true'
}

def get_page_html(url):
    clean_url = url.split('?')[0]
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
        'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
        'Referer': 'https://www.google.com/'
    }
    
    try:
        res = scraper.get(clean_url, headers=headers, cookies=COOKIES, timeout=15)
        if 'olx.pl' in clean_url and ('Ogłoszenia - Sprzedam' in res.text or 'd/oferta/' not in res.url):
            mobile_headers = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Safari/604.1',
                'Accept-Language': 'pl-PL,pl;q=0.9'
            }
            res_m = scraper.get(clean_url, headers=mobile_headers, cookies=COOKIES, timeout=15)
            if res_m.status_code == 200:
                return res_m.text, clean_url

        if res.status_code == 200:
            return res.text, clean_url
        return None, clean_url
    except Exception as e:
        print(f"Ошибка запроса к {url}: {e}")
        return None, clean_url

def find_czynsz_in_text(text):
    if not text:
        return None
    match = re.search(r'(?:czynsz|opłaty\s+administracyjne|opłaty\s+dodatkowe)\s*[:\-]?\s*(\d[\d\s\.]*)\s*(zł|PLN)', text, re.IGNORECASE)
    if match:
        return f"{match.group(1).strip()} {match.group(2)}"
    return None

# ==============================================================================
# ПАРСЕРЫ
# ==============================================================================
def parse_otodom(soup):
    title, price, czynsz, description, photos = None, None, None, None, []
    script = soup.find('script', id='__NEXT_DATA__')
    if script and script.string:
        try:
            data = json.loads(script.string)
            ad = data.get('props', {}).get('pageProps', {}).get('ad', {})
            if ad:
                title = ad.get('title')
                target = ad.get('target', {})
                p_val = target.get('Price')
                p_curr = target.get('Currency', 'PLN')
                if p_val:
                    price = f"{p_val} {p_curr}"

                rent_val = target.get('Rent')
                if rent_val:
                    czynsz = f"{rent_val} PLN"

                raw_desc = ad.get('description', '')
                description = BeautifulSoup(raw_desc, 'html.parser').get_text(separator='\n').strip()

                for img in ad.get('images', []):
                    img_url = img.get('large') or img.get('medium') or img.get('small')
                    if img_url:
                        photos.append(img_url)
        except Exception as e:
            print(f"Ошибка Otodom: {e}")

    return title, price, czynsz, description, photos

def parse_olx(soup):
    title, price, czynsz, description, photos = None, None, None, None, []

    for s in soup.find_all('script', type='application/ld+json'):
        if s.string:
            try:
                data = json.loads(s.string)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict) and item.get('@type') in ['Product', 'SinglePosting', 'Offer', 'Accommodation']:
                        title = title or item.get('name')
                        if 'offers' in item and isinstance(item['offers'], dict):
                            p = item['offers'].get('price')
                            c = item['offers'].get('priceCurrency', 'PLN')
                            if p:
                                price = f"{p} {c}"
                        if 'image' in item:
                            imgs = item['image'] if isinstance(item['image'], list) else [item['image']]
                            photos.extend([img for img in imgs if isinstance(img, str)])
                        if 'description' in item and not description:
                            description = item['description']
            except Exception:
                pass

    for script_id in ['__PRERENDERED_STATE__', '__NEXT_DATA__']:
        script = soup.find('script', id=script_id)
        if script and script.string:
            try:
                data = json.loads(script.string)
                ad = None
                if 'ad' in data:
                    ad = data.get('ad', {}).get('ad') or data.get('ad')
                elif 'props' in data:
                    ad = data.get('props', {}).get('pageProps', {}).get('ad')

                if isinstance(ad, dict):
                    title = title or ad.get('title')
                    price_data = ad.get('price', {})
                    if isinstance(price_data, dict) and not price:
                        price = price_data.get('displayValue') or (f"{price_data.get('value')} PLN" if price_data.get('value') else None)
                    
                    if not description:
                        raw_desc = ad.get('description', '')
                        description = BeautifulSoup(raw_desc, 'html.parser').get_text(separator='\n').strip()

                    for photo in ad.get('photos', []):
                        u = photo.get('link') if isinstance(photo, dict) else photo
                        if u:
                            photos.append(str(u).replace('{width}', '1000').replace('{height}', '750'))
            except Exception:
                pass

    if not title:
        t_elem = soup.find('h4', {'data-cy': 'ad_title'}) or soup.find('h1')
        if t_elem:
            title = t_elem.get_text(strip=True)

    if not price:
        p_elem = soup.find('h3', {'data-testid': 'ad-price-container'}) or soup.find('h3')
        if p_elem:
            price = p_elem.get_text(strip=True)

    if not description:
        d_elem = soup.find('div', {'data-cy': 'ad_description'})
        if d_elem:
            description = d_elem.get_text(separator='\n', strip=True)

    if len(photos) <= 1:
        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src')
            if src and ('apollo-ireland.akamaized.net' in src or 'olx' in src) and src.startswith('http'):
                photos.append(src)

    return title, price, czynsz, description, photos

def parse_nieruchomosci_online(soup):
    title, price, czynsz, description, photos = None, None, None, None, []

    for s in soup.find_all('script', type='application/ld+json'):
        if s.string:
            try:
                data = json.loads(s.string)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict):
                        if 'name' in item and not title:
                            title = item['name']
                        if 'description' in item and not description:
                            description = item['description']
                        if 'offers' in item and isinstance(item['offers'], dict):
                            p = item['offers'].get('price')
                            if p:
                                price = f"{p} PLN"
                        if 'image' in item:
                            imgs = item['image'] if isinstance(item['image'], list) else [item['image']]
                            photos.extend([i.get('contentUrl', i) if isinstance(i, dict) else i for i in imgs])
            except Exception:
                pass

    if not title:
        t_elem = soup.find('h1')
        if t_elem:
            title = t_elem.get_text(strip=True)

    if not price:
        p_elem = soup.find(class_=re.compile(r'price-main|info-price|price', re.I))
        if p_elem:
            price = p_elem.get_text(strip=True)

    if not description:
        d_elem = soup.find('div', id='description') or soup.find(class_=re.compile(r'description|desc-content', re.I))
        if d_elem:
            description = d_elem.get_text(separator='\n', strip=True)

    if len(photos) <= 1:
        for a in soup.find_all(['a', 'img']):
            href = a.get('href') or a.get('src') or a.get('data-src')
            if href and ('/photo/' in href or '/media/' in href or 'img' in href) and href.startswith('http'):
                if href not in photos and not href.endswith(('.svg', '.png')) and 'logo' not in href:
                    photos.append(href)

    return title, price, czynsz, description, photos

def fetch_listing_data(url):
    html, clean_url = get_page_html(url)
    if not html:
        return None

    soup = BeautifulSoup(html, 'html.parser')
    domain = urlparse(clean_url).netloc.lower()

    title, price, czynsz, description, photos = None, None, None, None, []

    if 'otodom' in domain:
        title, price, czynsz, description, photos = parse_otodom(soup)
    elif 'olx' in domain:
        title, price, czynsz, description, photos = parse_olx(soup)
    elif 'nieruchomosci-online' in domain:
        title, price, czynsz, description, photos = parse_nieruchomosci_online(soup)

    if not title:
        og_title = soup.find('meta', property='og:title')
        title = og_title['content'] if og_title and og_title.get('content') else 'Без названия'

    if not description:
        og_desc = soup.find('meta', property='og:description')
        description = og_desc['content'] if og_desc and og_desc.get('content') else 'Без описания'

    if not czynsz and description:
        czynsz = find_czynsz_in_text(description)

    if description and len(description) > 800:
        description = description[:800] + '...'

    valid_photos = []
    for p in photos:
        if p and isinstance(p, str) and p.startswith('http') and p not in valid_photos:
            valid_photos.append(p)

    return {
        'title': title or 'Без названия',
        'price': price or 'Не указана',
        'czynsz': czynsz or 'Не указан',
        'description': description or 'Без описания',
        'photos': valid_photos,
        'url': clean_url
    }

# ==============================================================================
# TELEGRAM ОБРАБОТЧИКИ
# ==============================================================================
def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("➕ Add new"), types.KeyboardButton("📋 List"))
    return markup

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(
        message,
        "📥 <b>Отправьте ссылку на объявление</b>\nПоддерживаются: Otodom, OLX, Nieruchomosci-online и др.",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(func=lambda m: m.text and ('Add new' in m.text or 'Добавить' in m.text))
def handle_add_new_button(message):
    bot.send_message(message.chat.id, "Пришлите ссылку на квартиру, чтобы добавить её в базу.")

@bot.message_handler(func=lambda m: m.text and ('List' in m.text or 'Список' in m.text or m.text == '/list'))
def handle_list_button(message):
    listings = get_user_listings(message.chat.id)
    if not listings:
        bot.send_message(message.chat.id, "📋 <b>Ваш список сохраненных объектов пуст.</b>", parse_mode='HTML', reply_markup=get_main_keyboard())
        return

    text = f"📋 <b>Сохраненные объекты ({len(listings)}):</b>\n\n"
    for idx, item in enumerate(listings, start=1):
        item_id, title, price, czynsz, url = item
        text += f"{idx}. <b>{title}</b>\n💰 Цена: {price} | Чинш: {czynsz}\n🔗 <a href='{url}'>Ссылка на объявление</a>\n\n"

    bot.send_message(message.chat.id, text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=get_main_keyboard())

@bot.message_handler(func=lambda m: m.text and m.text.startswith(('http://', 'https://')))
def handle_link(message):
    url = message.text.strip()
    loading_msg = bot.send_message(message.chat.id, "⏳ Загрузка данных и фото...")

    data = fetch_listing_data(url)

    if not data or data['title'] == 'Без названия':
        bot.edit_message_text("❌ Не удалось загрузить данные по этой ссылке.", message.chat.id, loading_msg.message_id)
        return

    try:
        bot.delete_message(message.chat.id, loading_msg.message_id)
    except Exception:
        pass

    # Сохраняем в БД SQLite
    item_id = save_listing(
        user_id=message.chat.id,
        title=data['title'],
        price=data['price'],
        czynsz=data['czynsz'],
        url=data['url'],
        description=data['description']
    )

    if data['photos']:
        media = []
        for photo_url in data['photos'][:10]:
            media.append(types.InputMediaPhoto(photo_url))

        try:
            bot.send_media_group(message.chat.id, media)
        except Exception as e:
            print(f"Ошибка отправки фото: {e}")

    text = (
        f"Статус: 🟢 <b>Сохранено в базу</b>\n\n"
        f"🏠 <b>{data['title']}</b>\n"
        f"💰 <b>Цена:</b> {data['price']}\n"
        f"🏷 <b>Чинш / Opłaty:</b> {data['czynsz']}\n\n"
        f"📝 <b>Описание:</b>\n{data['description']}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔗 Открыть на сайте", url=data['url']))
    markup.add(types.InlineKeyboardButton("🗑 Удалить из базы", callback_data=f"del_{item_id}"))

    bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('del_'))
def handle_delete(call):
    try:
        item_id = int(call.data.split('_')[1])
        delete_listing(item_id, call.message.chat.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Объект удален из базы!")
    except Exception as e:
        print(f"Ошибка удаления: {e}")

if __name__ == '__main__':
    print("Бот запущен и готов к работе...")
    bot.infinity_polling()
