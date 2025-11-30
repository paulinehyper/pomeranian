import sqlite3
import os

def get_db_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "emails.db")

def init_email_db():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS emails (
            msg_id TEXT PRIMARY KEY,
            subject TEXT,
            subject_norm TEXT,
            sender TEXT,
            date_header TEXT,
            body TEXT,
            full_text TEXT,
            category TEXT,
            due_date TEXT,
            is_completed INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def save_emails_to_db(email_list):
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    for email in email_list:
        c.execute('''
            INSERT OR REPLACE INTO emails (msg_id, subject, subject_norm, sender, date_header, body, full_text, category, due_date, is_completed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            email.get('msg_id'),
            email.get('subject'),
            email.get('subject_norm'),
            email.get('from'),
            email.get('date_header'),
            email.get('body'),
            email.get('full_text'),
            email.get('category'),
            email.get('due_date').strftime('%Y-%m-%d') if email.get('due_date') else None,
            int(email.get('is_completed', False))
        ))
    conn.commit()
    conn.close()

def load_emails_from_db():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('SELECT msg_id, subject, subject_norm, sender, date_header, body, full_text, category, due_date, is_completed FROM emails')
    rows = c.fetchall()
    conn.close()
    emails = []
    for row in rows:
        emails.append({
            'msg_id': row[0],
            'subject': row[1],
            'subject_norm': row[2],
            'from': row[3],
            'date_header': row[4],
            'body': row[5],
            'full_text': row[6],
            'category': row[7],
            'due_date': datetime.strptime(row[8], '%Y-%m-%d').date() if row[8] else None,
            'is_completed': bool(row[9])
        })
    return emails
