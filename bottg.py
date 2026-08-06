import json
import re
import sqlite3
from urllib.parse import urlparse, urljoin
import cloudscraper
from bs4 import BeautifulSoup
import telebot
from telebot import types

# ==============================================================================
# НАСТРОЙКИ И БАЗА ДАННЫХ
# ==============================================================================
TOKEN = '8922084961:AAEsofBAFeqY8TrZNJR-gjtabC_UaLmZ1mE'

bot = telebot.TeleBot(TOKEN)

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
            description TEXT,
            photos TEXT,
            photo_msg_ids TEXT
        )
    ''')
    cursor.execute("PRAGMA table_info(listings)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'photos' not in columns:
        cursor.execute("ALTER TABLE listings ADD COLUMN photos TEXT")
    if 'photo_msg_ids' not in columns:
        cursor.execute("ALTER TABLE listings ADD COLUMN photo_msg_ids TEXT")
    
    conn.commit()
    conn.close()

init_db()

def save_listing(user_id, title, price, czynsz, url, description, photos_list, photo_msg_ids=None):
    conn = sqlite3.connect('rent_bot.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM listings WHERE user_id = ? AND url = ?', (user_id, url))
    photos_json = json.dumps(photos_list) if photos_list else '[]'
    msg_ids_json = json.dumps(photo_msg_ids) if photo_msg_ids else '[]'
    cursor.execute('''
        INSERT INTO listings (user_id, title, price, czynsz, url, description, photos, photo_msg_ids)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, title, price, czynsz, url, description, photos_json, msg_ids_json))
    item_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return item_id

def update_photo_msg_ids(item_id, msg_ids):
    conn = sqlite3.connect('rent_bot.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE listings SET photo_msg_ids = ? WHERE id = ?', (json.dumps(msg_ids), item_id))
    conn.commit()
    conn.close()

def delete_listing(item_id, user_id):
    conn = sqlite3.connect('rent_bot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT photo_msg_ids FROM listings WHERE id = ? AND user_id = ?', (item_id, user_id))
    row = cursor.fetchone()
    photo_msg_ids = []
    if row and row[0]:
        try:
            photo_msg_ids = json.loads(row[0])
        except Exception:
            pass
            
    cursor.execute('DELETE FROM listings WHERE id = ? AND user_id = ?', (item_id, user_id))
    conn.commit()
    conn.close()
    return photo_msg_ids

def get_listing_by_id(item_id):
    conn = sqlite3.connect('rent_bot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT id, title, price, czynsz, url, description, photos FROM listings WHERE id = ?', (item_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def get_user_listings(user_id):
    conn = sqlite3.connect('rent_bot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT id, title, price, czynsz, url, description, photos, photo_msg_ids FROM listings WHERE user_id = ? ORDER BY id ASC', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

# ==============================================================================
# SCRAPER & UTILS
# ==============================================================================
scraper = cloudscraper.create_scraper(
    browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
)

COOKIES = {'l_obu': '1', 'ora_captcha': '0', 'data_protection_consent': 'true'}

def clean_photo_url(url, base_url):
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if url.startswith('//'):
        url = 'https:' + url
    elif url.startswith('/'):
        parsed = urlparse(base_url)
        url = f"{parsed.scheme}://{parsed.netloc}{url}"
    
    # Удаляем параметры размера для точного отсеивания дублей
    clean_base = url.split('?')[0]
    if clean_base.startswith(('http://', 'https://')):
        return clean_base
    return None

def get_page_html(url):
    clean_url = url.split('?')[0]
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
        'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
        'Referer': 'https://www.google.com/'
    }
    
    try:
        res = scraper.get(clean_url, headers=headers, cookies=COOKIES, timeout=12)
        if 'olx.pl' in clean_url and ('Ogłoszenia - Sprzedam' in res.text or 'd/oferta/' not in res.url):
            mobile_headers = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15',
                'Accept-Language': 'pl-PL,pl;q=0.9'
            }
            res_m = scraper.get(clean_url, headers=mobile_headers, cookies=COOKIES, timeout=12)
            if res_m.status_code == 200:
                return res_m.text, clean_url

        if res.status_code == 200:
            return res.text, clean_url
        return None, clean_url
    except Exception as e:
        print(f"Ошибка запроса: {e}")
        return None, clean_url

def find_czynsz_in_text(text, main_price=None):
    if not text:
        return None
    patterns = [
        r'(?:czynsz|opłaty\s+administracyjne|opłaty\s+dodatkowe|media)\s*[:\-]?\s*(\d[\d\s\,\.]*)\s*(zł|PLN)',
        r'(\d[\d\s\,\.]*)\s*(zł|PLN)\s*(?:czynsz|opłat|media)'
    ]
    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            val = f"{match.group(1).strip()} {match.group(2)}"
            if main_price and val.replace(' ', '') == main_price.replace(' ', ''):
                continue
            return val
    return None

def check_is_active(url):
    html, clean_url = get_page_html(url)
    if not html:
        return True
    
    html_lower = html.lower()
    if 'otodom.pl' in url:
        return not ('to ogłoszenie nie jest już dostępne' in html_lower or 'nie znaleziono ogłoszenia' in html_lower)
    elif 'olx.pl' in url:
        return not ('ogłoszenie nie jest już dostępne' in html_lower or 'to ogłoszenie nie jest dostępne' in html_lower or 'brak ogłoszenia' in html_lower)
    elif 'nieruchomosci-online.pl' in url:
        return not ('ogłoszenie wygasło' in html_lower or '404' in html_lower)
    return True

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
                    img_url = img.get('large') or img.get('medium')
                    if img_url:
                        photos.append(img_url)
        except Exception:
            pass

    return title, price, czynsz, description, photos

def parse_olx(soup):
    title, price, czynsz, description, photos = None, None, None, None, []

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
                    
                    for param in ad.get('params', []):
                        if isinstance(param, dict):
                            p_name = str(param.get('name', '')).lower()
                            if 'czynsz' in p_name or 'opłaty' in p_name:
                                val = param.get('value', {})
                                if isinstance(val, dict):
                                    czynsz = val.get('label')
                                elif isinstance(val, (str, int)):
                                    czynsz = f"{val} PLN"

                    if not description:
                        raw_desc = ad.get('description', '')
                        description = BeautifulSoup(raw_desc, 'html.parser').get_text(separator='\n').strip()

                    for photo in ad.get('photos', []):
                        u = photo.get('link') if isinstance(photo, dict) else photo
                        if u:
                            photos.append(str(u))
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
        d_elem = soup.find('div', {'data-cy': 'ad_description'}) or soup.find('div', class_=re.compile(r'description', re.I))
        if d_elem:
            description = d_elem.get_text(separator='\n', strip=True)

    return title, price, czynsz, description, photos

def parse_nieruchomosci_online(soup):
    title, price, czynsz, description, photos = None, None, None, None, []

    # Заголовок
    t_elem = soup.find('h1', class_=re.compile(r'title|header', re.I)) or soup.find('h1')
    if t_elem:
        title = t_elem.get_text(strip=True)

    # Цена
    p_elem = soup.find('span', class_=re.compile(r'price-main|info-price', re.I)) or soup.find(class_=re.compile(r'price', re.I))
    if p_elem:
        price = p_elem.get_text(strip=True)

    # Описание
    desc_elem = soup.find('div', id='description') or soup.find('div', class_=re.compile(r'box-description|details-desc|text-content', re.I))
    if desc_elem:
        description = desc_elem.get_text(separator='\n', strip=True)
    else:
        og_desc = soup.find('meta', property='og:description')
        if og_desc and og_desc.get('content') and "Najlepszy portal" not in og_desc['content']:
            description = og_desc['content']

    # Чинш
    for li in soup.find_all(['li', 'tr', 'p']):
        txt = li.get_text(strip=True).lower()
        if ('czynsz' in txt or 'opłaty' in txt) and 'cena' not in txt:
            match = re.search(r'(\d[\d\s\,\.]*)\s*(zł|PLN)', li.get_text(strip=True), re.I)
            if match:
                found = f"{match.group(1).strip()} {match.group(2)}"
                if price and found.replace(' ', '') != price.replace(' ', ''):
                    czynsz = found
                    break

    # Фотографии
    for img in soup.find_all(['img', 'a']):
        src = img.get('data-src') or img.get('src') or img.get('href')
        if src and ('static' in src or 'photos' in src or 'media' in src) and not src.endswith(('.svg', '.png', '.gif')):
            photos.append(src)

    return title, price, czynsz, description, photos

def fetch_listing_data(url):
    html, clean_url = get_page_html(url)
    if not html:
        return None

    soup = BeautifulSoup(html, 'html.parser')
    domain = urlparse(clean_url).netloc.lower()

    if 'otodom' in domain:
        title, price, czynsz, description, photos = parse_otodom(soup)
    elif 'olx' in domain:
        title, price, czynsz, description, photos = parse_olx(soup)
    elif 'nieruchomosci-online' in domain:
        title, price, czynsz, description, photos = parse_nieruchomosci_online(soup)
    else:
        title, price, czynsz, description, photos = None, None, None, None, []

    if not title or title.lower() in ['nieruchomości online', 'apartament wrocław', 'mieszkanie na wynajem']:
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            title = og_title['content'].split('|')[0].split('-')[0].strip()

    if not czynsz and description:
        czynsz = find_czynsz_in_text(description, price)

    valid_photos = []
    for p in photos:
        cleaned = clean_photo_url(p, clean_url)
        if cleaned and cleaned not in valid_photos:
            valid_photos.append(cleaned)

    return {
        'title': title or 'Объявление',
        'price': price or 'Не указана',
        'czynsz': czynsz or 'Не указан',
        'description': description or 'Описание отсутствует.',
        'photos': valid_photos,
        'url': clean_url
    }

# ==============================================================================
# TELEGRAM ОБРАБОТЧИКИ
# ==============================================================================
def render_card_text(title, price, czynsz, description, is_active=True, expanded=False):
    status_str = "🟢 <b>Активно</b>" if is_active else "🔴 <b>Завершено / Неактивно</b>"
    
    if not expanded and len(description) > 150:
        short_desc = description[:150].rsplit(' ', 1)[0] + "..."
    else:
        short_desc = description

    return (
        f"Статус: {status_str}\n\n"
        f"🏠 <b>{title}</b>\n"
        f"💰 <b>Цена:</b> {price}\n"
        f"🏷 <b>Чинш / Opłaty:</b> {czynsz}\n\n"
        f"📝 <b>Описание:</b>\n{short_desc}"
    )

def build_inline_keyboard(item_id, url, expanded=False):
    markup = types.InlineKeyboardMarkup()
    toggle_btn = types.InlineKeyboardButton("📖 Свернуть описание" if expanded else "📖 Показать описание", callback_data=f"toggle_{item_id}_{1 if expanded else 0}")
    markup.add(toggle_btn)
    markup.add(types.InlineKeyboardButton("🔗 Открыть на сайте", url=url))
    markup.add(types.InlineKeyboardButton("🗑 Удалить из базы", callback_data=f"del_{item_id}"))
    return markup

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "📥 Send me a link to a listing to save it.", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).row("➕ Add new", "📋 List"))

@bot.message_handler(func=lambda m: m.text and ('Add new' in m.text or 'Добавить' in m.text))
def handle_add_new_button(message):
    bot.send_message(message.chat.id, "Пришлите ссылку на квартиру.")

@bot.message_handler(func=lambda m: m.text and ('List' in m.text or 'Список' in m.text or m.text == '/list'))
def handle_list_button(message):
    listings = get_user_listings(message.chat.id)
    if not listings:
        bot.send_message(message.chat.id, "📋 Ваш список пуст.")
        return

    for item in listings:
        item_id, title, price, czynsz, url, description, photos_json, _ = item
        is_active = check_is_active(url)
        photos = json.loads(photos_json) if photos_json else []

        photo_msg_ids = []
        if photos:
            media = [types.InputMediaPhoto(p) for p in photos[:10]]
            try:
                sent_msgs = bot.send_media_group(message.chat.id, media)
                photo_msg_ids = [m.message_id for m in sent_msgs]
            except Exception as e:
                print(f"Ошибка отправки фото: {e}")

        card_text = render_card_text(title, price, czynsz, description, is_active=is_active, expanded=False)
        markup = build_inline_keyboard(item_id, url, expanded=False)
        sent_card = bot.send_message(message.chat.id, card_text, parse_mode='HTML', reply_markup=markup)
        
        # Записываем актуальные ID сообщений с фото для корректного удаления
        update_photo_msg_ids(item_id, photo_msg_ids)

@bot.message_handler(func=lambda m: m.text and m.text.startswith(('http://', 'https://')))
def handle_link(message):
    url = message.text.strip()
    loading_msg = bot.send_message(message.chat.id, "⏳ Загрузка данных...")
    data = fetch_listing_data(url)

    if not data or data['title'] == 'Объявление':
        bot.edit_message_text("❌ Не удалось загрузить данные по ссылке.", message.chat.id, loading_msg.message_id)
        return

    try:
        bot.delete_message(message.chat.id, loading_msg.message_id)
    except Exception:
        pass

    photo_msg_ids = []
    if data['photos']:
        media = [types.InputMediaPhoto(p) for p in data['photos'][:10]]
        try:
            sent_msgs = bot.send_media_group(message.chat.id, media)
            photo_msg_ids = [m.message_id for m in sent_msgs]
        except Exception as e:
            print(f"Ошибка отправки альбома: {e}")

    item_id = save_listing(
        user_id=message.chat.id,
        title=data['title'],
        price=data['price'],
        czynsz=data['czynsz'],
        url=data['url'],
        description=data['description'],
        photos_list=data['photos'],
        photo_msg_ids=photo_msg_ids
    )

    card_text = render_card_text(data['title'], data['price'], data['czynsz'], data['description'], is_active=True, expanded=False)
    markup = build_inline_keyboard(item_id, data['url'], expanded=False)
    bot.send_message(message.chat.id, card_text, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('toggle_'))
def handle_toggle_description(call):
    _, item_id_str, state_str = call.data.split('_')
    item_id = int(item_id_str)
    currently_expanded = bool(int(state_str))

    item = get_listing_by_id(item_id)
    if not item:
        bot.answer_callback_query(call.id, "Объявление не найдено в базе.")
        return

    _, title, price, czynsz, url, description, _ = item
    new_expanded = not currently_expanded
    
    card_text = render_card_text(title, price, czynsz, description, is_active=True, expanded=new_expanded)
    markup = build_inline_keyboard(item_id, url, expanded=new_expanded)

    try:
        bot.edit_message_text(card_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('del_'))
def handle_delete(call):
    try:
        item_id = int(call.data.split('_')[1])
        photo_msg_ids = delete_listing(item_id, call.message.chat.id)
        
        # Удаление альбома фотографий
        for m_id in photo_msg_ids:
            try:
                bot.delete_message(call.message.chat.id, m_id)
            except Exception:
                pass
                
        # Удаление основного текстового сообщения
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Объект и фото успешно удалены!")
    except Exception as e:
        print(f"Ошибка удаления: {e}")

if __name__ == '__main__':
    print("Бот запущен...")
    bot.infinity_polling()
