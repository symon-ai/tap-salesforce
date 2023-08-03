# pylint: disable=super-init-not-called

class TapSalesforceException(Exception):
    pass

class TapSalesforceQuotaExceededException(TapSalesforceException):
    pass

class TapSalesforceBulkAPIDisabledException(TapSalesforceException):
    pass

# used for Symon import error handling
class SymonException(Exception):
    def __init__(self, message, code, details=None):
        super().__init__(message)
        self.code = code
        self.details = details
