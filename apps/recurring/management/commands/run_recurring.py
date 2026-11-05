from django.core.management.base import BaseCommand

from apps.recurring.services import run_due, run_due_expenses


class Command(BaseCommand):
    help = "Create all due recurring invoices/bills for every organization. Schedule this daily (cron / task scheduler)."

    def handle(self, *args, **opts):
        total = 0
        for pid, docs in run_due().items():
            total += len(docs)
        expenses = sum(len(x) for x in run_due_expenses().values())
        self.stdout.write(f"Created {total} document(s) and {expenses} expense(s).")
