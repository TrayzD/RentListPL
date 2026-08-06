import os
import io
import json
import re
import uuid
import sqlite3
import requests
from bs4 import BeautifulSoup
import telebot
from telebot import types

# Токен берется из переменных окружения Render или вставляется напрямую для локального теста
TOKEN = os.environ.get('BOT_TOKEN', 'ВАШ_ТОКЕН_BOTFATHER')
bot = telebot.TeleBot(TOKEN)

# Хранилище описаний в памяти
descriptions_db = {}

# --- 1. РАБОТА С БАЗОЙ ДАННЫХ SQLITE ---
def init_db():
    conn = sqlite3.connect('flats.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS flats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            UNIQUE(chat_id, url)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def add_flat_to_db(chat_id, url):
    conn = sqlite3.connect('flats.db')
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT OR IGNORE INTO flats (chat_id, url) VALUES (?, ?)', (chat_id, url))
        conn.commit()
    finally:
        conn.close()

def get_user_flats(chat_id):
    conn = sqlite3.connect('flats.db')
    cursor = conn.cursor()
    cursor.execute('SELECT id, url FROM flats WHERE chat_id = ?', (chat_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows  # [(1, 'https://...'), (2, 'https://...')]

def delete_flat_from_db(flat_id, chat_id):
    conn = sqlite3.connect('flats.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM flats WHERE id = ? AND chat_id = ?', (flat_id, chat_id))
    conn.commit()
    conn.close()

# --- 2. ПАРСИНГ ОБЪЯВЛЕНИЙ (OLX / Otodom) ---
def parse_listing(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7'
    }
    try:
        res = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
        if res.status_code == 404 or res.url.rstrip('/') in ['https://www.olx.pl', 'https://www.otodom.pl']:
            return {'status': '🔴 Неактивно (Удалено/404)', 'title': 'Объявление не найдено', 'price': '-', 'czynsz': '-', 'photos': [], 'desc': '', 'url': url}

        soup = BeautifulSoup(res.text, 'html.parser')
        text = soup.get_text().lower()

        if any(p in text for p in ['to ogłoszenie nie jest już dostępne', 'ogłoszenie nieaktualne', 'oferta została zakończona']):
            return {'status': '🔴 Неактивно (Снято с публикации)', 'title': 'Объявление больше не актуально', 'price': '-', 'czynsz': '-', 'photos': [], 'desc': '', 'url': url}

        title, price, photos, desc = None, None, [], None
        
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string)
                if isinstance(data, list): data = data[0]
                if isinstance(data, dict):
                    title = title or data.get('name')
                    desc = desc or data.get('description')
                    
                    if 'offers' in data:
                        offers = data['offers'][0] if isinstance(data['offers'], list) else data['offers']
                        if offers.get('price'): 
                            price = f"{offers.get('price')} {offers.get('priceCurrency', 'PLN')}"
                    
                    if 'image' in data:
                        imgs = data['image']
                        if isinstance(imgs, list): photos.extend(imgs)
                        elif isinstance(imgs, str): photos.append(imgs)
            except Exception:
                continue

        if not photos:
            for og in soup.find_all('meta', property='og:image'):
                if og.get('content'): photos.append(og['content'])

        if not title:
            og_t = soup.find('meta', property='og:title')
            title = og_t['content'] if og_t else 'Без названия'
            
        if not desc:
            og_d = soup.find('meta', property='og:description')
            desc = og_d['content'] if og_d else ''

        if not price:
            pm = re.search(r'(\d[\d\s]*)\s*(zł|PLN)', soup.get_text(), re.IGNORECASE)
            price = pm.group(0).strip() if pm else 'Не указана'

        # Поиск стоимости коммуналки (Czynsz)
        czynsz = None
        czynsz_match = re.search(r'czynsz[:\s]*(\d[\d\s]*)\s*(zł|PLN)', soup.get_text(), re.IGNORECASE)
        if czynsz_match:
            czynsz = f"{czynsz_match.group(1).strip()} {czynsz_match.group(2)}"
        else:
            param_match = re.search(r'(?:czynsz|додатково|opłaty)[^\d]*(\d[\d\s]{1,5})\s*(zł|PLN)', soup.get_text(), re.IGNORECASE)
            if param_match:
                czynsz = f"{param_match.group(1).strip()} {param_match.group(2)}"

        desc_clean = ' '.join(desc.split()) if desc else ''
        unique_photos = list(dict.fromkeys(photos))[:10]

        return {
            'status': '🟢 Активно',
            'title': title,
            'price': price,
            'czynsz': czynsz or 'Не указан / Включен',
            'photos': unique_photos,
            'desc': desc_clean,
            'url': url
        }
    except Exception:
        return {'status': '⚠️ Ошибка доступа', 'title': 'Не удалось загрузить данные', 'price': '-', 'czynsz': '-', 'photos': [], 'desc': '', 'url': url}

# --- 3. ОТПРАВКА КАРТОЧКИ ОБЪЯВЛЕНИЯ ---
def send_flat_card(chat_id, flat_id, data):
    price_str = f"<b>Цена:</b> {data['price']}"
    if data['czynsz'] and data['czynsz'] != 'Не указан / Включен':
        price_str += f" + {data['czynsz']} (Czynsz)"

    full_desc = data['desc']
    short_desc = (full_desc[:120] + "...") if len(full_desc) > 120 else (full_desc or "Без описания")

    desc_id = str(uuid.uuid4())[:8]
    descriptions_db[desc_id] = full_desc or "Полное описание отсутствует."

    caption = (
        f"<b>Статус:</b> {data['status']}\n\n"
        f"🏠 <b>{data['title']}</b>\n"
        f"💰 {price_str}\n\n"
        f"📝 <b>Описание:</b> {short_desc}"
    )

    markup = types.InlineKeyboardMarkup()
    btn_link = types.InlineKeyboardButton("🔗 Открыть на сайте", url=data['url'])
    btn_del = types.InlineKeyboardButton("🗑 Удалить из базы", callback_data=f"del_{flat_id}")
    
    if len(full_desc) > 120:
        btn_more = types.InlineKeyboardButton("📖 Читать полностью", callback_data=f"read_{desc_id}")
        markup.row(btn_more)
        
    markup.row(btn_link)
    markup.row(btn_del)  # Кнопка удаления присутствует всегда!

    if data['photos'] and "🟢 Активно" in data['status']:
        media = []
        for i, photo_url in enumerate(data['photos']):
            if i == 0:
                media.append(types.InputMediaPhoto(photo_url, caption=caption, parse_mode='HTML'))
            else:
                media.append(types.InputMediaPhoto(photo_url))
        try:
            bot.send_media_group(chat_id, media)
            bot.send_message(chat_id, "⚙️ Управление объявлением:", reply_markup=markup)
            return
        except Exception:
            pass

    bot.send_message(chat_id, caption, parse_mode='HTML', reply_markup=markup)

# --- 4. ХЕНДЛЕРЫ КОМАНД И КНОПОК ---
def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("➕ Add new"), types.KeyboardButton("📋 List"))
    return markup

@bot.message_handler(commands=['start'])
def start_cmd(message):
    bot.send_message(
        message.chat.id,
        "👋 **Бот мониторинга квартир готов к работе!**\n\n"
        "• **➕ Add new** — добавить новую ссылку.\n"
        "• **📋 List** — посмотреть все варианты.\n"
        "Неактивные объявления можно удалить кнопкой 🗑 **Удалить из базы** прямо под ними.",
        reply_markup=get_main_keyboard(),
        parse_mode='Markdown'
    )

@bot.message_handler(func=lambda msg: msg.text in ["➕ Add new", "📋 List"])
def menu_buttons_handler(message):
    chat_id = message.chat.id
    if message.text == "➕ Add new":
        bot.send_message(chat_id, "📥 Отправьте ссылку на объявление с OLX или Otodom:")
    elif message.text == "📋 List":
        flats = get_user_flats(chat_id)
        if not flats:
            bot.send_message(chat_id, "Ваша база пуста! Нажмите **➕ Add new** и отправьте первую ссылку.")
            return
        
        bot.send_message(chat_id, f"📋 **Сохраненные варианты ({len(flats)} шт.):**", parse_mode='Markdown')
        for flat_id, url in flats:
            data = parse_listing(url)
            send_flat_card(chat_id, flat_id, data)

@bot.callback_query_handler(func=lambda call: call.data.startswith('read_'))
def handle_read_more(call):
    desc_id = call.data.split('read_')[1]
    full_text = descriptions_db.get(desc_id, "Текст описания не найден.")
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"📄 **Полное описание:**\n\n{full_text}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('del_'))
def handle_delete_flat(call):
    flat_id = int(call.data.split('del_')[1])
    delete_flat_from_db(flat_id, call.message.chat.id)
    bot.answer_callback_query(call.id, text="🗑 Объявление успешно удалено!")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        bot.send_message(call.message.chat.id, "✅ Объявление удалено из вашей базы!")

@bot.message_handler(func=lambda msg: True)
def process_links(message):
    chat_id = message.chat.id
    url = message.text.strip()

    if not (url.startswith('http://') or url.startswith('https://')):
        bot.send_message(message.chat.id, "Отправьте корректную ссылку (http/https) или используйте меню ниже.", reply_markup=get_main_keyboard())
        return

    msg = bot.send_message(chat_id, "⏳ Считываем данные объявления...")
    add_flat_to_db(chat_id, url)
    
    # Получаем сгенерированный ID из базы
    flats = get_user_flats(chat_id)
    flat_id = [f[0] for f in flats if f[1] == url][-1]

    data = parse_listing(url)
    bot.delete_message(chat_id, msg.message_id)
    
    send_flat_card(chat_id, flat_id, data)

print("Бот с кнопкой удаления неактивных вариантов запущен...")
bot.infinity_polling()