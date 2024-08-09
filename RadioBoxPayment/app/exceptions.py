class RequestValidationError(Exception):
    """Exception raised for errors in the input request."""

    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class LimitReachedError(Exception):
    """Exception raised when a limit is reached."""

    def __init__(self, message):
        self.message = message
        super().__init__(self.message)
