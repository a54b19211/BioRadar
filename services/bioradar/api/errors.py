from dataclasses import dataclass


@dataclass
class DomainError(Exception):
    code: str
    message: str
    status_code: int = 503
    retryable: bool = False

    def __str__(self) -> str:
        return self.message
