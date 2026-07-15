"""
===========================================================
 CHATBOT INTENT CLASSIFIER (rule-based, free)
===========================================================
User jo bhi likhta hai (Hindi/English mixed), usse ek "intent"
pehchaanta hai (jaise TOP_PICKS, DAILY_REPORT, STOCK_ANALYSIS) -
keyword-matching se, koi paid AI API nahi lagti isliye.

Priority order zaroori hai: company-name detection (STOCK_ANALYSIS)
sabse LAST mein check hota hai, taaki "Tata Steel entry kab milegi"
jaisा sentence "entry" keyword se kisi generic intent mein na fas
jaaye - specific keyword-intents pehle check hote hain.

V8.2.0 FIX (Task F5): Word-boundary regex matching use karta hai
(pure substring ki jagah). Pehle "options" alias "menu" jaise
generic HELP keywords "show me my options" jais text ko bhi HELP
intent mein le jaate the (galat route). Ab `\b` use karke exact
word match karta hai. Saath hi, longer/more-specific keywords ko
priority milti hai - agar ek message mein multiple keywords mile
to longest matching keyword wala intent chuna jaata hai.
===========================================================
"""

import re

from company_lookup import find_symbol_by_name
from logger import logger

INTENTS = {
    "TOP_PICKS": ["top stock", "top pick", "top stocks", "aaj ke top", "aaj ka top"],
    "PDF_REPORT": ["pdf", "full report"],
    "WEEKLY_REPORT": ["weekly report", "hafte ka", "week ka", "weekly performance"],
    "MONTHLY_REPORT": ["monthly report", "mahine ka", "month ka", "monthly performance"],
    "TARGET_HIT": ["target hit", "target done", "kitne target"],
    "BEST_RR": ["risk reward", "best risk", "risk:reward", "r:r"],
    "ACTIVE_TRADES": ["active trade", "mere trade", "watchlist", "mera watchlist", "open position"],
    "NIFTY_TREND": ["nifty ka trend", "nifty trend", "market trend", "nifty kaisa", "market kaisa"],
    "DAILY_REPORT": ["daily report", "aaj ka report", "performance report", "win rate", "win-rate"],
    "MASTER_DASHBOARD": ["master dashboard", "dashboard dikhao", "dashboard bhejo"],
    # V8.2.0: "options" ko HELP keywords se hata diya - "options trading"
    # jaisa user intent galat HELP route mein jaata tha. "menu" rakha
    # hai kyunki Telegram /menu command genuine menu-request hai.
    "HELP": ["help", "madad", "kya kar sakte ho", "kya kar sakta hai", "menu"],
    # V9.0: Conversational intents - inke liye rule-based reply NAHI,
    # seedha AI Brain (GLM) call hota hai (handle_natural_language mein).
    # Common conversational questions ki GLM API cost isse save hoti
    # hai - UNKNOWN intent ki tarah inke liye bhi ask_ai() call hota
    # hai, lekin intent detect hone se GLM ko behtar context milta hai.
    "MARKET_VIEW": [
        "market kaisa lagega", "market kaisa", "bazaar kaisa",
        "market view", "market outlook", "kal kya", "aaj kya expect",
    ],
    "BEST_STOCK": [
        "best stock", "achha stock", "koi achha", "best pick",
        "top recommendation", "kya buy karu", "kya khareedu",
    ],
    "ENTRY_EXIT": [
        "entry kab", "exit kab", "entry timing", "kab enter",
        "kab exit", "entry level", "exit level",
    ],
}

# V8.2.0: pre-compile word-boundary regex for each keyword - faster + safer.
# Pure substring `kw in text` se "options" "show me my options" ko match
# karta tha (galat). `\bkw\b` word-boundary use karta hai.
# Cache compiled regexes taaki har call par re-compile na karna pade.
_COMPILED_KEYWORDS = {}  # intent -> list of compiled regex


def _compile_keywords():
    """V8.2.0: har keyword ke liye word-boundary regex pre-compile karta hai."""
    for intent, keywords in INTENTS.items():
        compiled = []
        for kw in keywords:
            # Hindi/Devanagari words ke liye \b kaam nahi karta (\w Unicode
            # aware hai but Python re default ASCII \w use karta hai).
            # English keywords ke liye \b use karo, Hindi keywords ke liye
            # plain substring fallback (koi ASCII boundary nahi milegi).
            if re.search(r"[a-zA-Z]", kw):
                # English / mixed - use word boundary
                try:
                    pattern = re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
                    compiled.append((kw, pattern, True))  # (kw, regex, use_word_boundary)
                except re.error:
                    compiled.append((kw, None, False))
            else:
                # Hindi-only - plain substring (no word boundary)
                compiled.append((kw, None, False))
        _COMPILED_KEYWORDS[intent] = compiled


_compile_keywords()


def parse_intent(text):
    """
    Return: (intent_name, symbol_or_none)
    intent_name ek string hai (jaise "TOP_PICKS", "STOCK_ANALYSIS", "UNKNOWN")
    symbol sirf STOCK_ANALYSIS ke liye set hota hai.

    V8.2.0: Word-boundary matching + longest-keyword-wins priority.
    Agar ek message mein multiple keywords mile (e.g. "weekly vs monthly
    report"), to longest matching keyword wala intent chunta hai - more
    specific intent wins over generic.
    """
    if not isinstance(text, str) or not text.strip():
        return "UNKNOWN", None

    text_lower = text.lower().strip()

    # V8.2.0: collect all matching (intent, keyword_length) - longest
    # keyword wala intent wins (more specific). Multiple matches ke
    # case mein disambiguation milta hai.
    matches = []  # list of (intent, keyword_length, keyword)

    for intent, compiled_list in _COMPILED_KEYWORDS.items():
        for kw, pattern, use_word_boundary in compiled_list:
            matched = False
            if use_word_boundary and pattern is not None:
                if pattern.search(text_lower):
                    matched = True
            else:
                # Hindi keywords - plain substring match
                if kw in text_lower:
                    matched = True
            if matched:
                matches.append((intent, len(kw), kw))

    if matches:
        # V8.2.0: longest keyword wala intent chuno - more specific wins.
        # "monthly report" (14 chars) vs "weekly report" (13 chars) - agar
        # dono same message mein hain to "monthly report" wins.
        # Tie-breaking: pehla match (dict order = original priority).
        matches.sort(key=lambda m: -m[1])  # descending by keyword length
        best_intent = matches[0][0]
        return best_intent, None

    # Koi generic keyword match nahi hua - dekho ki koi company
    # name/symbol mention hua hai kya (isse STOCK_ANALYSIS milta hai)
    try:
        symbol = find_symbol_by_name(text)
    except Exception as e:
        logger.debug(f"find_symbol_by_name error: {e}")
        symbol = None
    if symbol:
        return "STOCK_ANALYSIS", symbol

    return "UNKNOWN", None
