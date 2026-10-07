"""Reference data every database needs: reviewer identities and the chart of accounts.

Fictional (ASM-03). Shared by `init` and the evaluator, which builds its own database
for each measured run so it never touches the working one.
"""

from __future__ import annotations

from app.config import Config
from app.infra.audit import utc_now
from app.infra.db import Database
from app.infra.repositories import Repositories

STAFF = [
    ("Maya Castillo", "ROL-01"),
    ("Ethan Brooks", "ROL-01"),
    ("Priya Raman", "ROL-02"),
    ("Daniel Okafor", "ROL-03"),
]

GL_ACCOUNTS = [
    ("1010", "Cash - Operating", "asset"),
    ("1210", "Accounts Receivable", "asset"),
    ("2010", "Accounts Payable", "liability"),
    ("6810", "Bank Service Charges", "expense"),
    ("6820", "Returned Item Charges", "expense"),
    ("7010", "Interest Income", "income"),
    ("9990", "Suspense - Under Investigation", "asset"),
]


def seed(database: Database, repositories: Repositories, config: Config) -> dict[str, int]:
    """Create the identities, accounts and bank account. Returns user ids by name."""
    users: dict[str, int] = {}
    with database.transaction() as connection:
        now = utc_now()
        for full_name, role in STAFF:
            users[full_name] = repositories.users.create(connection, full_name, role, now)
        repositories.accounts.create_gl_accounts(connection, GL_ACCOUNTS)
        repositories.accounts.create_bank_account(
            connection, bank_name=config.organization.bank_name,
            account_label=config.organization.account_label,
            account_mask=config.organization.account_mask,
            gl_account_code=config.organization.gl_account_code,
            currency_code=config.organization.currency)
    return users
