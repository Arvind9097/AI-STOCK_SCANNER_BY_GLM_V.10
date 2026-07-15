# breaking_news.py
"""
===========================================================
 BREAKING NEWS - REAL-TIME INDIAN MARKET HEADLINES (V8.1.2)
===========================================================
User requirement: "Indian share market related news jo Hindi mein
ho... TOP BREAKING NEWS hi send ho... quick response jaise hi koi
news update aaye"

V8.1.2 UPDATE 3 (naya): "breaking news sirf Indian market se
sambandhit hona chahiye - share, equity, listed company ke baare
mein. News ka heading stylish karo. News sambandhi brief news
hona chahiye aur shortened link hona chahiye."
  -> Keyword-based STRICT FILTER add kiya (sirf equity/share-market
     specific news pass hoti hai - politics, world-news, generic
     lifestyle content ab automatically exclude hoti hai)
  -> Stylish heading format (category-tag + bold + emoji-accent)
  -> Brief summary (feedparser ka entry.summary field use karta hai,
     max ~120 characters tak truncate, sirf headline nahi)
  -> Link shortening (is.gd - free, no signup/API-key chahiye)

Ye module V8.1 ke news.py se ALAG hai:
  - news.py     -> per-stock cached news (jab koi specific stock
                   query kare, "Reliance ki news batao" jaisa)
  - ye module   -> GENERAL market-wide breaking news, poll karta
                   rehta hai (background thread), naya headline
                   milte hi TURANT Telegram par bhej deta hai

SOURCES (sab free, RSS - koi API key nahi chahiye):
  1. Moneycontrol - Latest News
  2. Economic Times - Default RSS
  3. Business Standard - Top Stories
  4. LiveMint - Markets

Har feed independent try/except mein hai - agar ek feed down/block ho
jaaye, baaki feeds phir bhi kaam karte rehte hain (poora feature fail
nahi hota).

DEDUPLICATION: Ek baar bheja gaya headline (link+title ke hash se
track hota hai) dobara nahi bheja jaata. State file mein persist hota
hai (taaki restart ke baad bhi purani news dobara na bhejein) - agar
state file kho jaaye (Render restart, ephemeral disk), worst case
sirf kuch headlines dobara bhej di jaayengi - crash kabhi nahi hota.

BROWSER-LIKE HEADERS: Kai publishers (Moneycontrol, ET) apne CDN
(CloudFront) par default feedparser User-Agent ko 403 se block karte
hain - isliye pehle 'requests' se browser-jaisa User-Agent bhejke raw
feed content download karte hain, phir usko feedparser ko parse karne
ke liye dete hain (seedha URL feedparser ko nahi dete).

IMPORTANT: RSS URLs neeche dee gayi hain - Moneycontrol, Economic
Times, aur Business Standard multiple independent sources se verify
kiye gaye hain. LiveMint ka URL is waqt is environment se directly
fetch karke test nahi ho paya (network block) - agar log mein
"LiveMint: RSS malformed ya empty" baar-baar dikhe, publisher ki
website se current RSS URL dhoondh kar neeche update kar dena (baaki
3 sources tab bhi normally kaam karte rahenge).
===========================================================
"""

import os
import re
import json
import time
import html
import hashlib
import threading
import requests

from logger import logger

# V8.2.0 FIX (bug #14): thread-safety - poller thread + any manual
# bot_listener call to check_and_dispatch_new_news() concurrently
# could corrupt seen-set / topic-list state (TOCTOU race). Module-level
# lock guards all mutations of state files.
_state_lock = threading.Lock()

try:
    import feedparser
except ImportError:
    feedparser = None
    logger.warning(
        "'feedparser' package install nahi hai - breaking news feature "
        "kaam nahi karega. Install: pip install feedparser"
    )

# V8.3.0 (Task G5): Hinglish translation - stock names ENGLISH mein
# preserve hote hain (RELIANCE, TCS, M&M, NIFTY, etc). Priority:
# to_hinglish (best) > to_hindi (legacy Devanagari) > identity.
try:
    from translator import to_hinglish
except ImportError:
    to_hinglish = None

try:
    from translator import to_hindi
except ImportError:
    def to_hindi(text, max_len=450):
        return text


# -----------------------------------------------------------
# CONFIG
# -----------------------------------------------------------
# IMPORTANT: Ye publishers apne RSS URL structure kabhi bhi badal sakte
# hain (jaisa NSE ke unofficial endpoints ke saath hota hai - dekho
# nse_market_data.py ka disclaimer). Agar koi feed consistently 0
# items de raha hai (log mein "RSS malformed ya empty" dikhega), to:
#   1. Publisher ki website par jaake unka current RSS feed URL dhoondo
#      (aksar footer mein "RSS" link hota hai, ya /rss-feeds/ path par)
#   2. Neeche wali dict mein sirf wo ek URL update kar do - baaki poora
#      module (dedup, Hindi translation, Telegram dispatch) waisa hi
#      chalega, kuch aur badalne ki zaroorat nahi.
# Har feed independent hai - ek galat/dead URL se sirf wo EK source
# skip hota hai, baaki teenon phir bhi kaam karte rehte hain.
# V8.3.3 UPDATE: Cloud (Render) par test karke pata chala ki 3 of 4
# purane RSS sources BLOCK ho jaate hain (Moneycontrol 403, Business
# Standard 403, LiveMint 403). Sirf Economic Times kaam karta hai.
#
# V9.1 UPDATE: User complaint tha ki global news aa rahi (sirf Indian
# stock market related chahiye). Google News ke generic "indian stock
# market" search bahut broad tha (world economy, US market, etc bhi aa
# jaate the). Ab Google News feeds HATA diye — sirf Economic Times ke
# 4 highly-specific equity-market feeds use karte hain. Ye sab strictly
# Indian share market focused hain (koi global/politics/sports mix nahi).
RSS_SOURCES = {
    "ET Markets":       "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "ET Stocks":        "https://economictimes.indiatimes.com/stocks/rssfeeds/2143744245.cms",
    "ET Stocks News":   "https://economictimes.indiatimes.com/stocks/stock-quotes/rssfeeds/1152489465.cms",
    "ET Live BSE/NSE":  "https://economictimes.indiatimes.com/markets/stocks/live-bse-nse-stock-quote/rssfeeds/102573178.cms",
    "ET Live Commentary": "https://economictimes.indiatimes.com/markets/stocks/live-commentary/rssfeeds/102573227.cms",
}

# Har feed check karte waqt sirf TOP N latest items dekhte hain (poora
# feed history nahi) - "top breaking news hi" requirement ke liye.
TOP_N_PER_FEED = 5

# Kitni der mein ek baar saare feeds check karein (seconds). Chhota
# rakha hai taaki "jaise hi news aaye turant" requirement pura ho,
# lekin itna bhi chhota nahi ki publishers ko spam kar de.
POLL_INTERVAL_SEC = 180  # 3 minute

# State file - already-sent headlines ka hash yahan store hota hai
SEEN_STATE_FILE = "data/breaking_news_seen.json"
MAX_SEEN_HISTORY = 500  # itne purane hash yaad rakho, usse zyada purge kar do

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

_REQUEST_TIMEOUT = 12

# -----------------------------------------------------------
# V8.1.2 NAYA: INDIAN EQUITY/SHARE-MARKET FILTER
# -----------------------------------------------------------
# User requirement: "breaking stocks sirf Indian market se sambandhit
# hona chahiye. Share, equity, listed company ke baare mein."
#
# Sabhi 4 RSS feeds "latest news"/"top stories" wale hain - politics,
# world-news, generic-business, lifestyle sab mix hote hain. Isliye
# do-tarafa keyword filter: INCLUDE (equity/market-specific signal)
# aur EXCLUDE (jo saaf tor par non-market/politics/world-news hai).
# Ek headline sirf tab pass hoti hai jab:
#   1. Kam se kam EK INCLUDE keyword match kare, AND
#   2. KOI BHI EXCLUDE keyword match NA kare
INCLUDE_KEYWORDS = [
    # Indices/Exchanges
    "nifty", "sensex", "bse", "nse", "share market", "stock market",
    "equity", "equities", "stock exchange",
    # Company/listing terms
    "listed company", "shares of", "stock price", "share price",
    "q1 results", "q2 results", "q3 results", "q4 results",
    "quarterly results", "earnings", "ipo", "listing", "delisting",
    "bonus shares", "stock split", "buyback", "rights issue",
    "dividend", "market cap", "target price", "rating upgrade",
    "rating downgrade", "brokerage", "upper circuit", "lower circuit",
    "52-week high", "52-week low", "block deal", "bulk deal",
    "fii", "dii", "mutual fund", "sebi", "rbi policy",
    "sector index", "nifty bank", "nifty it", "midcap", "smallcap",
    "largecap", "multibagger", "stock to buy", "stock to sell",
    "shares surge", "shares jump", "shares fall", "shares slump",
    "shares rally", "stake sale", "acquisition", "merger", "demerger",
    "order win", "orders worth", "contract worth",
]

EXCLUDE_KEYWORDS = [
    # Politics/international/generic - ye is hi feeds mein occasionally
    # mix ho jaate hain, market ke sath direct sambandh nahi hota
    "election", "parliament", "assembly poll", "chief minister",
    "prime minister", "president trump", "white house", "war ",
    "ukraine", "gaza", "israel", "bollywood", "cricket score",
    "world cup", "olympics", "cyclone", "earthquake", "monsoon forecast",
    "recipe", "horoscope", "health tips", "fashion trend",
    # V9.1: Global/foreign market news (user sirf Indian equity chahiye)
    "wall street", "dow jones", "nasdaq", "s&p 500", "sp 500",
    "us stock", "u.s. stock", "american market", "new york stock",
    "london stock", "ftse 100", "dax", "nikkei", "hang seng",
    "shanghai", "china stock", "japan stock", "korean market",
    "european central bank", "federal reserve", "fed chair",
    "global market", "world market", "international market",
    "oil price", "crude oil", "gold price", "silver price",
    "bitcoin", "ethereum", "cryptocurrency", "crypto market",
    # V9.1: Non-equity Indian topics (politics/entertainment/sports)
    "modi", "rahul gandhi", "kejriwal", "bjp ", "congress ",
    "poll result", "vote bank", "rally protest",
    "entertainment", "movie review", "ott release", "web series",
    "iplt20", "ipl match", "ind vs ", "test match",
    # V9.1: Forex/bond/commodity (not equity)
    "rupee vs dollar", "currency future", "bond yield",
    "commodity market", "mcx", "ncdex",
]

# Har headline ke saath "brief news" - feedparser ka summary field
# use hota hai (agar available ho). User requirement: "kam se kam 100
# words". Words approximate karne ke liye character-count use karte
# hain (average English/Hindi word ~6 characters + space = 7), isliye
# 100 words ~700 characters ka target rakhte hain.
BRIEF_SUMMARY_MAX_CHARS = 700
BRIEF_SUMMARY_MIN_WORDS_TARGET = 100
# Title sirf tab prepend hoti hai jab RSS summary GENUINELY bahut
# chhoti ho (isse kam - "target" ki taraf force-fit karne ke liye
# nahi, warna already-meaningful summaries mein title dobara aakar
# awkward repetition lagta hai)
BRIEF_SUMMARY_TITLE_PREPEND_THRESHOLD = 25

# Link shortening - is.gd free hai, koi signup/API-key nahi chahiye
LINK_SHORTENER_ENABLED = True
LINK_SHORTENER_API = "https://is.gd/create.php"
LINK_SHORTENER_TIMEOUT = 6

# User requirement: "URL ko hide kar de sirf likh - Click here for
# more details". HTML <a href> tag use karte hain (Telegram parse_mode
# HTML already hai) - link clickable rehta hai, bas raw URL text mein
# nahi dikhta.
LINK_DISPLAY_TEXT = "🔗 Click Here for More Details"

# -----------------------------------------------------------
# V8.1.2 UPDATE 5: CROSS-SOURCE DUPLICATE DETECTION
# -----------------------------------------------------------
# Problem: Same news (jaise "Reliance Q1 results") 4 alag publishers
# (Moneycontrol/ET/BS/LiveMint) alag wording mein likhte hain - purana
# exact-hash dedup (link+title) inhe ALAG samajh kar 4 baar bhej deta
# tha (user complaint: "ek hi type ke message ko send nahi kare").
#
# Fix: Har naye headline ko pichhle DEDUP_WINDOW_MINUTES ke andar
# bheji gayi headlines se word-overlap (Jaccard similarity) compare
# karte hain. Agar similarity threshold se zyada ho, to ye "same topic"
# maana jaata hai aur skip ho jaata hai - chahe alag source/wording ho.
DEDUP_SIMILARITY_THRESHOLD = 0.5   # 50%+ significant-word overlap = same topic
DEDUP_WINDOW_MINUTES = 180          # 3 ghante ke andar ki headlines se hi compare karo
# Common words jo har headline mein hote hain (stopwords) - inhe
# similarity-comparison se exclude karte hain, taaki sirf STOCK NAMES
# aur KEY NUMBERS/EVENTS match karein, generic words nahi.
DEDUP_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or",
    "is", "are", "was", "were", "be", "been", "with", "as", "by", "from",
    "this", "that", "it", "its", "after", "before", "over", "under",
    "up", "down", "news", "today", "share", "shares", "stock", "stocks",
    "market", "markets", "says", "said", "report", "reports", "may",
    "will", "has", "have", "had", "into", "amid", "amidst", "than",
}


# -----------------------------------------------------------
# SEEN-STATE (dedup persistence) - EXACT hash (same link+title)
# -----------------------------------------------------------
# V8.2.0 FIX (bug #5, #32): Pehle set() use hota tha jiska iteration
# order deterministic nahi tha - list(seen_set)[-500:] randomly
# recent ya old hashes drop kar deta tha. Ab ordered LIST use karte
# hain (insertion-order preserved) taaki `[-500:]` guarantee kare
# ki MOST RECENT 500 hash retain ho (purane purge ho jaayein).
# Side-benefit: state file ab diffable (deterministic order).
def _load_seen():
    """Ordered list of seen headline hashes (most-recent last).
    Backward-compat: purana set-format JSON bhi load karta hai (dedup
    karte hue ordered list banata hai)."""
    if not os.path.exists(SEEN_STATE_FILE):
        return []
    try:
        with open(SEEN_STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Backward-compat: agar purana set-format JSON mila (list of
        # hashes without order guarantee), dedup karte hue list banao.
        if isinstance(raw, list):
            seen = []
            for h in raw:
                if h not in seen:
                    seen.append(h)
            return seen
        if isinstance(raw, set):
            return list(raw)
        return []
    except Exception:
        return []


def _save_seen(seen_list):
    """Persist seen hashes list (ordered, most-recent last).
    Trim karte waqt list ke LAST MAX_SEEN_HISTORY retain karte hain
    (insertion-order preserved -> most-recent retained, oldest purged)."""
    try:
        os.makedirs(os.path.dirname(SEEN_STATE_FILE), exist_ok=True)
        # V8.2.0 FIX (bug #5): list slicing preserves insertion order
        # (set iteration nahi) -> most-recent 500 retain hote hain.
        trimmed = list(seen_list)[-MAX_SEEN_HISTORY:]
        # V8.2.0 FIX (bug #32): atomic write via utils helper (crash-safe).
        from utils import atomic_write_json
        atomic_write_json(SEEN_STATE_FILE, trimmed)
    except Exception as e:
        logger.debug(f"Breaking news seen-state save fail (harmless): {e}")


def _headline_hash(link, title):
    """Link + title dono se hash banate hain (link change ho lekin title
    same rahe jaise cases ko bhi catch karne ke liye)."""
    key = f"{link}|{title}".encode("utf-8", errors="ignore")
    return hashlib.md5(key).hexdigest()


# -----------------------------------------------------------
# V8.1.2 UPDATE 5: CROSS-SOURCE SIMILARITY DEDUP (topic-level,
# alag-alag publisher ki alag-wording wali SAME news ko bhi pakadta hai)
# -----------------------------------------------------------
TOPIC_STATE_FILE = "data/breaking_news_topics.json"
MAX_TOPIC_HISTORY = 200  # itni purani topic-entries yaad rakho


def _extract_keywords(title):
    """
    Headline se significant words nikalta hai (stopwords hata kar,
    lowercase, sirf alphanumeric). Return: set of words - isi set ka
    do headlines ke beech overlap (Jaccard similarity) nikalte hain.
    """
    words = re.findall(r"[a-z0-9]+", title.lower())
    return {w for w in words if w not in DEDUP_STOPWORDS and len(w) > 2}


def _extract_proper_nouns(title):
    """
    Title mein Capitalized words nikalta hai (company names jaise
    "Reliance", "TCS", "HDFC", "Infosys" - English headlines mein
    ye almost hamesha capitalized hote hain). Ye keyword-overlap se
    ZYADA RELIABLE signal hai company-identity ke liye, kyunki alag
    publishers same company ke baare mein likhte waqt bhi naam wahi
    rakhte hain, chahe baaki wording bilkul alag ho.
    """
    words = re.findall(r"\b[A-Z][a-zA-Z]+\b", title)
    return {w.lower() for w in words if len(w) > 1}


def _jaccard_similarity(set_a, set_b):
    """Do word-sets ka overlap ratio (0.0 = bilkul alag, 1.0 = same)."""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def _load_recent_topics():
    """
    Return: list of dict {keywords: [...], proper_nouns: [...],
    timestamp: float} - sirf DEDUP_WINDOW_MINUTES ke andar wali
    entries (purani apne aap expire ho jaati hain).
    """
    if not os.path.exists(TOPIC_STATE_FILE):
        return []
    try:
        with open(TOPIC_STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        cutoff = time.time() - (DEDUP_WINDOW_MINUTES * 60)
        return [t for t in raw if t.get("timestamp", 0) >= cutoff]
    except Exception:
        return []


def _save_recent_topics(topics):
    try:
        os.makedirs(os.path.dirname(TOPIC_STATE_FILE), exist_ok=True)
        trimmed = topics[-MAX_TOPIC_HISTORY:]
        # V8.2.0 FIX: atomic write (crash-safe).
        from utils import atomic_write_json
        atomic_write_json(TOPIC_STATE_FILE, trimmed)
    except Exception as e:
        logger.debug(f"Breaking news topic-state save fail (harmless): {e}")


def _is_duplicate_topic(title, recent_topics):
    """
    Check karta hai ki ye headline pichhle DEDUP_WINDOW_MINUTES mein
    bheji gayi kisi headline se topic-wise "same" to nahi hai (alag
    publisher, alag wording, lekin same news).

    HYBRID LOGIC (V8.1.2 fix): Sirf generic keyword-overlap (Jaccard)
    real-world headlines ke liye kaafi reliable nahi tha - alag
    publishers itne alag verbs/adjectives use karte hain ("shares
    slump" vs "stock falls") ki overlap sirf 15-20% reh jaata hai,
    jabki company-naam (Proper Nouns - "Reliance", "Infosys", "TCS")
    hamesha consistent rehta hai. Isliye:
      - PEHLE proper-noun (company-name) overlap check karo - agar
        koi common company-name mile, to ye STRONG signal hai
      - Us case mein, sirf EK company-naam match hone se galti se
        alag-alag news duplicate na maani jaaye (jaise "Reliance
        Q1 results" vs "Reliance chairman speech" alag topics ho
        sakte hain), isliye company-match ke SAATH kam se kam EK
        aur significant keyword bhi match hona chahiye
      - Agar koi common company-naam NA mile, to normal (generic)
        keyword-Jaccard-threshold use karo (fallback, jab dono
        headlines mein company-naam na ho, jaise "Sensex Nifty
        record high" jaisी market-wide news)

    Return: bool (True = duplicate topic, skip kar do)
    """
    new_keywords = _extract_keywords(title)
    new_proper_nouns = _extract_proper_nouns(title)

    if not new_keywords:
        return False  # keywords hi nahi nikle to safe side - duplicate mat maano

    for topic in recent_topics:
        existing_keywords = set(topic.get("keywords", []))
        existing_proper_nouns = set(topic.get("proper_nouns", []))

        # STRONG SIGNAL: common company-naam - high-confidence duplicate.
        # NOTE: Sirf company-naam match hone par bhi duplicate maan lete
        # hain (koi extra keyword-overlap requirement nahi) - practically
        # ek hi company ke baare mein DEDUP_WINDOW_MINUTES (3 ghante) ke
        # andar 2 alag headlines aana zyadatar EK HI event hota hai
        # (companies itni frequently multiple GENUINELY-distinct news
        # generate nahi karti). Trade-off: bahut rare case mein (jaise
        # ek hi company ki 2 sach mein alag khabrein 3 ghante ke andar)
        # doosri wali skip ho sakti hai - lekin isse user ka main
        # complaint (same news baar-baar alag wording mein) poori tarah
        # solve hota hai.
        if new_proper_nouns & existing_proper_nouns:
            return True

        # FALLBACK: koi common company-naam nahi mila - generic Jaccard
        # threshold try karo (market-wide news jaise "Sensex hits
        # record high" ke liye, jahan koi single company nahi hai)
        similarity = _jaccard_similarity(new_keywords, existing_keywords)
        if similarity >= DEDUP_SIMILARITY_THRESHOLD:
            return True

    return False


# -----------------------------------------------------------
# FEED FETCH (per-source, isolated failure)
# -----------------------------------------------------------
def _fetch_one_feed(source_name, url):
    """
    Ek RSS source se latest headlines nikalta hai. Return: list of
    dict {title, link, source} (max TOP_N_PER_FEED items) ya [] agar
    fail ho jaaye (KABHI exception raise nahi karta - calling loop
    ke liye poora feature safe rehta hai).
    """
    if feedparser is None:
        return []

    try:
        # Browser-jaisa User-Agent bhejke raw content download karo
        # (kai publishers ka CDN default feedparser agent ko 403 block
        # karta hai) - phir feedparser ko RAW BYTES parse karne do,
        # seedha URL nahi.
        resp = requests.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()

        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            # bozo=True matlab malformed XML mila - lekin agar entries
            # phir bhi mil gaye to unhe use kar lete hain (feedparser
            # lenient hai), sirf tab skip karte hain jab kuch na mile
            logger.debug(f"{source_name}: RSS malformed ya empty, skip kar raha hoon")
            return []

        items = []
        for entry in parsed.entries[:TOP_N_PER_FEED]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", "").strip()
            if not title:
                continue
            items.append({"title": title, "link": link, "source": source_name, "summary": summary})
        return items

    except Exception as e:
        logger.debug(f"{source_name}: RSS fetch fail ({e})")
        return []


# -----------------------------------------------------------
# V8.1.2 NAYA: INDIAN EQUITY/SHARE-MARKET FILTER
# -----------------------------------------------------------
def _is_indian_equity_news(title, summary=""):
    """
    Check karta hai ki ye headline Indian share market/equity/listed-
    company se related hai ya nahi. INCLUDE_KEYWORDS mein se kam se
    kam ek match hona chahiye, AUR EXCLUDE_KEYWORDS mein se koi bhi
    match NAHI hona chahiye.

    Case-insensitive matching (title + summary dono check hote hain,
    kabhi kabhi title mein signal na ho par summary mein ho).
    """
    combined_text = f"{title} {summary}".lower()

    for exclude_kw in EXCLUDE_KEYWORDS:
        if exclude_kw in combined_text:
            return False

    for include_kw in INCLUDE_KEYWORDS:
        if include_kw in combined_text:
            return True

    return False


# -----------------------------------------------------------
# V8.1.2 NAYA: BRIEF SUMMARY EXTRACTION (~100 words)
# -----------------------------------------------------------
def _clean_html_tags(text):
    """RSS summary field mein kabhi-kabhi HTML tags aa jaate hain (e.g.
    <p>, <a href>) - inhe hata kar plain text banata hai."""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()


def _count_words(text):
    return len(text.split())


def _get_brief_summary(summary, title="", max_chars=BRIEF_SUMMARY_MAX_CHARS,
                        min_words_target=BRIEF_SUMMARY_MIN_WORDS_TARGET):
    """
    "Brief news" requirement - target ~100 words ka meaningful brief,
    sirf headline nahi. feedparser ka RSS summary field aksar khud
    chhota hota hai (kabhi sirf 10-40 words) - agar wo target se kam
    ho, to title ko bhi context ke roop mein prepend karte hain.

    IMPORTANT HONESTY NOTE: Ye function KABHI BHI fake/padded content
    generate nahi karta sirf 100-word target poora karne ke liye - agar
    RSS source khud bahut kam text de (title+summary milakar bhi
    ~100 words se kam), to jitna GENUINE content available hai utna
    hi bheja jaata hai. Trading-alerts ke liye fabricated/hallucinated
    text bhejna khatarnaak hoga - isliye "target" hai, "guarantee" nahi.
    Zyadatar established publishers (Moneycontrol, ET, Business
    Standard) ke summary fields is target ke aas-paas hi hote hain.

    Max max_chars tak truncate hota hai (word-boundary par, beech
    shabd mein nahi todta) - taaki Telegram message bahut lamba na ho
    jaaye agar RSS summary khud bahut bada ho.

    Return: "" agar na summary na title mila (calling code sirf
    headline hi dikha dega us case mein).
    """
    clean_summary = _clean_html_tags(summary)

    if not clean_summary:
        return ""  # kuch summary nahi mila - headline hi kaafi hai

    combined = clean_summary
    # Agar RSS summary GENUINELY bahut chhoti hai (< threshold, ~25
    # words - "target" 100 nahi), title ko context ke roop mein pehle
    # jod do - warna already-meaningful summaries mein title dobara
    # jodna awkward repetition lagta hai
    if title and _count_words(clean_summary) < BRIEF_SUMMARY_TITLE_PREPEND_THRESHOLD:
        clean_title = _clean_html_tags(title)
        if clean_title and clean_title.lower() not in clean_summary.lower():
            combined = f"{clean_title}. {clean_summary}"

    if len(combined) <= max_chars:
        return combined

    # Word-boundary tak trim karo (beech shabd mein mat todo)
    truncated = combined[:max_chars].rsplit(" ", 1)[0]
    return truncated + "..."


# -----------------------------------------------------------
# V8.1.2 NAYA: LINK SHORTENING (is.gd - free, no signup)
# -----------------------------------------------------------
def _shorten_link(long_url):
    """
    is.gd free API se link shorten karta hai. Fail hone par (network
    issue, API down) ORIGINAL link hi return karta hai - kabhi crash
    nahi karta, aur link kabhi missing nahi hota.
    """
    if not LINK_SHORTENER_ENABLED or not long_url:
        return long_url

    try:
        resp = requests.get(
            LINK_SHORTENER_API,
            params={"format": "simple", "url": long_url},
            timeout=LINK_SHORTENER_TIMEOUT,
        )
        if resp.status_code == 200 and resp.text.startswith("http"):
            return resp.text.strip()
    except Exception as e:
        logger.debug(f"Link shortening fail (asli link use kar raha hoon): {e}")

    return long_url


# -----------------------------------------------------------
# V8.3.0 (Task G5) NAYA: HINGLISH TRANSLATION HELPER
# -----------------------------------------------------------
# Breaking news headlines/summaries ko Hinglish mein translate karta
# hai. Stock names/tickers ENGLISH mein preserve hote hain.
#
# Priority chain (kabhi crash nahi karta - worst case original text):
#   1. translator.to_hinglish() - BEST. Roman Hinglish agar ZAI_API_KEY
#      set (GLM API), warn Devanagari + English stock names (placeholder
#      technique with Google Translate).
#   2. translator.to_hindi() - legacy fallback. Full Devanagari (stock
#      names bhi translate ho jaate hain - less ideal, par kabhi crash
#      nahi). Used only if to_hinglish import fail ho.
#   3. Original text - sab fail (kabhi nahi hona chahiye).
def _translate_to_hinglish(text):
    """
    V8.3.0 (Task G5): text ko Hinglish mein translate karta hai with
    English stock name preservation. Never crashes - worst case returns
    original text.

    Args:
        text: English (ya mixed) news headline / summary

    Return: Hinglish-translated string (Roman ya Devanagari-with-
            English-stocks), ya original text on failure.
    """
    if not text:
        return text
    # 1. Best: to_hinglish (Roman + English stock names)
    if to_hinglish is not None:
        try:
            return to_hinglish(text)
        except Exception as e:
            logger.debug(f"to_hinglish fail on breaking news ({e}), fallback chain")
    # 2. Legacy: to_hindi (full Devanagari)
    if to_hindi is not None:
        try:
            return to_hindi(text)
        except Exception as e:
            logger.debug(f"to_hindi fail on breaking news ({e}), original return")
    # 3. Original
    return text


# -----------------------------------------------------------
# V8.1.2 NAYA: STYLISH HEADING FORMAT
# -----------------------------------------------------------
def _detect_category_tag(title, summary=""):
    """
    Headline ke content ke hisaab se ek chhota category-tag chunta hai
    (stylish heading ke liye) - "results", "ipo", "rating", waghera.
    Match na ho to default "MARKET" return karta hai.
    """
    text = f"{title} {summary}".lower()
    tag_rules = [
        (["q1 results", "q2 results", "q3 results", "q4 results", "quarterly results", "earnings"], "📊 RESULTS"),
        (["ipo", "listing", "delisting"], "🆕 IPO"),
        (["upper circuit", "lower circuit", "shares surge", "shares jump", "shares rally", "52-week high"], "🚀 RALLY"),
        (["shares fall", "shares slump", "52-week low"], "📉 DECLINE"),
        (["rating upgrade", "rating downgrade", "brokerage", "target price"], "🎯 RATING"),
        (["acquisition", "merger", "demerger", "stake sale"], "🤝 M&A"),
        (["dividend", "bonus shares", "stock split", "buyback"], "💰 CORP. ACTION"),
        (["rbi policy", "sebi", "fii", "dii"], "🏛️ REGULATORY"),
        (["nifty", "sensex", "market cap"], "📈 INDEX"),
    ]
    for keywords, tag in tag_rules:
        if any(kw in text for kw in keywords):
            return tag
    return "💹 MARKET"


def get_latest_breaking_news():
    """
    Saare configured RSS sources se latest headlines nikalta hai (har
    source independently, ek fail ho to baaki chalte rehte hain).

    V8.1.2 NAYA: Ab sirf Indian equity/share-market related headlines
    return hoti hain - _is_indian_equity_news() filter automatically
    apply hota hai. Politics, world-news, generic-lifestyle content
    yahin discard ho jaata hai (dispatch tak kabhi nahi pahunchta).

    Return: list of dict {title, link, source, summary}
    """
    all_items = []
    filtered_out_count = 0

    for source_name, url in RSS_SOURCES.items():
        items = _fetch_one_feed(source_name, url)
        for item in items:
            if _is_indian_equity_news(item["title"], item.get("summary", "")):
                all_items.append(item)
            else:
                filtered_out_count += 1

    if filtered_out_count:
        logger.debug(f"Breaking news filter: {filtered_out_count} non-equity headlines discard ki gayin")

    return all_items


# -----------------------------------------------------------
# NEW-HEADLINE DETECTION + TELEGRAM DISPATCH
# -----------------------------------------------------------
def check_and_dispatch_new_news(send_func, translate_to_hindi=True):
    """
    Saare feeds check karta hai, jo headlines PEHLE kabhi nahi bheji
    gayi unhe Hindi mein translate karke `send_func(text)` ke through
    turant bhej deta hai. Dedup state file mein persist hoti hai.

    V8.1.2 UPDATE 5: Ab DO-LAYER dedup hai:
      1. EXACT hash (link+title) - jaisa pehle tha
      2. SIMILARITY-based topic dedup (naya) - agar Moneycontrol,
         ET, BS, LiveMint sab EK HI news ko alag wording mein likhein
         ("Reliance Q1 results beat estimates" vs "Reliance reports
         strong Q1 numbers"), to sirf PEHLI wali bheji jaati hai,
         baaki teenon skip ho jaati hain (DEDUP_WINDOW_MINUTES ke
         andar). Isse ek hi topic par multiple similar messages
         nahi aate.

    V8.2.0 FIXES:
      - bug #2 (CRITICAL): title/brief/short_link ab HTML-escape
        hote hain before insertion into parse_mode=HTML Telegram
        message. Pehle M&M, P&L, Stocks<2% jaise headlines Telegram
        reject kar deta tha.
      - bug #5: seen-set ab ordered list (was set) - insertion-order
        preserved, trim `[-500:]` ab truly most-recent retains.
      - bug #14: thread-safety - entire function body lock-protected.
      - bug #17: short_link in <a href> ab quote-escaped (defense-in-depth).

    Args:
        send_func: callable(str) - Telegram par message bhejne wala
                   function (e.g. telegram_alerts.send_telegram_text)
        translate_to_hindi: Hindi translation on/off

    Return: int - kitni nayi headlines bheji gayin
    """
    # V8.2.0 FIX (bug #14): thread-safety - agar poller thread +
    # bot_listener manual call simultaneously ho, to seen-set/topic
    # state corrupt nahi hona chahiye (TOCTOU race).
    with _state_lock:
        seen = _load_seen()
        recent_topics = _load_recent_topics()
        all_items = get_latest_breaking_news()

        new_items = []
        for item in all_items:
            h = _headline_hash(item["link"], item["title"])
            if h in seen:
                continue  # Layer 1: exact duplicate (same link+title)

            if _is_duplicate_topic(item["title"], recent_topics):
                continue  # Layer 2: same topic, alag source/wording

            new_items.append((h, item))
            # Turant recent_topics mein bhi jod do (isi batch ke andar do
            # alag sources ka same-topic bhi pakadne ke liye - warna dono
            # is loop ke andar hi "new" lagenge kyunki topic-check sirf
            # PEHLE-se-saved topics se hota, is batch ke pehle-processed
            # items se nahi)
            recent_topics.append({
                "keywords": list(_extract_keywords(item["title"])),
                "proper_nouns": list(_extract_proper_nouns(item["title"])),
                "timestamp": time.time(),
            })

        if not new_items:
            return 0

        # Sabse purani pehle bhejo (natural chronological order lagta hai)
        new_items.reverse()

        sent_count = 0
        for h, item in new_items:
            title = item["title"]
            summary = item.get("summary", "")
            # category_tag aur source kabhi translate nahi hote - inhe
            # pehle se hi HTML-escape karo (defense-in-depth).
            category_tag = _detect_category_tag(item["title"], summary)  # original (English) text par detect karo, translate se pehle
            source = item.get("source", "")

            # V8.1.2: brief summary ab title ko bhi context ke roop mein
            # use karta hai (agar RSS summary khud chhota ho) - translate
            # se PEHLE nikalte hain taaki original English text par
            # accurate word-count/context mile
            brief = _get_brief_summary(summary, title=item["title"])

            # V8.3.0 (Task G5): Hinglish translation - stock names
            # ENGLISH mein preserve hote hain. `translate_to_hindi`
            # parameter naam backward-compat ke liye waise hi hai,
            # lekin ab actual translation Hinglish (ya Devanagari +
            # English stock names fallback) hoti hai, pure Devanagari
            # ki jagah. Common words Hinglish mein (Roman script),
            # stock names/tickers (RELIANCE, TCS, M&M, NIFTY) English.
            if translate_to_hindi:
                title = _translate_to_hinglish(title)
                if brief:
                    brief = _translate_to_hinglish(brief)

            # V8.2.0 FIX (bug #2): HAR dynamic string ko HTML-escape
            # karo BEFORE Telegram HTML message mein insert karo.
            # Pehle raw title/brief source se aa rahe the - M&M, P&L,
            # Stocks<2% jaise characters Telegram HTML parse fail kar
            # rahe the. to_hindi (deep-translator) unescapes entities,
            # isliye escape TRANSLATE KE BAAD karna zaroori hai.
            # V8.3.0: to_hinglish (GLM) bhi entities unescape kar sakta
            # hai - same defense-in-depth applies.
            title = html.escape(title, quote=False)
            brief = html.escape(brief, quote=False) if brief else ""
            category_tag_esc = html.escape(category_tag, quote=False)
            source_esc = html.escape(source, quote=False)

            # V8.1.2: link ab raw URL ki jagah clickable "Click Here" text
            # ke peeche hidden hai (HTML <a href> tag, Telegram parse_mode
            # HTML already hai) - user complaint tha ki raw link "irritate"
            # karta hai
            raw_link = item.get("link", "")
            short_link = _shorten_link(raw_link) if raw_link else ""

            # V8.2.0 FIX (bug #17): short_link in <a href="..."> attribute
            # mein jaata hai. Agar URL mein `&` (utm params) ya `"` ho to
            # HTML attribute break ho jaata tha. quote=True escape karta
            # hai `"` -> `&quot;` aur `&` -> `&amp;`.
            short_link_esc = html.escape(short_link, quote=True) if short_link else ""

            # V8.1.2 NAYA: stylish heading format
            #   [Category Tag]
            #   🚨 <bold headline>
            #   📝 brief news (~100 words, agar mili)
            #   🔗 Click Here for More Details (link hidden andar)
            text = f"🏷️ <b>{category_tag_esc}</b>  |  {source_esc}\n"
            text += f"🚨 <b>{title}</b>\n"
            if brief:
                text += f"📝 {brief}\n"
            if short_link_esc:
                text += f'<a href="{short_link_esc}">{LINK_DISPLAY_TEXT}</a>'

            try:
                send_func(text)
                # V8.2.0 FIX (bug #5): list mein append (not set.add)
                # so that insertion-order preserved for `[-500:]` trim.
                seen.append(h)
                sent_count += 1
                time.sleep(1.5)  # Telegram rate-limit ke liye chhota gap
            except Exception as e:
                logger.warning(f"Breaking news dispatch fail ({item.get('source','?')}): {e}")

        _save_seen(seen)
        _save_recent_topics(recent_topics)
        if sent_count:
            logger.info(f"📰 Breaking News: {sent_count} nayi headline(s) bheji gayin")

        return sent_count


# -----------------------------------------------------------
# BACKGROUND POLLING THREAD
# -----------------------------------------------------------
_polling_thread = None
_stop_flag = threading.Event()


def _poll_loop(send_func, translate_to_hindi):
    logger.info(
        f"📰 Breaking News poller shuru ho gaya (har {POLL_INTERVAL_SEC}s check karega, "
        f"sources: {', '.join(RSS_SOURCES.keys())})"
    )
    while not _stop_flag.is_set():
        try:
            check_and_dispatch_new_news(send_func, translate_to_hindi)
        except Exception as e:
            logger.error(f"Breaking news poll loop error (jaari rakhta hoon): {e}")

        # POLL_INTERVAL_SEC tak wait karo, lekin stop_flag ko turant
        # respond karne ke liye chhote chunks mein sleep karo
        for _ in range(POLL_INTERVAL_SEC):
            if _stop_flag.is_set():
                break
            time.sleep(1)


def start_breaking_news_poller(send_func=None, translate_to_hindi=True):
    """
    Background daemon thread mein breaking-news poller start karta hai.
    Non-blocking - turant return hota hai, poller peeche chalta rehta hai.

    Args:
        send_func: callable(str) - default telegram_alerts.send_telegram_text
        translate_to_hindi: Hindi translation on/off

    Ek hi baar call karo (main.py ya bot_listener.py ke start mein) -
    dobara call karne se pehla thread already running rahega (safe hai,
    duplicate thread nahi banega).
    """
    global _polling_thread

    if feedparser is None:
        logger.warning("Breaking news poller start nahi kar sakta - 'feedparser' install nahi hai.")
        return None

    if _polling_thread is not None and _polling_thread.is_alive():
        logger.info("Breaking news poller already chal raha hai, dobara start nahi kar raha.")
        return _polling_thread

    if send_func is None:
        from telegram_alerts import send_telegram_text
        send_func = send_telegram_text

    _stop_flag.clear()
    _polling_thread = threading.Thread(
        target=_poll_loop, args=(send_func, translate_to_hindi),
        daemon=True, name="BreakingNewsPoller",
    )
    _polling_thread.start()
    return _polling_thread


def stop_breaking_news_poller():
    """Poller ko gracefully stop karta hai (agla sleep-check cycle par rukega)."""
    _stop_flag.set()
