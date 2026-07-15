"""
===========================================================
 TARGET CALCULATION (single source of truth)
===========================================================
Entry aur Stoploss (scanner.py se, ATR-based) ke upar Fibonacci
extension se Target 1 / Target 2 / Final Target nikalta hai.

IMPORTANT: Ye function tracker.py, charts.py, report.py, aur
telegram messages - SABME use hota hai, taaki chart par dikha
target, Telegram message ka target, aur database mein save hua
target - teeno HAMESHA same rahein. Pehle ye teeno jagah alag-alag
calculate ho rahe the (bug) - ab sab yahi ek function call karte hain.
===========================================================
"""

import math


def _is_invalid(x):
    """
    None ya NaN check karne ka helper. Pandas/numpy floats NaN ho
    sakte hain (e.g., ATR NaN tha to derived entry/stoploss bhi NaN),
    aur `NaN <= 0` False return karta hai - isliye simple `<= 0`
    check se NaN slip ho jaata tha. Ye helper sab cases cover karta hai.
    """
    if x is None:
        return True
    try:
        return math.isnan(x)
    except (TypeError, ValueError):
        return True


def calculate_targets(entry, stoploss):
    """
    entry, stoploss: scanner.py ke ATR-based Entry/Stoploss (row['Entry'], row['Stoploss'])
    Return: (target_1, target_2, final_target) - teeno floats, 2 decimal rounded
            ya (None, None, None) agar inputs invalid / NaN / non-positive risk

    Risk (R) = entry - stoploss
    Target 1 = entry + 1R   (conservative, jaldi book kar sakte ho)
    Target 2 = entry + 2R
    Final Target = entry + 3R  (poora move, trailing SL ke saath hold)
    """
    # V8.2.0 BUGFIX: NaN guard add kiya. Pehle sirf `entry is None`
    # check tha - agar entry NaN (float) tha to `risk = NaN - stoploss`
    # = NaN, phir `risk <= 0` False (NaN comparisons), aur NaN targets
    # return ho jaate the. Ab NaN/None dono handle hote hain.
    if _is_invalid(entry) or _is_invalid(stoploss):
        return None, None, None

    risk = entry - stoploss
    if risk <= 0:
        # invalid setup (stoploss entry se upar hai) - safe default
        return None, None, None

    target_1 = round(entry + risk * 1.0, 2)
    target_2 = round(entry + risk * 2.0, 2)
    final_target = round(entry + risk * 3.0, 2)

    return target_1, target_2, final_target


def calculate_entry_zone(entry, atr, fraction=None):
    """
    Single entry price ki jagah ek REALISTIC ZONE deta hai (jaise
    professional desks "Entry Zone: 2940-2955" dete hain, sirf ek
    exact number nahi - kyunki market mein exact price par fill
    milna guaranteed nahi hota).

    Zone width = entry +/- (ATR * fraction). fraction chhota (0.2-0.4)
    rakhte hain taaki zone tight rahe, bahut wide na ho jaaye.

    Return: (entry_low, entry_high) - dono floats, 2 decimal rounded
            ya (None, None) agar inputs invalid / NaN / non-positive
    """
    # V8.2.0 BUGFIX: NaN guard add kiya (calculate_targets jaisa).
    if _is_invalid(entry) or _is_invalid(atr) or atr <= 0:
        return None, None

    if fraction is None:
        try:
            from config import ENTRY_ZONE_ATR_FRACTION
            fraction = ENTRY_ZONE_ATR_FRACTION
        except ImportError:
            fraction = 0.3

    half_width = atr * fraction
    entry_low = round(entry - half_width, 2)
    entry_high = round(entry + half_width, 2)
    return entry_low, entry_high
