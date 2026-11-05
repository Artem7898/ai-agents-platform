"""Public billing errors and bounds; no provider payloads belong in diagnostics."""

MAX_MINOR = 10**12


class BillingError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__("Billing operation could not be completed")
