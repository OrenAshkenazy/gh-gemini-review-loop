def process_refund(payment_id: str) -> dict[str, str]:
    return {"payment_id": payment_id, "status": "refund_queued"}
