from decimal import Context, Decimal

# Enough digits for any uint256, so scaling never rounds.
_EXACT = Context(prec=100)


def scale_amount(raw: int, decimals: int) -> Decimal:
    """Move the decimal point of an integer amount, exactly and without trailing zeros."""
    return _tidy(Decimal(raw).scaleb(-decimals, context=_EXACT))


def _tidy(value: Decimal) -> Decimal:
    value = value.normalize(_EXACT)
    # normalize() turns 100 into 1E+2, keep plain integers plain
    if value.as_tuple().exponent > 0:  # type: ignore[operator]
        return value.quantize(Decimal(1), context=_EXACT)
    return value
