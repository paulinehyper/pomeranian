import sqlite3
import os
from datetime import datetime

def get_db_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cards.db")

def init_card_db():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS cards (
            card_id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT,
            body TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_card(card_id, subject, body):
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    if card_id is None:
        c.execute('''
            INSERT INTO cards (subject, body) VALUES (?, ?)
        ''', (subject, body))
        card_id = c.lastrowid
    else:
        c.execute('''
            UPDATE cards SET subject=?, body=?, updated_at=CURRENT_TIMESTAMP WHERE card_id=?
        ''', (subject, body, card_id))
    conn.commit()
    conn.close()
    return card_id

def load_cards():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('SELECT card_id, subject, body FROM cards ORDER BY card_id ASC')
    rows = c.fetchall()
    conn.close()
    cards = []
    for row in rows:
        cards.append({
            'card_id': row[0],
            'subject': row[1],
            'body': row[2]
        })
    return cards
