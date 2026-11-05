"""Issue internal overage invoices without contacting a payment processor."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from src.billing.contracts import BillingError
from src.billing.models import BillingPeriod
from src.billing.services import issue_overage


class Command(BaseCommand):
    help = "Close settled ended periods in BILLING_MODE and issue capped overage invoices."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= 1000 or settings.BILLING_MODE not in {"test", "live"}:
            raise CommandError("Use a limit of 1 to 1000 and a configured billing mode")
        candidates = list(
            BillingPeriod.objects.filter(
                subscription__account__mode=settings.BILLING_MODE,
                paid_at__isnull=False,
                closed_at__isnull=True,
                ends_at__lte=timezone.now(),
                reserved_tokens=0,
                reserved_calls=0,
            )
            .exclude(invocations__reservation_active=True)
            .order_by("ends_at", "id")
            .values_list("id", "subscription__account__owner_id")[:limit]
        )
        if options["dry_run"]:
            self.stdout.write(f"Eligible candidates: {len(candidates)}; no records changed")
            return
        issued = deferred = 0
        for period_id, owner_id in candidates:
            try:
                issue_overage(owner_id=owner_id, period_id=period_id)
            except BillingError as exc:
                deferred += 1
                self.stdout.write(f"Deferred {period_id}: {exc.code}")
            else:
                issued += 1
        self.stdout.write(f"Issued: {issued}; deferred: {deferred}")
