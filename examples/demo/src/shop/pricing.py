"""A small pricing module used to demonstrate jev-test-triage."""

import logging

__all__ = ["apply_discount", "can_refund", "shipping_cost"]

log = logging.getLogger(__name__)

FREE_SHIPPING_OVER = 50.0
REQUEST_TIMEOUT_S = 10


def apply_discount(total: float, code: str | None, is_member: bool) -> float:
    """Return the total after a discount code and a member discount."""
    if total <= 0:
        return 0.0
    if code == "SAVE10":
        total = total * 0.9
    if is_member and total > 20:
        total = total - 5
    log.info("discounted total %s", total)
    return round(total, 2)


def can_refund(role: str, days_since_purchase: int, amount: float) -> bool:
    """Admins may always refund; others within 30 days and under 500."""
    if role == "admin":
        return True
    return days_since_purchase <= 30 and amount < 500


def shipping_cost(total: float, express: bool) -> float:
    if total >= FREE_SHIPPING_OVER and not express:
        return 0.0
    return 15.0 if express else 5.0
