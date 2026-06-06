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


class UpdateClientLabelRequest(BaseModel):
    sender: str
    lead_label: str


class ToggleClientAiRequest(BaseModel):
    sender: str
    ai_disabled: bool | None = None


class SenderActionRequest(BaseModel):
    sender: str


class ToggleClientRequest(BaseModel):
    sender: str
    bookmarked: bool | None = None
    blocked: bool | None = None


class BotIdentityUpdate(BaseModel):
    bot_name: str
    greeting: str


class InstructionsUpdate(BaseModel):
    main_instruction: str
    dos: str
    donts: str


class SocialHandle(BaseModel):
    platform: str
    url: str


class CompanyInfoUpdate(BaseModel):
    company_address: str
    company_phone: str
    company_email: str
    social_handles: list[SocialHandle] = []


class FlowConfigUpdate(BaseModel):
    config: dict


class SettingsResponse(BaseModel):
    bot_name: str | None = None
    greeting: str | None = None
    main_instruction: str | None = None
    dos: str | None = None
    donts: str | None = None
    company_address: str | None = None
    company_phone: str | None = None
    company_email: str | None = None
    social_handles: list[dict] = []
    flow_builder: dict | None = None
    setup_completed: bool = False
