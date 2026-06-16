from app.workers.refund_worker import process_refund


def test_process_refund_marks_payment_as_queued():
    assert process_refund("pay_demo_123") == {
        "payment_id": "pay_demo_123",
        "status": "refund_queued",
    }
