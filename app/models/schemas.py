from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class SendMessageRequest(BaseModel):
    sender: str
    message: str


class RenameClientRequest(BaseModel):
    sender: str
    name: str


class SenderActionRequest(BaseModel):
    sender: str


class ToggleClientRequest(BaseModel):
    sender: str
    bookmarked: bool | None = None
    blocked: bool | None = None
