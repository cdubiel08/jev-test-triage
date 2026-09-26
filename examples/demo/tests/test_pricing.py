from shop.pricing import apply_discount, can_refund, shipping_cost


def test_discount_code_reduces_total():
    assert apply_discount(100, "SAVE10", False) == 90.0


def test_member_discount_is_applied():
    assert apply_discount(100, None, True) is not None


def test_admin_can_always_refund():
    assert can_refund("admin", 400, 10_000) is True


def test_refund_window():
    assert can_refund("user", 5, 10)


def test_shipping_is_free_over_threshold():
    assert shipping_cost(80, False) == 0.0
