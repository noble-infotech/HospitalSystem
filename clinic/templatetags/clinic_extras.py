"""Template filters for money: Indian digit grouping (1,23,456) and amounts in words."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

register = template.Library()


def _group_indian(n):
    """1234567 → '12,34,567'."""
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


@register.filter
def inr(value):
    """Format as ₹1,23,456 (paise shown only when there are any)."""
    try:
        amount = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return value
    sign = "−" if amount < 0 else ""
    amount = abs(amount)
    rupees, paise = divmod(int(amount * 100), 100)
    text = _group_indian(rupees)
    if paise:
        text += f".{paise:02d}"
    return f"{sign}₹{text}"


ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
        "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_hundred(n):
    return ONES[n] if n < 20 else (TENS[n // 10] + (" " + ONES[n % 10] if n % 10 else ""))


def _words(n):
    if n == 0:
        return "Zero"
    parts = []
    for size, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand"), (100, "Hundred")):
        if n >= size:
            parts.append(f"{_words(n // size)} {name}")
            n %= size
    if n:
        parts.append(_below_hundred(n))
    return " ".join(parts)


@register.filter
def amount_in_words(value):
    """5250.50 → 'Rupees Five Thousand Two Hundred Fifty and Fifty Paise Only'."""
    try:
        amount = abs(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    rupees, paise = divmod(int(amount * 100), 100)
    text = f"Rupees {_words(rupees)}"
    if paise:
        text += f" and {_below_hundred(paise)} Paise"
    return text + " Only"
