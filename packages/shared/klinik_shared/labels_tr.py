"""Turkish display labels. Business logic uses the codes, never these strings."""

from __future__ import annotations

from klinik_shared.enums import AppointmentStatus, NotificationEvent, PaymentMethod

STATUS_LABELS: dict[str, str] = {
    AppointmentStatus.PLANNED: "Planlandı",
    AppointmentStatus.COMPLETED: "Tamamlandı",
    AppointmentStatus.CANCELLED: "İptal",
    AppointmentStatus.NO_SHOW: "Gelmedi",
}

PAYMENT_METHOD_LABELS: dict[str, str] = {
    PaymentMethod.CASH: "Nakit",
    PaymentMethod.CARD: "Kart",
    PaymentMethod.TRANSFER: "Havale / EFT",
}

NOTIFICATION_TITLES: dict[str, str] = {
    NotificationEvent.APPOINTMENT_CREATED: "Yeni randevu",
    NotificationEvent.APPOINTMENT_UPDATED: "Randevu değişti",
    NotificationEvent.APPOINTMENT_CANCELLED: "Randevu iptal edildi",
    NotificationEvent.REMINDER_24H: "Yarın randevu var",
    NotificationEvent.REMINDER_1H: "1 saat sonra randevu",
    NotificationEvent.CONFLICT_CREATED: "Çakışma: karar gerekiyor",
}


def label(table: dict[str, str], code: str) -> str:
    """Label for a code; falls back to the code itself so nothing renders blank."""
    return table.get(code, code)
