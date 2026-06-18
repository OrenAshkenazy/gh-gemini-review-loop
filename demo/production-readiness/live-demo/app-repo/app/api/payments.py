def capture_payment(payment_id: str) -> dict[str, str]:
    return {"payment_id": payment_id, "status": "captured"}
