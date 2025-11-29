# -*- coding: utf-8 -*-
"""
KSD 이메일 회신 To-Do 관리 (IMAP + GUI)
- 카테고리 분류용 AI (KSDTodoClassifier)
- 마감일 여부 판단용 AI (KSDDueDateClassifier)
- 두 모델 모두 Naive Bayes + TF-IDF 기반
- 제목 정규화 + 뉴스데일리 자동 참고 처리 포함
- Python 3.8~3.12 호환
"""

import imaplib
import email
import re
from email.header import decode_header
from datetime import datetime, timedelta, date
import csv
from typing import Optional, List, Dict
import sys
import os
import json
import tkinter as tk
from tkinter import ttk, messagebox
import tkinter.font as tkfont
from html import unescape  # HTML → 텍스트 변환용

# ML
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.naive_bayes import MultinomialNB
from sklearn.svm import SVC
from sklearn.ensemble import RandomForestClassifier
from konlpy.tag import Okt
import joblib

# Sentence Transformers for Korean
try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False


# =====================================================
# Configuration
# =====================================================

# 메일 서버 프리셋
MAIL_PRESETS = {
    "KSD 메일": {
        "host": "omail.ksd.or.kr",
        "port": 143,
        "use_ssl": False
    },
    "Gmail": {
        "host": "imap.gmail.com",
        "port": 993,
        "use_ssl": True
    },
    "Naver": {
        "host": "imap.naver.com",
        "port": 993,
        "use_ssl": True
    },
    "Outlook": {
        "host": "outlook.office365.com",
        "port": 993,
        "use_ssl": True
    },
    "Daum": {
        "host": "imap.daum.net",
        "port": 993,
        "use_ssl": True
    },
    "사용자 정의": {
        "host": "",
        "port": 993,
        "use_ssl": True
    }
}

DAYS_LOOKBACK = 7

# 회신 필요 여부(To-Do 후보) 키워드
REPLY_KEYWORDS = ["회신", "답장", "response", "reply", "제출", "보고"]

# 마감일(Deadline) 문맥 키워드
DUE_KEYWORDS = [
    "까지", "제출", "요청", "보고", "마감", "기한",
    "deadline", "due", "회신", "요망", "필요"
]

DEFAULT_CATEGORIES = ["제출", "안내", "검토", "광고"]
DEADLINE_RELATED_CATEGORIES = ["제출"]

# 카테고리 설명
CATEGORY_DESCRIPTIONS = {
    "제출": "답장/회신이 필요하고 제출 기한이 있는 메일",
    "안내": "정보 공유 및 가이드 메일 (답장 불필요)",
    "검토": "확인이 필요하지만 답장 기한이 없는 메일",
    "광고": "마케팅, 프로모션, 뉴스레터 등 홍보성 메일"
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CATEGORY_FILE = os.path.join(BASE_DIR, "todo_categories.json")
CLASSIFICATION_MAP_FILE = os.path.join(BASE_DIR, "classification_map.json")
CLASSIFICATION_DATA_FILE = os.path.join(BASE_DIR, "classification_data.json")
MODEL_FILE = os.path.join(BASE_DIR, "todo_classifier.pkl")

DUE_CLASSIFICATION_DATA_FILE = os.path.join(BASE_DIR, "due_classification_data.json")
DUE_MODEL_FILE = os.path.join(BASE_DIR, "due_classifier.pkl")

SETTINGS_FILE = os.path.join(BASE_DIR, "mail_settings.json")
USER_CATEGORIES_FILE = os.path.join(BASE_DIR, "user_categories.json")
TRAINING_DATA_FILE = os.path.join(BASE_DIR, "training_data.json")


# =====================================================
# Utility Functions
# =====================================================

def decode_mime_words(s: str) -> str:
    if not s:
        return ""
    decoded_fragments = []
    for frag, enc in decode_header(s):
        if isinstance(frag, bytes):
            try:
                decoded_fragments.append(frag.decode(enc or "utf-8", errors="ignore"))
            except Exception:
                decoded_fragments.append(frag.decode("utf-8", errors="ignore"))
        else:
            decoded_fragments.append(frag)
    return "".join(decoded_fragments)


def normalize_subject(subject: str) -> str:
    """
    제목에서 날짜/요일 등을 제거해 동일 제목 패턴으로 정규화.
    """
    if not subject:
        return ""

    subject = re.sub(r"\d{1,2}\s*월\s*\d{1,2}\s*일\s*\([^)]*\)", "", subject)
    subject = re.sub(r"\d{1,2}\s*월\s*\d{1,2}\s*일", "", subject)
    subject = re.sub(r"\d{1,2}[./-]\d{1,2}", "", subject)
    subject = re.sub(r"\s+", " ", subject)
    return subject.strip()


def html_to_text(html: str) -> str:
    """아주 가벼운 HTML → 텍스트 변환 (script/style 제거, 태그 제거, 줄바꿈 정리)"""
    if not html:
        return ""
    # script/style 제거
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    # <br>, </p> → 줄바꿈
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n", html)
    # 나머지 태그 제거
    text = re.sub(r"<[^>]+>", " ", html)
    # HTML 엔티티 처리
    text = unescape(text)
    # 공백 정리
    text = re.sub(r"\r", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+\n", "\n\n", text)
    return text.strip()


def extract_text_from_message(msg: email.message.Message) -> str:
    """
    멀티파트 메일에서 text/plain 우선, 없으면 text/html까지 파싱해서 텍스트 추출 강화 버전
    """
    parts: List[str] = []
    html_parts: List[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue

            try:
                charset = part.get_content_charset() or "utf-8"
            except Exception:
                charset = "utf-8"

            if ctype == "text/plain":
                try:
                    text = part.get_payload(decode=True).decode(charset, errors="ignore")
                    parts.append(text)
                except Exception:
                    continue
            elif ctype == "text/html":
                try:
                    html = part.get_payload(decode=True).decode(charset, errors="ignore")
                    html_parts.append(html)
                except Exception:
                    continue
    else:
        ctype = msg.get_content_type()
        try:
            charset = msg.get_content_charset() or "utf-8"
        except Exception:
            charset = "utf-8"

        if ctype == "text/plain":
            try:
                text = msg.get_payload(decode=True).decode(charset, errors="ignore")
                parts.append(text)
            except Exception:
                pass
        elif ctype == "text/html":
            try:
                html = msg.get_payload(decode=True).decode(charset, errors="ignore")
                html_parts.append(html)
            except Exception:
                pass

    if parts:
        return "\n".join(parts)

    if html_parts:
        # text/plain이 없으면 HTML을 텍스트로 변환해서 사용
        combined_html = "\n".join(html_parts)
        return html_to_text(combined_html)

    return ""


def make_short_summary(subject: str, body: str, max_len: int = 80) -> str:
    body = body.strip().replace("\r", "")
    first_line = body.split("\n")[0] if body else ""
    subject = re.sub(r"^(Re:|Fwd:|FWD:)\s*", "", subject, flags=re.I)
    base = subject.strip()
    if first_line and not base.startswith(first_line[:20]):
        summary = f"{base} / {first_line}"
    else:
        summary = base
    summary = summary.strip()
    if len(summary) > max_len:
        summary = summary[: max_len - 3] + "..."
    return summary


def looks_like_reply_needed(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(kw.lower() in lowered for kw in REPLY_KEYWORDS)


def has_due_context(text: str) -> bool:
    if not text:
        return False
    for kw in DUE_KEYWORDS:
        if kw in text:
            return True
    return False


def extract_due_date_candidate(text: str) -> Optional[date]:
    """텍스트에서 마감일 추출"""
    if not text:
        return None
    now = datetime.now()
    this_year = now.year

    def is_valid(y, m, d):
        try:
            date(y, m, d)
            return True
        except:
            return False

    # YYYY-MM-DD, YYYY/MM/DD, YYYY.MM.DD
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
        if is_valid(y, mo, d):
            return date(y, mo, d)

    # 11월 30일 (목)
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    if m:
        mo, d = map(int, m.groups())
        if is_valid(this_year, mo, d):
            due = date(this_year, mo, d)
            if due < now.date() and is_valid(this_year + 1, mo, d):
                due = date(this_year + 1, mo, d)
            return due

    # 11/30, 11-30, 11.30
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})(?:\s|$|까지|[)\]])", text)
    if m:
        mo, d = map(int, m.groups())
        if is_valid(this_year, mo, d):
            due = date(this_year, mo, d)
            if due < now.date() and is_valid(this_year + 1, mo, d):
                due = date(this_year + 1, mo, d)
            return due

    return None


def calculate_days_remaining(due_date: date) -> tuple[int, str]:
    """마감일까지 남은 날짜 계산"""
    if not due_date:
        return 0, ""
    
    today = date.today()
    delta = (due_date - today).days
    
    if delta < 0:
        return delta, f"⚠️ {abs(delta)}일 경과"
    elif delta == 0:
        return delta, "🔴 오늘 마감"
    elif delta == 1:
        return delta, "🟡 내일 마감"
    elif delta <= 3:
        return delta, f"🟠 {delta}일 남음"
    elif delta <= 7:
        return delta, f"🟢 {delta}일 남음"
    else:
        return delta, f"{delta}일 남음"


def classify_todo_by_subject(subject: str, classification_map: Dict[str, List[str]]) -> str:
    subject_lower = subject.lower()
    for category, keywords in classification_map.items():
        for keyword in keywords:
            if keyword and keyword.lower() in subject_lower:
                return category
    return ""


# =====================================================
# Settings Management
# =====================================================

def load_settings() -> Dict:
    """설정 파일 로드"""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    # 기본 설정
    return {
        "mail_server": "KSD 메일",
        "custom_host": "",
        "custom_port": 993,
        "custom_ssl": True,
        "days_lookback": 7
    }

def save_settings(settings: Dict):
    """설정 파일 저장"""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
    except Exception as e:
        raise Exception(f"설정 저장 실패: {e}")


def load_categories() -> List[str]:
    """사용자 카테고리 로드"""
    categories = DEFAULT_CATEGORIES.copy()
    if os.path.exists(USER_CATEGORIES_FILE):
        try:
            with open(USER_CATEGORIES_FILE, "r", encoding="utf-8") as f:
                user_cats = json.load(f)
                for cat in user_cats:
                    if cat not in categories:
                        categories.append(cat)
        except:
            pass
    return categories


def save_categories(categories: List[str]):
    """사용자 카테고리 저장"""
    user_cats = [cat for cat in categories if cat not in DEFAULT_CATEGORIES]
    try:
        with open(USER_CATEGORIES_FILE, "w", encoding="utf-8") as f:
            json.dump(user_cats, f, indent=2, ensure_ascii=False)
    except Exception as e:
        raise Exception(f"카테고리 저장 실패: {e}")


def load_training_data() -> List[Dict]:
    """학습 데이터 로드"""
    if os.path.exists(TRAINING_DATA_FILE):
        try:
            with open(TRAINING_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return []


def save_training_data(data: List[Dict]):
    """학습 데이터 저장"""
    try:
        with open(TRAINING_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        raise Exception(f"학습 데이터 저장 실패: {e}")


# =====================================================
# AI Mail Classifier
# =====================================================

class EmailClassifier:
    def __init__(self):
        # 한국어 임베딩 모델 사용 (sentence-transformers 사용 가능 시)
        self.use_embeddings = SENTENCE_TRANSFORMERS_AVAILABLE
        
        if self.use_embeddings:
            try:
                # 한국어 특화 경량 모델 사용
                print("한국어 임베딩 모델 로딩 중...")
                self.embedding_model = SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')
                self.classifier = SVC(kernel='rbf', probability=True, random_state=42)
                print("✓ 한국어 임베딩 모델 로딩 완료")
            except Exception as e:
                print(f"임베딩 모델 로드 실패, TF-IDF 사용: {e}")
                self.use_embeddings = False
        
        if not self.use_embeddings:
            # TF-IDF + RandomForest (Naive Bayes보다 성능 향상)
            self.vectorizer = TfidfVectorizer(max_features=500, ngram_range=(1, 2))
            self.classifier = RandomForestClassifier(n_estimators=100, random_state=42)
        
        self.is_trained = False
        self.categories = load_categories()
        self.training_data = load_training_data()
    
    def preprocess_text(self, text: str) -> str:
        """텍스트 전처리"""
        # 불필요한 문자 제거
        text = re.sub(r'[^\w\s가-힣]', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()
    
    def train(self):
        """모델 학습"""
        if len(self.training_data) < 3:
            return False
        
        texts = []
        labels = []
        
        for item in self.training_data:
            text = f"{item['subject']} {item['body']}"
            texts.append(self.preprocess_text(text))
            labels.append(item['category'])
        
        try:
            if self.use_embeddings:
                # 임베딩 벡터 생성
                X = self.embedding_model.encode(texts, show_progress_bar=False)
            else:
                # TF-IDF 벡터 생성
                X = self.vectorizer.fit_transform(texts)
            
            self.classifier.fit(X, labels)
            self.is_trained = True
            return True
        except Exception as e:
            print(f"학습 실패: {e}")
            return False
    
    def predict(self, subject: str, body: str, from_email: str = "") -> str:
        """메일 분류 예측 (AI + 규칙 하이브리드)"""
        # 1단계: 소셜미디어는 발신자 주소로 먼저 확인 (가장 확실)
        from_email_lower = from_email.lower()
        social_media_domains = [
            "linkedin", "facebook", "twitter", "instagram", "tiktok",
            "youtube", "snapchat", "reddit", "pinterest", 
            "mail.instagram.com", "facebookmail.com", "linkedin.com"
        ]
        if from_email_lower:
            for domain in social_media_domains:
                if domain in from_email_lower:
                    return "광고"
        
        # 2단계: AI 모델로 의미 기반 분류
        if not self.is_trained:
            # 학습되지 않았으면 키워드 기반 분류
            return self.keyword_based_classify(subject, body, from_email)
        
        try:
            text = f"{subject} {body}"
            text = self.preprocess_text(text)
            
            if self.use_embeddings:
                # 임베딩 벡터로 예측
                X = self.embedding_model.encode([text], show_progress_bar=False)
            else:
                # TF-IDF 벡터로 예측
                X = self.vectorizer.transform([text])
            
            prediction = self.classifier.predict(X)[0]
            
            # 예측 신뢰도 확인
            probabilities = self.classifier.predict_proba(X)[0]
            max_prob = max(probabilities)
            
            # 3단계: 신뢰도가 낮으면 키워드 기반으로 보완
            if max_prob < 0.5:  # 신뢰도 임계값 0.4 -> 0.5로 상향
                keyword_result = self.keyword_based_classify(subject, body, from_email)
                if keyword_result:
                    return keyword_result
            
            return prediction
        except Exception as e:
            print(f"예측 실패: {e}")
            return self.keyword_based_classify(subject, body, from_email)
    
    def keyword_based_classify(self, subject: str, body: str, from_email: str = "") -> str:
        """키워드 기반 분류 (fallback)"""
        text = f"{subject} {body}".lower()
        from_email_lower = from_email.lower()
        
        # 소셜 미디어 알림 패턴 (광고로 분류) - 우선 체크
        social_media_domains = [
            "linkedin", "facebook", "twitter", "instagram", "tiktok",
            "youtube", "snapchat", "reddit", "pinterest", "카카오", "네이버밴드",
            "mail.instagram.com", "facebookmail.com", "linkedin.com"
        ]
        
        social_notification_patterns = [
            "업데이트 공유", "님이 최근 올렸음", "님이 올렸음", "반응", "댓글", "좋아요",
            "팔로우", "추천", "회원님을 위한", "추천 피드", "새로운 게시물", "님이 공유",
            "shared an update", "posted", "likes", "comments", "followers",
            "new connection", "직원이", "인맥들의", "스토리", "story", "stories",
            "확인해보세요", "새로운 알림", "new notification"
        ]
        
        # from_email 필드 우선 체크 (가장 확실함)
        if from_email_lower:
            for domain in social_media_domains:
                if domain in from_email_lower:
                    return "광고"
        
        # 소셜 미디어 도메인 체크 (본문에서)
        is_social_media = any(domain in text for domain in social_media_domains)
        has_social_notification = any(pattern in text for pattern in social_notification_patterns)
        
        # 소셜 미디어에서 온 알림은 무조건 광고로 분류 (noreply여도 광고)
        if is_social_media:
            if has_social_notification or "noreply" in text or "no-reply" in text:
                return "광고"
        
        # 자동 메일 패턴 체크 (안내로 분류)
        auto_mail_patterns = [
            "발신전용", "noreply", "no-reply", "no_reply", "account_noreply",
            "자동발송", "자동전송", "do not reply", "본 메일은 발신전용",
            "updates-noreply", "notification", "이메일 받지 않기", "구독 취소"
        ]
        if any(pattern in text for pattern in auto_mail_patterns):
            return "안내"
        
        # 시스템 알림/인증 메일 패턴 (안내로 분류)
        system_patterns = [
            "비밀번호 생성", "인증", "로그인", "계정", "보안",
            "생성되었습니다", "변경되었습니다", "등록되었습니다",
            "password", "authentication", "verification"
        ]
        system_score = sum(1 for pattern in system_patterns if pattern in text)
        
        # 제출 키워드 (기한이 있고 답장 필요)
        submit_keywords = [
            "제출", "회신", "답장", "응답", "reply", "보내주세요", "제출해주",
            "요청드립니다", "부탁드립니다", "회신해주", "보내주시기"
        ]
        
        # 광고 키워드 (마케팅, 프로모션)
        ad_keywords = [
            "광고", "프로모션", "할인", "이벤트", "특가", "세일", "쿠폰",
            "promotion", "discount", "sale", "offer", "deal", "뉴스레터",
            "마케팅", "unsubscribe", "이메일 받지 않기", "구독 취소",
            "업데이트 공유", "추천", "더보기", "linkedin", "updates-noreply",
            "반응", "댓글", "좋아요", "공감", "newsletter",
            "지금 뜨는", "바로 확인", "놓치지 마세요", "채용", "포지션",
            "경력직", "신입", "채용정보", "구인", "지원하세요", "합격",
            "취업", "이력서", "Job", "Career", "Hiring"
        ]
        
        # 안내 키워드 (정보 공유)
        guide_keywords = [
            "안내", "알림", "공지", "공유", "참고", "information", "notice",
            "알려드립니다", "안내드립니다", "공지사항", "소식",
            "발송되었습니다", "생성 내역", "활동 내역"
        ]
        
        # 검토 키워드 (확인 필요, 기한 없음)
        review_keywords = [
            "검토", "확인", "review", "점검", "살펴", "검토해", "확인해",
            "의견", "피드백", "논의"
        ]
        
        # 점수 계산
        submit_score = sum(1 for kw in submit_keywords if kw in text)
        ad_score = sum(1 for kw in ad_keywords if kw in text)
        guide_score = sum(1 for kw in guide_keywords if kw in text)
        review_score = sum(1 for kw in review_keywords if kw in text)
        
        # 광고성 메일 패턴 체크
        ad_patterns = [
            "unsubscribe", "이메일 받지 않기", "구독 취소", "수신거부",
            "updates-noreply", "newsletter", "marketing"
        ]
        if any(pattern in text for pattern in ad_patterns):
            ad_score += 3
        
        # 소셜 미디어 알림 체크
        social_media_list = ["linkedin", "facebook", "instagram", "twitter", "youtube", "tiktok", "reddit"]
        social_notification_words = ["업데이트", "공유", "반응", "댓글", "좋아요", "팔로우", "추천", "포스트", "님이"]
        
        is_from_social = any(social in text for social in social_media_list)
        has_notification = any(word in text for word in social_notification_words)
        
        if is_from_social and has_notification:
            ad_score += 5
        
        # 시스템 메일이면 안내 점수 증가
        if system_score >= 2:
            guide_score += 3
        
        # 제출 요청 문구가 명확히 있는지 체크
        submit_request_patterns = [
            r'제출해\s*주', r'회신해\s*주', r'보내\s*주', r'답장\s*부탁',
            r'요청드립니다', r'제출\s*요청'
        ]
        has_submit_request = any(re.search(pattern, text) for pattern in submit_request_patterns)
        
        # 날짜/기한 표현이 있고 제출 요청이 명확한 경우만 제출 점수 증가
        has_deadline = re.search(r'\d{1,2}[월/.-]\d{1,2}[일]?\s*(까지|by|before)|까지\s*제출|마감|기한|deadline', text)
        if has_deadline and has_submit_request:
            submit_score += 3
        elif has_deadline:
            # 기한만 있고 제출 요청이 없으면 검토로
            review_score += 1
        
        scores = {
            "제출": submit_score,
            "광고": ad_score,
            "안내": guide_score,
            "검토": review_score
        }
        
        max_category = max(scores, key=scores.get)
        
        # 모든 점수가 0이면 안내로 분류
        if scores[max_category] == 0:
            return "안내"
        
        return max_category
    
    def add_training_data(self, subject: str, body: str, category: str):
        """학습 데이터 추가"""
        self.training_data.append({
            "subject": subject,
            "body": body[:500],  # 본문은 500자까지만 저장
            "category": category,
            "timestamp": datetime.now().isoformat()
        })
        
        # 최근 1000개만 유지
        if len(self.training_data) > 1000:
            self.training_data = self.training_data[-1000:]
        
        save_training_data(self.training_data)
        
        # 재학습
        self.train()


# =====================================================
# IMAP Fetch
# =====================================================

def detect_inbox_mailbox(imap: imaplib.IMAP4) -> str:
    """
    서버에서 LIST 결과를 보고 Inbox/INBOX/받은편지함 등 메일함 이름 자동 탐지
    """
    try:
        status, mailboxes = imap.list()
        if status != "OK":
            return "INBOX"

        names: List[str] = []
        for m in mailboxes:
            line = m.decode(errors="ignore")
            # 맨 마지막 "..." 부분이 메일함 이름
            match = re.search(r'"([^"]+)"\s*$', line)
            if match:
                names.append(match.group(1))

        # 1순위: INBOX (대소문자 무시)
        for name in names:
            if name.upper() == "INBOX":
                return name

        # 2순위: inbox 포함
        for name in names:
            if "inbox" in name.lower():
                return name

        # 3순위: 받은편지함 비슷한 이름
        for name in names:
            if "받은" in name or "편지함" in name:
                return name

        # 그래도 없으면 첫 번째
        if names:
            return names[0]

    except Exception as e:
        print(f"[WARN] 메일함 자동 탐지 실패, 기본 INBOX 사용: {e}")
    return "INBOX"


def fetch_emails(username: str, password: str, host: str, port: int, use_ssl: bool, days: int = 7) -> List[Dict]:
    imap = None
    try:
        if use_ssl:
            imap = imaplib.IMAP4_SSL(host, port)
        else:
            imap = imaplib.IMAP4(host, port)

        # 로그인
        imap.login(username, password)
        print("[DEBUG] IMAP 로그인 성공")

        # 메일함 자동탐지
        inbox_name = detect_inbox_mailbox(imap)
        print(f"[DEBUG] 선택된 메일함: {inbox_name}")

        status, _ = imap.select(inbox_name)
        if status != "OK":
            # 혹시나해서 INBOX로 재시도
            print(f"[WARN] {inbox_name} 선택 실패, INBOX로 재시도")
            status, _ = imap.select("INBOX")
            if status != "OK":
                raise Exception(f"메일함 선택 실패: {inbox_name}")

        since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        print(f"[DEBUG] 검색 기준 날짜(SINCE): {since_date}")

        # 검색 방식 수정: (SINCE {date}) 문자열이 아니라 'SINCE', date 로 나눠서 넘김
        status, data = imap.search(None, "SINCE", since_date)

        if status != "OK":
            print(f"[WARN] SINCE 검색 실패, ALL로 재시도. status={status}, data={data}")
            status, data = imap.search(None, "ALL")

        # 결과가 비어 있으면 ALL로 다시 시도 후, 파이썬에서 날짜 필터링
        use_python_date_filter = False
        if status == "OK" and data and data[0]:
            msg_nums = data[0].split()
            if not msg_nums:
                print("[INFO] SINCE 결과가 0개, ALL로 재시도")
                status, data = imap.search(None, "ALL")
                use_python_date_filter = True
        else:
            print("[INFO] 검색 결과 없음 또는 오류, ALL로 재시도")
            status, data = imap.search(None, "ALL")
            use_python_date_filter = True

        if status != "OK":
            raise Exception(f"IMAP 검색 실패: {status}, data={data}")

        emails: List[Dict] = []
        all_nums = data[0].split()
        print(f"[DEBUG] 총 검색된 메일 수: {len(all_nums)}")

        # 뒤에서부터(최신 메일 우선)
        for num in reversed(all_nums):
            msg_id_num = num.decode()
            status, msg_data = imap.fetch(num, "(RFC822)")
            if status != "OK":
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            subject = decode_mime_words(msg.get("Subject", ""))
            from_ = decode_mime_words(msg.get("From", ""))
            date_str = decode_mime_words(msg.get("Date", ""))
            body = extract_text_from_message(msg)

            # Python에서 날짜 필터링이 필요한 경우
            if use_python_date_filter and days > 0:
                try:
                    msg_date_tuple = email.utils.parsedate_tz(date_str)
                    if msg_date_tuple:
                        msg_ts = email.utils.mktime_tz(msg_date_tuple)
                        msg_dt = datetime.fromtimestamp(msg_ts)
                        if msg_dt < datetime.now() - timedelta(days=days):
                            # 너무 오래된 메일이면 스킵
                            continue
                except Exception:
                    pass

            normalized_subject = normalize_subject(subject)
            full_text = f"{(normalized_subject + ' ') * 3}{body}"

            emails.append({
                "msg_id": msg_id_num,
                "subject": subject,
                "subject_norm": normalized_subject,
                "from": from_,
                "date_header": date_str,
                "body": body,
                "full_text": full_text,
            })

        print(f"[DEBUG] 최종 수집된 메일 수: {len(emails)}")
        return emails

    except imaplib.IMAP4.error as e:
        raise imaplib.IMAP4.error(f"IMAP 로그인/처리 실패: {e}")
    except Exception as e:
        raise Exception(f"IMAP 서버 처리 중 오류: {e}")
    finally:
        if imap is not None:
            try:
                imap.close()
            except Exception:
                pass
            try:
                imap.logout()
            except Exception:
                pass


# =====================================================
# Settings Dialog
# =====================================================

class SettingsDialog:
    def __init__(self, parent, current_settings):
        self.result = None
        self.current_settings = current_settings
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("환경설정")
        self.dialog.geometry("550x600")
        self.dialog.resizable(False, False)
        self.dialog.transient(parent)
        self.dialog.grab_set()

        # Variables
        self.mail_server_var = tk.StringVar(value=current_settings.get("mail_server", "KSD 메일"))
        self.custom_host_var = tk.StringVar(value=current_settings.get("custom_host", ""))
        self.custom_port_var = tk.IntVar(value=current_settings.get("custom_port", 993))
        self.custom_ssl_var = tk.BooleanVar(value=current_settings.get("custom_ssl", True))
        self.days_var = tk.IntVar(value=current_settings.get("days_lookback", 7))

        self.noti_interval_var = tk.IntVar(value=current_settings.get("noti_interval", 30))
        self.create_widgets()

        # Center dialog
        self.dialog.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.dialog.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.dialog.winfo_height()) // 2
        self.dialog.geometry(f"+{x}+{y}")
    
    def create_widgets(self):
        # Main frame
        main_frame = ttk.Frame(self.dialog, padding=20, borderwidth=0, relief="flat")
        main_frame.pack(fill="both", expand=True)
        
        # Mail Server Selection
        server_frame = ttk.LabelFrame(main_frame, text="메일 서버 선택", padding=10, borderwidth=0, relief="flat")
        server_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Label(server_frame, text="메일 서버:").grid(row=0, column=0, sticky="w", pady=5)
        server_combo = ttk.Combobox(server_frame, textvariable=self.mail_server_var, 
                                     values=list(MAIL_PRESETS.keys()), state="readonly", width=30)
        server_combo.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=5)
        server_combo.bind("<<ComboboxSelected>>", self.on_server_change)
        
        server_frame.columnconfigure(1, weight=1)
        
        # Server Info
        info_frame = ttk.LabelFrame(main_frame, text="서버 정보", padding=10, borderwidth=0, relief="flat")
        info_frame.pack(fill="x", pady=(0, 10))
        
        self.host_label = ttk.Label(info_frame, text="")
        self.host_label.pack(anchor="w")
        self.port_label = ttk.Label(info_frame, text="")
        self.port_label.pack(anchor="w")
        self.ssl_label = ttk.Label(info_frame, text="")
        self.ssl_label.pack(anchor="w")
        
        self.update_server_info()
        
        # Custom Server Settings
        self.custom_frame = ttk.LabelFrame(main_frame, text="사용자 정의 서버", padding=10, borderwidth=0, relief="flat")
        self.custom_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Label(self.custom_frame, text="호스트:").grid(row=0, column=0, sticky="w", pady=5)
        self.custom_host_entry = ttk.Entry(self.custom_frame, textvariable=self.custom_host_var, width=30)
        self.custom_host_entry.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=5)
        
        ttk.Label(self.custom_frame, text="포트:").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(self.custom_frame, textvariable=self.custom_port_var, width=10).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=5)
        
        ttk.Checkbutton(self.custom_frame, text="SSL 사용", variable=self.custom_ssl_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=5)
        
        self.custom_frame.columnconfigure(1, weight=1)
        self.toggle_custom_settings()
        
        # General Settings
        general_frame = ttk.LabelFrame(main_frame, text="일반 설정", padding=10, borderwidth=0, relief="flat")
        general_frame.pack(fill="x", pady=(0, 10))

        # Username
        ttk.Label(general_frame, text="아이디:").grid(row=0, column=0, sticky="w", pady=5)
        self.username_var = tk.StringVar(value=self.current_settings.get("username", ""))
        ttk.Entry(general_frame, textvariable=self.username_var, width=30).grid(row=0, column=1, sticky="w", padx=(10, 0), pady=5)

        # Password
        ttk.Label(general_frame, text="비밀번호:").grid(row=1, column=0, sticky="w", pady=5)
        self.password_var = tk.StringVar(value=self.current_settings.get("password", ""))
        ttk.Entry(general_frame, textvariable=self.password_var, show="*", width=30).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=5)

        # Email lookup days
        ttk.Label(general_frame, text="이메일 조회 기간:").grid(row=2, column=0, sticky="w", pady=5)
        days_frame = ttk.Frame(general_frame)
        days_frame.grid(row=2, column=1, sticky="w", padx=(10, 0))
        ttk.Entry(days_frame, textvariable=self.days_var, width=10).pack(side="left")
        ttk.Label(days_frame, text="일").pack(side="left", padx=(5, 0))

        # Notification interval
        ttk.Label(general_frame, text="알림 간격(분):").grid(row=3, column=0, sticky="w", pady=5)
        noti_frame = ttk.Frame(general_frame)
        noti_frame.grid(row=3, column=1, sticky="w", padx=(10, 0))
        ttk.Entry(noti_frame, textvariable=self.noti_interval_var, width=10).pack(side="left")
        ttk.Label(noti_frame, text="분").pack(side="left", padx=(5, 0))
        
        # Separator above buttons
        ttk.Separator(main_frame, orient="horizontal").pack(fill="x", pady=10)

        # Buttons (always at bottom)
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x", side="bottom", pady=(10, 10))

        save_btn = ttk.Button(button_frame, text="저장", command=self.save, width=12)
        save_btn.pack(side="right", padx=(5, 0))

        cancel_btn = ttk.Button(button_frame, text="취소", command=self.cancel, width=12)
        cancel_btn.pack(side="right")
    
    def on_server_change(self, event=None):
        self.update_server_info()
        self.toggle_custom_settings()
    
    def update_server_info(self):
        server_name = self.mail_server_var.get()
        if server_name in MAIL_PRESETS:
            preset = MAIL_PRESETS[server_name]
            self.host_label.config(text=f"호스트: {preset['host']}")
            self.port_label.config(text=f"포트: {preset['port']}")
            self.ssl_label.config(text=f"SSL: {'사용' if preset['use_ssl'] else '미사용'}")
    
    def toggle_custom_settings(self):
        if self.mail_server_var.get() == "사용자 정의":
            for child in self.custom_frame.winfo_children():
                if isinstance(child, (ttk.Entry, ttk.Checkbutton)):
                    child.config(state="normal")
        else:
            for child in self.custom_frame.winfo_children():
                if isinstance(child, (ttk.Entry, ttk.Checkbutton)):
                    child.config(state="disabled")
    
    def save(self):
        self.result = {
            "mail_server": self.mail_server_var.get(),
            "custom_host": self.custom_host_var.get(),
            "custom_port": self.custom_port_var.get(),
            "custom_ssl": self.custom_ssl_var.get(),
            "days_lookback": self.days_var.get(),
            "noti_interval": self.noti_interval_var.get(),
            "username": self.username_var.get(),
            "password": self.password_var.get()
        }
        self.dialog.destroy()
    
    def cancel(self):
        self.dialog.destroy()


# =====================================================
# GUI Application
# =====================================================

class TodoApp:
    def _clear_placeholder(self, entry, placeholder):
        if entry.get() == placeholder:
            entry.delete(0, tk.END)
            entry.config(foreground="black", show="")

    def _add_placeholder(self, entry, placeholder):
        if not entry.get():
            entry.insert(0, placeholder)
            entry.config(foreground="gray", show="")

    def _clear_placeholder_pw(self, entry, placeholder):
        if entry.get() == placeholder:
            entry.delete(0, tk.END)
            entry.config(foreground="black", show="*")

    def _add_placeholder_pw(self, entry, placeholder):
        if not entry.get():
            entry.insert(0, placeholder)
            entry.config(foreground="gray", show="")

    def __init__(self, root):
        self.root = root
        self.root.title("이메일 To-Do 관리")
        self.root.geometry("1100x700")
        
        # Load settings
        self.settings = load_settings()
        
        # Variables
        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.emails_data = []
        
        # AI Classifier
        self.classifier = EmailClassifier()
        self.classifier.train()
        
        # Categories
        self.categories = load_categories()
        
        self.create_widgets()
        self.update_status_with_settings()
        # 알림 타이머 시작
        self.noti_popup_window = None
        self.start_todo_notification_timer()

    def start_todo_notification_timer(self):
        interval_min = self.settings.get("noti_interval", 30)
        interval_ms = max(1, int(interval_min)) * 60 * 1000
        self.root.after(interval_ms, self.show_todo_notification)

    def show_todo_notification(self):
        # 미완료 할일 추출
        todos = [e for e in self.emails_data if e.get("category") in ["제출", "검토"] and not e.get("is_completed", False)]
        if todos:
            msg = "\n".join([
                f"[{e.get('category','')}] {e.get('subject','')[:30]}" + (f" (마감: {e.get('due_date').strftime('%m/%d')})" if e.get('due_date') else "")
                for e in todos[:5]
            ])
            self._show_popup(msg, len(todos))
        # 다음 알림 예약
        self.start_todo_notification_timer()

    def _show_popup(self, msg, count):
        # 기존 팝업 닫기
        if self.noti_popup_window and self.noti_popup_window.winfo_exists():
            self.noti_popup_window.destroy()
        self.noti_popup_window = tk.Toplevel(self.root)
        self.noti_popup_window.title("미처리 할일 알림")
        self.noti_popup_window.attributes("-topmost", True)
        self.noti_popup_window.resizable(False, False)
        self.noti_popup_window.geometry("300x180")
        # 화면 우측 하단 배치
        self.noti_popup_window.update_idletasks()
        sw = self.noti_popup_window.winfo_screenwidth()
        sh = self.noti_popup_window.winfo_screenheight()
        w, h = 300, 180
        x = sw - w - 20
        y = sh - h - 60
        self.noti_popup_window.geometry(f"{w}x{h}+{x}+{y}")
        frame = ttk.Frame(self.noti_popup_window, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"미처리 할일 {count}건", font=("", 12, "bold"), foreground="red").pack(pady=(0, 10))
        text = tk.Text(frame, height=6, wrap="word", font=("", 10))
        text.insert("1.0", msg)
        text.config(state="disabled", bg="#fff8e1", relief="flat")
        text.pack(fill="both", expand=True)
        ttk.Button(frame, text="확인", command=self.noti_popup_window.destroy).pack(pady=(10, 0))
    
    def create_widgets(self):
        # Menu bar
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="설정", menu=settings_menu)
        settings_menu.add_command(label="환경설정", command=self.open_settings)
        settings_menu.add_separator()
        settings_menu.add_command(label="종료", command=self.root.quit)

        # Modernized 로그인 카드형 중앙 배치
        self.login_card = ttk.LabelFrame(self.root, text="IMAP 로그인", padding=10, borderwidth=0, relief="flat")
        self.login_card.pack(fill="x", padx=10, pady=5)

        self.username_entry = ttk.Entry(self.login_card, textvariable=self.username_var, width=28, foreground="gray")
        self.username_entry.grid(row=0, column=0, padx=(10, 5), pady=(5, 5), sticky='w')
        self.username_entry.insert(0, '사용자명')
        self.username_entry.bind('<FocusIn>', lambda e: self._clear_placeholder(self.username_entry, '사용자명'))
        self.username_entry.bind('<FocusOut>', lambda e: self._add_placeholder(self.username_entry, '사용자명'))

        self.password_entry = ttk.Entry(self.login_card, textvariable=self.password_var, show='', width=28, foreground="gray")
        self.password_entry.grid(row=0, column=1, padx=(5, 5), pady=(5, 5), sticky='w')
        self.password_entry.insert(0, '비밀번호')
        self.password_entry.bind('<FocusIn>', lambda e: self._clear_placeholder_pw(self.password_entry, '비밀번호'))
        self.password_entry.bind('<FocusOut>', lambda e: self._add_placeholder_pw(self.password_entry, '비밀번호'))

        ttk.Button(self.login_card, text="⚙ 환경설정", command=self.open_settings, width=14).grid(row=0, column=2, padx=(5, 5), pady=5, sticky='e')
        ttk.Button(self.login_card, text="이메일 불러오기", command=self.fetch_emails_handler, width=18).grid(row=0, column=3, padx=(5, 10), pady=5, sticky='e')
        self.login_card.columnconfigure(1, weight=1)

        # 앱 시작 시 자동 이메일 불러오기 (설정에 아이디/비밀번호가 있으면)
        if self.settings.get("username") and self.settings.get("password"):
            self.username_var.set(self.settings.get("username"))
            self.password_var.set(self.settings.get("password"))
            self._add_placeholder(self.username_entry, '사용자명')
            self._add_placeholder_pw(self.password_entry, '비밀번호')
            self.root.after(500, self.fetch_emails_handler)

        # Main content area with notebook (tabs)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=5)

        # Tab 1: To-Do List
        todo_tab = ttk.Frame(self.notebook, borderwidth=0, relief="flat")
        self.notebook.add(todo_tab, text="📋 할일 목록")

        # Tab 2: All Emails
        email_tab = ttk.Frame(self.notebook, borderwidth=0, relief="flat")
        self.notebook.add(email_tab, text="📧 전체 메일")

        # ===== To-Do List Tab =====
        todo_container = ttk.Frame(todo_tab, borderwidth=0, relief="flat")
        todo_container.pack(fill="both", expand=True, padx=5, pady=5)

        # To-Do Tree
        todo_list_frame = ttk.LabelFrame(todo_container, text="할일 목록 (제출/검토)", padding=10, borderwidth=0, relief="flat")
        todo_list_frame.pack(side="left", fill="both", expand=True)

        todo_columns = ("상태", "분류", "마감일", "제목", "발신자")
        self.todo_tree = ttk.Treeview(todo_list_frame, columns=todo_columns, show="tree headings", height=20)

        self.todo_tree.heading("#0", text="번호")
        self.todo_tree.column("#0", width=50)

        for col in todo_columns:
            self.todo_tree.heading(col, text=col)

        self.todo_tree.column("상태", width=60)
        self.todo_tree.column("분류", width=60)
        self.todo_tree.column("마감일", width=150)
        self.todo_tree.column("제목", width=350)
        self.todo_tree.column("발신자", width=150)

        todo_scrollbar = ttk.Scrollbar(todo_list_frame, orient="vertical", command=self.todo_tree.yview)
        self.todo_tree.configure(yscrollcommand=todo_scrollbar.set)

        self.todo_tree.pack(side="left", fill="both", expand=True)
        todo_scrollbar.pack(side="right", fill="y")

        # To-Do Actions
        todo_action_frame = ttk.LabelFrame(todo_container, text="할일 관리", padding=10, borderwidth=0, relief="flat")
        todo_action_frame.pack(side="right", fill="y", padx=(10, 0))

        ttk.Button(todo_action_frame, text="✓ 완료 처리", command=self.mark_todo_complete, width=15).pack(pady=5)
        ttk.Button(todo_action_frame, text="↻ 미완료로 변경", command=self.mark_todo_incomplete, width=15).pack(pady=5)
        ttk.Button(todo_action_frame, text="상세 보기", command=self.view_todo_detail, width=15).pack(pady=5)

        # 구분선은 borderwidth=0, relief='flat'으로 대체
        ttk.Separator(todo_action_frame, orient="horizontal").pack(fill="x", pady=10)

        ttk.Label(todo_action_frame, text="할일 통계:", font=("", 9, "bold")).pack(anchor="w", pady=(0, 5))
        self.todo_stats_label = ttk.Label(todo_action_frame, text="", font=("", 8), foreground="gray")
        self.todo_stats_label.pack(anchor="w", fill="x")

        self.todo_tree.bind("<<TreeviewSelect>>", self.on_todo_select)
        self.todo_tree.bind("<Double-1>", lambda e: self.view_todo_detail())

        # ===== All Emails Tab =====
        email_container = ttk.Frame(email_tab, borderwidth=0, relief="flat")
        email_container.pack(fill="both", expand=True, padx=5, pady=5)

        # Email List Frame
        list_frame = ttk.LabelFrame(email_container, text="이메일 목록", padding=10, borderwidth=0, relief="flat")
        list_frame.pack(side="left", fill="both", expand=True)

        # Treeview
        columns = ("분류", "마감일", "제목", "발신자", "날짜")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="tree headings", height=15)
        
        self.tree.heading("#0", text="번호")
        self.tree.column("#0", width=50)
        
        for col in columns:
            self.tree.heading(col, text=col)
        
        self.tree.column("분류", width=70)
        self.tree.column("마감일", width=150)
        self.tree.column("제목", width=380)
        self.tree.column("발신자", width=160)
        self.tree.column("날짜", width=150)
        
        # Scrollbar
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Detail and Category Frame
        detail_category_frame = ttk.Frame(email_container)
        detail_category_frame.pack(side="right", fill="both", expand=False, padx=(10, 0))
        
        # Category Frame
        category_frame = ttk.LabelFrame(detail_category_frame, text="분류 / 마감일 관리", padding=10, borderwidth=0, relief="flat")
        category_frame.pack(fill="x")
        
        ttk.Label(category_frame, text="현재 분류:").pack(anchor="w", pady=(0, 5))
        self.current_category_label = ttk.Label(category_frame, text="-", font=("", 10, "bold"))
        self.current_category_label.pack(anchor="w", pady=(0, 5))
        
        ttk.Label(category_frame, text="마감일:").pack(anchor="w", pady=(0, 5))
        self.due_date_label = ttk.Label(category_frame, text="-", font=("", 9), foreground="red")
        self.due_date_label.pack(anchor="w", pady=(0, 10))
        
        ttk.Separator(category_frame, orient="horizontal").pack(fill="x", pady=5)
        
        ttk.Label(category_frame, text="분류 변경:").pack(anchor="w", pady=(0, 5))
        self.category_var = tk.StringVar()
        self.category_combo = ttk.Combobox(category_frame, textvariable=self.category_var, 
                                            values=self.categories, state="readonly", width=15)
        self.category_combo.pack(pady=(0, 5))
        
        ttk.Button(category_frame, text="분류 적용", command=self.apply_category, width=15).pack(pady=(0, 5))
        
        ttk.Label(category_frame, text="마감일 설정:").pack(anchor="w", pady=(10, 5))
        due_date_entry_frame = ttk.Frame(category_frame)
        due_date_entry_frame.pack(fill="x", pady=(0, 5))
        
        self.due_date_entry = ttk.Entry(due_date_entry_frame, width=10)
        self.due_date_entry.pack(side="left")
        ttk.Label(due_date_entry_frame, text="MM/DD", font=("", 8)).pack(side="left", padx=(5, 0))
        
        ttk.Button(category_frame, text="마감일 적용", command=self.apply_due_date, width=15).pack(pady=(0, 10))
        
        ttk.Separator(category_frame, orient="horizontal").pack(fill="x", pady=10)
        
        ttk.Label(category_frame, text="카테고리 관리:").pack(anchor="w", pady=(0, 5))
        ttk.Button(category_frame, text="새 카테고리 추가", command=self.add_category, width=15).pack(pady=(0, 5))
        
        ttk.Separator(category_frame, orient="horizontal").pack(fill="x", pady=10)
        
        ttk.Label(category_frame, text="AI 학습 상태:", font=("", 8)).pack(anchor="w")
        self.training_status_label = ttk.Label(category_frame, text=f"{len(self.classifier.training_data)}개 학습됨", 
                                                font=("", 8), foreground="gray")
        self.training_status_label.pack(anchor="w", pady=(0, 5))
        
        # 메일 내용 미리보기 영역
        preview_frame = ttk.LabelFrame(detail_category_frame, text="메일 내용 미리보기", padding=5, borderwidth=0, relief="flat")
        preview_frame.pack(fill="both", expand=True, pady=(10, 0))
        
        self.detail_text = tk.Text(preview_frame, wrap="word", height=20)
        detail_scroll = ttk.Scrollbar(preview_frame, orient="vertical", command=self.detail_text.yview)
        self.detail_text.configure(yscrollcommand=detail_scroll.set)
        self.detail_text.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        
        # Bind selection event
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        
        # Status bar
        self.status_label = ttk.Label(self.root, text="준비", relief="sunken")
        self.status_label.pack(fill="x", side="bottom", padx=10, pady=5)
    
    def update_status_with_settings(self):
        server_name = self.settings.get("mail_server", "KSD 메일")
        days = self.settings.get("days_lookback", 7)
        self.status_label.config(text=f"현재 설정: {server_name} | {days}일 조회")
    
    def open_settings(self):
        dialog = SettingsDialog(self.root, self.settings)
        self.root.wait_window(dialog.dialog)

        if dialog.result:
            self.settings = dialog.result
            save_settings(self.settings)
            self.update_status_with_settings()
            # 환경설정에서 아이디/비밀번호를 입력하면 로그인 입력란에도 반영
            username = self.settings.get("username", "")
            password = self.settings.get("password", "")
            self.username_var.set(username)
            self.password_var.set(password)
            self._add_placeholder(self.username_entry, '사용자명')
            self._add_placeholder_pw(self.password_entry, '비밀번호')
            # 알림 타이머 재시작
            self.start_todo_notification_timer()
            messagebox.showinfo("설정 저장", "환경설정이 저장되었습니다.")
    
    def get_mail_config(self):
        """현재 설정에서 메일 서버 정보 가져오기"""
        server_name = self.settings.get("mail_server", "KSD 메일")
        
        if server_name == "사용자 정의":
            return {
                "host": self.settings.get("custom_host", ""),
                "port": self.settings.get("custom_port", 993),
                "use_ssl": self.settings.get("custom_ssl", True)
            }
        elif server_name in MAIL_PRESETS:
            return MAIL_PRESETS[server_name]
        else:
            # 기본값
            return MAIL_PRESETS["KSD 메일"]
    
    def fetch_emails_handler(self):
        username = self.username_var.get().strip()
        password = self.password_var.get().strip()
        
        if not username or not password:
            messagebox.showwarning("입력 오류", "사용자명과 비밀번호를 입력하세요.")
            return
        
        # Get mail configuration
        mail_config = self.get_mail_config()
        
        if not mail_config["host"]:
            messagebox.showerror("설정 오류", "메일 서버 호스트가 설정되지 않았습니다.\n환경설정에서 서버를 설정하세요.")
            return
        
        server_name = self.settings.get("mail_server", "KSD 메일")
        days = self.settings.get("days_lookback", 7)
        
        self.status_label.config(text=f"이메일 가져오는 중... ({server_name})")
        self.root.update()
        
        try:
            self.emails_data = fetch_emails(
                username, 
                password, 
                mail_config["host"], 
                mail_config["port"], 
                mail_config["use_ssl"],
                days
            )
            self.populate_tree()
            self.populate_todo_tree()
            self.status_label.config(text=f"{server_name}에서 {len(self.emails_data)}개의 이메일을 가져왔습니다.")
            messagebox.showinfo("성공", f"{len(self.emails_data)}개의 이메일을 가져왔습니다.")
        except Exception as e:
            self.status_label.config(text="오류 발생")
            messagebox.showerror("오류", f"이메일 가져오기 실패:\n{str(e)}\n\n팁: 사내 메일은 IMAP 포트/보안장비 정책도 확인 필요합니다.")
    
    def populate_todo_tree(self):
        """할일 목록 트리 업데이트"""
        # Clear existing items
        for item in self.todo_tree.get_children():
            self.todo_tree.delete(item)
        
        todo_count = 0
        completed_count = 0
        overdue_count = 0
        
        # 제출/검토 메일만 필터링
        for idx, email_data in enumerate(self.emails_data, 1):
            category = email_data.get("category", "")
            
            # 제출 또는 검토 메일만 표시
            if category not in ["제출", "검토"]:
                continue
            
            subject = email_data.get("subject", "제목 없음")
            from_ = email_data.get("from", "발신자 없음")
            due_date = email_data.get("due_date")
            is_completed = email_data.get("is_completed", False)
            
            # 상태 표시
            if is_completed:
                status = "✓ 완료"
                completed_count += 1
            else:
                status = "☐ 대기"
                todo_count += 1
            
            # 마감일 표시
            if due_date:
                days_remaining, remaining_str = calculate_days_remaining(due_date)
                due_date_str = f"{due_date.strftime('%m/%d')} {remaining_str}"
                
                # 미완료이고 마감 지난 경우
                if not is_completed and days_remaining < 0:
                    overdue_count += 1
            else:
                due_date_str = "-"
            
            # 트리에 추가
            item_id = self.todo_tree.insert("", "end", text=str(idx), 
                                            values=(status, category, due_date_str, subject, from_))
            
            # 완료된 항목은 회색으로
            if is_completed:
                self.todo_tree.item(item_id, tags=("completed",))
            # 마감 경과는 빨간색으로
            elif due_date and days_remaining < 0:
                self.todo_tree.item(item_id, tags=("overdue",))
        
        # 태그 색상 설정
        self.todo_tree.tag_configure("completed", foreground="gray")
        self.todo_tree.tag_configure("overdue", foreground="red")
        
        # 통계 업데이트
        stats_text = f"미완료: {todo_count}개\n완료: {completed_count}개\n기한경과: {overdue_count}개"
        self.todo_stats_label.config(text=stats_text)
    
    def populate_tree(self):
        # Clear existing items
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        # Add emails to tree with AI classification
        for idx, email_data in enumerate(self.emails_data, 1):
            subject = email_data.get("subject", "제목 없음")
            from_ = email_data.get("from", "발신자 없음")
            date_header = email_data.get("date_header", "날짜 없음")
            body = email_data.get("body", "")
            
            # AI 분류 (이미 분류되어 있지 않으면)
            if "category" not in email_data:
                category = self.classifier.predict(subject, body, from_)
                email_data["category"] = category
            else:
                category = email_data["category"]
            
            # 마감일 추출 (제출 카테고리인 경우)
            due_date_str = "-"
            if "due_date" not in email_data and category == "제출":
                full_text = f"{subject} {body}"
                due_date = extract_due_date_candidate(full_text)
                if due_date:
                    email_data["due_date"] = due_date
                    _, remaining_str = calculate_days_remaining(due_date)
                    due_date_str = f"{due_date.strftime('%m/%d')} {remaining_str}"
                else:
                    email_data["due_date"] = None
            elif "due_date" in email_data and email_data["due_date"]:
                due_date = email_data["due_date"]
                _, remaining_str = calculate_days_remaining(due_date)
                due_date_str = f"{due_date.strftime('%m/%d')} {remaining_str}"
            
            self.tree.insert("", "end", text=str(idx), values=(category, due_date_str, subject, from_, date_header))
    
    def on_select(self, event):
        selection = self.tree.selection()
        if not selection:
            return
        
        item = selection[0]
        idx = int(self.tree.item(item, "text")) - 1
        
        if 0 <= idx < len(self.emails_data):
            email_data = self.emails_data[idx]
            
            # 상세 정보 표시
            self.detail_text.delete("1.0", "end")
            
            category = email_data.get('category', '미분류')
            due_date = email_data.get('due_date')
            
            content = f"제목: {email_data.get('subject', '')}\n"
            content += f"발신자: {email_data.get('from', '')}\n"
            content += f"날짜: {email_data.get('date_header', '')}\n"
            content += f"분류: {category}\n"
            
            if due_date:
                _, remaining_str = calculate_days_remaining(due_date)
                content += f"마감일: {due_date.strftime('%Y년 %m월 %d일')} ({remaining_str})\n"
            
            content += f"정규화된 제목: {email_data.get('subject_norm', '')}\n"
            content += "\n" + "="*50 + "\n\n"
            content += email_data.get('body', '')
            
            self.detail_text.insert("1.0", content)
            
            # 현재 분류 표시
            self.current_category_label.config(text=category)
            self.category_var.set(category)
            
            # 마감일 표시
            if due_date:
                days_remaining, remaining_str = calculate_days_remaining(due_date)
                due_str = f"{due_date.strftime('%Y-%m-%d')}\n{remaining_str}"
                
                # 색상 설정
                if days_remaining < 0:
                    color = "red"
                elif days_remaining == 0:
                    color = "red"
                elif days_remaining <= 3:
                    color = "orange"
                else:
                    color = "green"
                
                self.due_date_label.config(text=due_str, foreground=color)
                self.due_date_entry.delete(0, "end")
                self.due_date_entry.insert(0, due_date.strftime("%m/%d"))
            else:
                self.due_date_label.config(text="-", foreground="gray")
                self.due_date_entry.delete(0, "end")
    
    def apply_category(self):
        """선택한 메일에 분류 적용"""
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning("선택 없음", "분류를 변경할 메일을 선택하세요.")
            return
        
        item = selection[0]
        idx = int(self.tree.item(item, "text")) - 1
        new_category = self.category_var.get()
        
        if not new_category:
            messagebox.showwarning("분류 없음", "적용할 분류를 선택하세요.")
            return
        
        if 0 <= idx < len(self.emails_data):
            email_data = self.emails_data[idx]
            old_category = email_data.get("category", "")
            
            # 분류 변경
            email_data["category"] = new_category
            
            # AI 학습 데이터에 추가
            self.classifier.add_training_data(
                email_data.get("subject", ""),
                email_data.get("body", ""),
                new_category
            )
            
            # 트리 업데이트
            values = list(self.tree.item(item, "values"))
            values[0] = new_category
            
            # 마감일 재추출 (제출로 변경된 경우)
            if new_category == "제출" and not email_data.get("due_date"):
                full_text = f"{email_data.get('subject', '')} {email_data.get('body', '')}"
                due_date = extract_due_date_candidate(full_text)
                if due_date:
                    email_data["due_date"] = due_date
                    _, remaining_str = calculate_days_remaining(due_date)
                    values[1] = f"{due_date.strftime('%m/%d')} {remaining_str}"
                    self.due_date_label.config(text=f"{due_date.strftime('%Y-%m-%d')}\n{remaining_str}")
                    self.due_date_entry.delete(0, "end")
                    self.due_date_entry.insert(0, due_date.strftime("%m/%d"))
            elif new_category != "제출":
                # 제출이 아닌 경우 마감일 제거
                values[1] = "-"
                email_data["due_date"] = None
            
            self.tree.item(item, values=values)
            
            # 현재 분류 레이블 업데이트
            self.current_category_label.config(text=new_category)
            
            # 학습 상태 업데이트
            self.training_status_label.config(text=f"{len(self.classifier.training_data)}개 학습됨")
            
            # 할일 목록 업데이트
            self.populate_todo_tree()
            
            if old_category != new_category:
                messagebox.showinfo("분류 변경", f"'{old_category}' → '{new_category}'로 변경되었습니다.\nAI 학습이 업데이트되었습니다.")
    
    def apply_due_date(self):
        """마감일 수동 설정"""
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning("선택 없음", "마감일을 설정할 메일을 선택하세요.")
            return
        
        item = selection[0]
        idx = int(self.tree.item(item, "text")) - 1
        due_date_str = self.due_date_entry.get().strip()
        
        if not due_date_str:
            messagebox.showwarning("입력 오류", "마감일을 MM/DD 형식으로 입력하세요.\n예: 11/30")
            return
        
        # 날짜 파싱
        try:
            parts = due_date_str.split("/")
            if len(parts) != 2:
                raise ValueError("형식 오류")
            
            month, day = map(int, parts)
            year = datetime.now().year
            
            # 날짜 검증
            due_date = date(year, month, day)
            
            # 과거 날짜면 내년으로
            if due_date < date.today():
                due_date = date(year + 1, month, day)
            
        except Exception as e:
            messagebox.showerror("입력 오류", f"올바른 날짜 형식이 아닙니다.\nMM/DD 형식으로 입력하세요.\n예: 11/30\n\n오류: {e}")
            return
        
        if 0 <= idx < len(self.emails_data):
            email_data = self.emails_data[idx]
            email_data["due_date"] = due_date
            
            # 트리 업데이트
            values = list(self.tree.item(item, "values"))
            _, remaining_str = calculate_days_remaining(due_date)
            values[1] = f"{due_date.strftime('%m/%d')} {remaining_str}"
            self.tree.item(item, values=values)
            
            # 라벨 업데이트
            days_remaining, remaining_str = calculate_days_remaining(due_date)
            due_str = f"{due_date.strftime('%Y-%m-%d')}\n{remaining_str}"
            
            if days_remaining < 0:
                color = "red"
            elif days_remaining == 0:
                color = "red"
            elif days_remaining <= 3:
                color = "orange"
            else:
                color = "green"
            
            self.due_date_label.config(text=due_str, foreground=color)
            
            # 할일 목록 업데이트
            self.populate_todo_tree()
            
            messagebox.showinfo("마감일 설정", f"마감일이 {due_date.strftime('%Y년 %m월 %d일')}로 설정되었습니다.\n{remaining_str}")
    
    def add_category(self):
        """새 카테고리 추가"""
        dialog = tk.Toplevel(self.root)
        dialog.title("새 카테고리 추가")
        dialog.geometry("350x150")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        
        ttk.Label(frame, text="새 카테고리 이름:").pack(anchor="w", pady=(0, 5))
        
        category_entry = ttk.Entry(frame, width=30)
        category_entry.pack(pady=(0, 10))
        category_entry.focus()
        
        def save_new_category():
            new_cat = category_entry.get().strip()
            if not new_cat:
                messagebox.showwarning("입력 오류", "카테고리 이름을 입력하세요.")
                return
            
            if new_cat in self.categories:
                messagebox.showwarning("중복", "이미 존재하는 카테고리입니다.")
                return
            
            self.categories.append(new_cat)
            save_categories(self.categories)
            self.category_combo['values'] = self.categories
            messagebox.showinfo("성공", f"'{new_cat}' 카테고리가 추가되었습니다.")
            dialog.destroy()
        
        button_frame = ttk.Frame(frame)
        button_frame.pack(pady=(10, 0))
        
        ttk.Button(button_frame, text="추가", command=save_new_category).pack(side="left", padx=5)
        ttk.Button(button_frame, text="취소", command=dialog.destroy).pack(side="left", padx=5)
        
        # Enter 키로 저장
        category_entry.bind("<Return>", lambda e: save_new_category())
    
    def on_todo_select(self, event):
        """할일 목록 선택 이벤트"""
        selection = self.todo_tree.selection()
        if not selection:
            return
        
        item = selection[0]
        idx = int(self.todo_tree.item(item, "text")) - 1
        
        if 0 <= idx < len(self.emails_data):
            # TODO: 필요하면 전체 메일 탭과 연동
            pass
    
    def mark_todo_complete(self):
        """할일 완료 처리"""
        selection = self.todo_tree.selection()
        if not selection:
            messagebox.showwarning("선택 없음", "완료 처리할 할일을 선택하세요.")
            return
        
        item = selection[0]
        idx = int(self.todo_tree.item(item, "text")) - 1
        
        if 0 <= idx < len(self.emails_data):
            email_data = self.emails_data[idx]
            email_data["is_completed"] = True
            
            self.populate_todo_tree()
            messagebox.showinfo("완료", "할일이 완료 처리되었습니다.")
    
    def mark_todo_incomplete(self):
        """할일 미완료로 변경"""
        selection = self.todo_tree.selection()
        if not selection:
            messagebox.showwarning("선택 없음", "미완료로 변경할 할일을 선택하세요.")
            return
        
        item = selection[0]
        idx = int(self.todo_tree.item(item, "text")) - 1
        
        if 0 <= idx < len(self.emails_data):
            email_data = self.emails_data[idx]
            email_data["is_completed"] = False
            
            self.populate_todo_tree()
            messagebox.showinfo("변경", "할일이 미완료로 변경되었습니다.")
    
    def view_todo_detail(self):
        """할일 상세 보기"""
        selection = self.todo_tree.selection()
        if not selection:
            messagebox.showwarning("선택 없음", "상세 보기할 할일을 선택하세요.")
            return
        
        item = selection[0]
        idx = int(self.todo_tree.item(item, "text")) - 1
        
        if 0 <= idx < len(self.emails_data):
            email_data = self.emails_data[idx]
            
            # 상세 정보 다이얼로그
            detail_window = tk.Toplevel(self.root)
            detail_window.title("할일 상세 정보")
            detail_window.geometry("700x500")
            detail_window.transient(self.root)
            
            frame = ttk.Frame(detail_window, padding=20)
            frame.pack(fill="both", expand=True)
            
            # 정보 표시
            info_frame = ttk.Frame(frame)
            info_frame.pack(fill="x", pady=(0, 10))
            
            category = email_data.get('category', '미분류')
            due_date = email_data.get('due_date')
            is_completed = email_data.get('is_completed', False)
            
            ttk.Label(info_frame, text=f"분류: {category}", font=("", 10, "bold")).pack(anchor="w")
            
            if due_date:
                days_remaining, remaining_str = calculate_days_remaining(due_date)
                color = "red" if days_remaining < 0 else "orange" if days_remaining <= 3 else "green"
                due_label = ttk.Label(info_frame, 
                                     text=f"마감일: {due_date.strftime('%Y년 %m월 %d일')} ({remaining_str})",
                                     font=("", 10), foreground=color)
                due_label.pack(anchor="w")
            
            status_text = "✓ 완료됨" if is_completed else "☐ 진행 중"
            ttk.Label(info_frame, text=f"상태: {status_text}", font=("", 10)).pack(anchor="w")
            
            ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=10)
            
            # 메일 내용
            content_frame = ttk.LabelFrame(frame, text="메일 내용", padding=10, borderwidth=0, relief="flat")
            content_frame.pack(fill="both", expand=True)
            
            text_widget = tk.Text(content_frame, wrap="word", height=15)
            text_scroll = ttk.Scrollbar(content_frame, orient="vertical", command=text_widget.yview)
            text_widget.configure(yscrollcommand=text_scroll.set)
            
            content = f"제목: {email_data.get('subject', '')}\n"
            content += f"발신자: {email_data.get('from', '')}\n"
            content += f"날짜: {email_data.get('date_header', '')}\n"
            content += "\n" + "="*50 + "\n\n"
            content += email_data.get('body', '')
            
            text_widget.insert("1.0", content)
            text_widget.config(state="disabled")
            
            text_widget.pack(side="left", fill="both", expand=True)
            text_scroll.pack(side="right", fill="y")
            
            # 버튼
            button_frame = ttk.Frame(frame)
            button_frame.pack(fill="x", pady=(10, 0))
            
            if is_completed:
                ttk.Button(button_frame, text="미완료로 변경", 
                          command=lambda: [email_data.update({"is_completed": False}), 
                                          self.populate_todo_tree(), detail_window.destroy()]).pack(side="right", padx=5)
            else:
                ttk.Button(button_frame, text="완료 처리", 
                          command=lambda: [email_data.update({"is_completed": True}), 
                                          self.populate_todo_tree(), detail_window.destroy()]).pack(side="right", padx=5)
            
            ttk.Button(button_frame, text="닫기", command=detail_window.destroy).pack(side="right")


# =====================================================
# Main
# =====================================================


from ttkthemes import ThemedTk
import os

if __name__ == "__main__":
    # ThemedTk로 테마 적용 (adapta)
    root = ThemedTk(theme="adapta")

    # BMJUA_ttf.ttf 폰트 family명 자동 감지 및 전체 적용
    font_path = os.path.join(os.path.dirname(__file__), "BMDOHYEON_ttf.ttf")
    try:
        # 윈도우에서 ttf 직접 로드
        if os.name == "nt":
            import ctypes
            FR_PRIVATE = 0x10
            ctypes.windll.gdi32.AddFontResourceExW(font_path, FR_PRIVATE, 0)

        # 사용 가능한 폰트 family명 목록 확인
        available_fonts = list(tkfont.families(root))
        print("[DEBUG] 사용 가능한 폰트 family명:")
        for fam in sorted(available_fonts):
            print(fam)
        # 나눔스퀘어(NanumSquare) family명으로 강제 지정
        nanum_family = 'NanumSquare'
        print(f"[DEBUG] 폰트 family명 강제 적용: {nanum_family}")

        # Tkinter 기본 폰트 전체 변경
        for font_name in ["TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"]:
            root.tk.call("font", "configure", font_name, "-family", nanum_family, "-size", 11)

        # ttk 위젯에도 폰트 강제 적용
        style = ttk.Style(root)
        style.configure("TLabel", font=(nanum_family, 11))
        style.configure("TButton", font=(nanum_family, 11))
        style.configure("TEntry", font=(nanum_family, 11))
        style.configure("TMenubutton", font=(nanum_family, 11))
        style.configure("Treeview", font=(nanum_family, 11))
        style.configure("TNotebook", font=(nanum_family, 11))
        style.configure("TNotebook.Tab", font=(nanum_family, 11))
        style.configure("TCombobox", font=(nanum_family, 11))
        style.configure("TCheckbutton", font=(nanum_family, 11))
        style.configure("TRadiobutton", font=(nanum_family, 11))
        style.configure("TFrame", font=(nanum_family, 11))
        style.configure("TLabelframe", font=(nanum_family, 11))
        style.configure("TSeparator", font=(nanum_family, 11))
        style.configure("TScrollbar", font=(nanum_family, 11))
        style.configure("TProgressbar", font=(nanum_family, 11))
        style.configure("TScale", font=(nanum_family, 11))
        style.configure("TPanedwindow", font=(nanum_family, 11))
        style.configure("TSpinbox", font=(nanum_family, 11))
        style.configure("TSizegrip", font=(nanum_family, 11))
        print(f"[DEBUG] 폰트 family명 강제 적용: {nanum_family}")
    except Exception as e:
        print(f"BMJUA 폰트 적용 실패: {e}")

    app = TodoApp(root)
    root.mainloop()
