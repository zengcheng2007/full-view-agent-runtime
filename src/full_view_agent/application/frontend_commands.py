from full_view_agent.application.errors import CommandReceiptConflict
from full_view_agent.domain.models import FrontendCommandReceipt


def merge_receipt(
    existing: FrontendCommandReceipt | None,
    incoming: FrontendCommandReceipt,
) -> FrontendCommandReceipt:
    if existing is None:
        return incoming
    if existing == incoming:
        return existing
    if existing.status != "accepted":
        raise CommandReceiptConflict("terminal command receipt cannot be changed")
    if incoming.status == "accepted":
        raise CommandReceiptConflict("accepted command receipt payload cannot be changed")
    return incoming
