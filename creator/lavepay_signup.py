"""Public LavePay Mini App signup link used in connection instructions."""
from __future__ import annotations

import html

LAVEPAY_SIGNUP_URL = "https://t.me/laveshka?startapp=ref_p8lzxfvu"


def lavepay_signup_link(label: str) -> str:
    """HTML anchor: the referral URL is hidden in the visible word."""
    return (
        f'<a href="{html.escape(LAVEPAY_SIGNUP_URL, quote=True)}">'
        f"{html.escape(label)}</a>"
    )
