from typing import Literal, Optional
from pydantic import BaseModel, field_validator


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


class PipelineOption(BaseModel):
    id: Optional[str] = None
    label: str
    exit_message: Optional[str] = None
    exit_follow_up: Optional[str] = None


class PipelineRule(BaseModel):
    id: Optional[str] = None
    op: Literal["equals", "not_equals", "contains", "any_of", "regex"]
    value: Optional[str | list[str]] = None
    next: str


class PipelineQuestion(BaseModel):
    id: str
    text: str
    type: Literal["text", "radio", "select", "checkbox"]
    required: bool = False
    options: Optional[list[PipelineOption]] = None
    context: Optional[str] = None
    rules: Optional[list[PipelineRule]] = None
    default_next: Optional[str] = None

    @field_validator("options")
    @classmethod
    def validate_options_for_type(cls, v, info):
        field_type = info.data.get("type")
        if field_type in ("radio", "select", "checkbox") and not v:
            raise ValueError(f"options are required for {field_type} question type")
        return v


class PipelineConfig(BaseModel):
    questions: list[PipelineQuestion]
    greeting_message: str
    completion_message: str
    thank_you_message: str
    run_mode: Literal["every_session", "once_per_user"] = "every_session"
    node_positions: Optional[dict[str, dict]] = None
    edge_handles: Optional[dict[str, dict]] = None

    @field_validator("questions")
    @classmethod
    def validate_unique_question_ids(cls, v):
        ids = [q.id for q in v]
        if len(ids) != len(set(ids)):
            raise ValueError("question ids must be unique within the flow")
        return v


class FlowConfigUpdate(BaseModel):
    config: PipelineConfig


class BotSelectRequest(BaseModel):
    bot: str  # "information_bot" or "sales_bot"


class SectionToggleRequest(BaseModel):
    section: str
    enabled: bool


class KnowledgeDocument(BaseModel):
    id: str
    name: str
    size: int = 0
    character_count: int = 0
    status: str = "pending"
    created_at: Optional[str] = None


class KnowledgeFilesResponse(BaseModel):
    files: list[KnowledgeDocument] = []


class UploadKnowledgeResponse(BaseModel):
    message: str
    files: list[KnowledgeDocument] = []


class DeleteKnowledgeResponse(BaseModel):
    message: str


class ProductServiceItem(BaseModel):
    id: str
    kind: Literal["product", "service"]
    name: str = ""
    short_description: str = ""
    full_description: str = ""
    category: str = ""
    price: str = ""
    discount_price: str = ""
    status: str = "Active"
    images: str = ""
    rating: str = ""
    reviews_count: str = ""
    purchased_count: str = ""
    source: Literal["manual", "excel"] = "manual"
    vectorization_status: str = "processing"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ProductServiceFields(BaseModel):
    name: str = ""
    short_description: str = ""
    full_description: str = ""
    category: str = ""
    price: str = ""
    discount_price: str = ""
    status: str = "Active"
    images: str = ""
    rating: str = ""
    reviews_count: str = ""
    purchased_count: str = ""


class ProductServiceCreate(ProductServiceFields):
    kind: Literal["product", "service"]


class ProductServiceUpdate(ProductServiceFields):
    pass


class ProductsServicesListResponse(BaseModel):
    items: list[ProductServiceItem] = []


class ProductServiceSaveResponse(BaseModel):
    item: ProductServiceItem


class ProductsServicesUploadStartResponse(BaseModel):
    job_id: str
    total: int


class ProductsServicesUploadStatusResponse(BaseModel):
    status: Literal["processing", "done", "failed"]
    total: int
    processed: int
    items: list[ProductServiceItem] = []
    error: Optional[str] = None


class SchedulerItem(BaseModel):
    id: str
    day_of_week: str
    time_start: str
    time_end: str
    exclude_time_start: str = ""
    exclude_time_end: str = ""
    is_special_time: bool = False
    special_date: str = ""
    source: Literal["manual"] = "manual"
    vectorization_status: str = "processing"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SchedulerCreate(BaseModel):
    day_of_week: str
    time_start: str
    time_end: str
    exclude_time_start: str = ""
    exclude_time_end: str = ""
    is_special_time: bool = False
    special_date: str = ""


class SchedulerUpdate(BaseModel):
    day_of_week: str
    time_start: str
    time_end: str
    exclude_time_start: str = ""
    exclude_time_end: str = ""
    is_special_time: bool = False
    special_date: str = ""


class SchedulerListResponse(BaseModel):
    items: list[SchedulerItem] = []


class SchedulerSaveResponse(BaseModel):
    item: SchedulerItem


class EnhancedRetrievalUpdate(BaseModel):
    enabled: bool


class ConversationHistoryUpdate(BaseModel):
    enabled: bool


# ── Sales Bot ────────────────────────────────────────────────────────────────


class SalesProductItem(BaseModel):
    id: str
    retailer_id: str = ""
    name: str = ""
    description: str = ""
    category: str = ""
    price_minor: int = 0
    currency: str = "INR"
    image_url: str = ""
    stock_quantity: Optional[int] = None
    is_active: bool = True
    meta_catalog_id: Optional[str] = None
    sync_status: str = "pending"
    sync_error: str = ""
    source: Literal["manual", "excel"] = "manual"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SalesProductCreate(BaseModel):
    name: str = ""
    description: str = ""
    category: str = ""
    price_minor: int = 0
    currency: str = "INR"
    image_url: str = ""
    stock_quantity: Optional[int] = None
    is_active: bool = True
    retailer_id: Optional[str] = None  # auto-generated when omitted


class SalesProductUpdate(BaseModel):
    name: str = ""
    description: str = ""
    category: str = ""
    price_minor: int = 0
    currency: str = "INR"
    image_url: str = ""
    stock_quantity: Optional[int] = None
    is_active: bool = True


class SalesProductsListResponse(BaseModel):
    items: list[SalesProductItem] = []


class SalesProductSaveResponse(BaseModel):
    item: SalesProductItem


class SalesProductsUploadStartResponse(BaseModel):
    job_id: str
    total: int


class SalesProductsUploadStatusResponse(BaseModel):
    status: Literal["processing", "done", "failed"]
    total: int
    processed: int
    items: list[SalesProductItem] = []
    error: Optional[str] = None


class SalesCatalogSyncResponse(BaseModel):
    synced: int = 0
    failed: int = 0
    items: list[SalesProductItem] = []


class SalesPaymentSettingsUpdate(BaseModel):
    # payment_enabled is toggled via the sidebar section switch (/settings/sections),
    # so it is intentionally not part of this currency/shipping update.
    default_currency: str = "INR"
    flat_shipping_minor: int = 0
    free_shipping_threshold_minor: Optional[int] = None


class SalesPaymentSettingsResponse(BaseModel):
    payment_enabled: bool = False
    default_currency: str = "INR"
    flat_shipping_minor: int = 0
    free_shipping_threshold_minor: Optional[int] = None
    razorpay_configured: bool = False
    catalog_configured: bool = False
    checkout_flow_configured: bool = False


class SalesOrderItemOut(BaseModel):
    name: str = ""
    retailer_id: str = ""
    quantity: int = 1
    unit_price_minor: int = 0
    line_total_minor: int = 0


class SalesOrderOut(BaseModel):
    id: str
    order_number: str
    sender: str
    status: str
    subtotal_minor: int = 0
    shipping_minor: int = 0
    total_minor: int = 0
    currency: str = "INR"
    customer_name: str = ""
    customer_phone: str = ""
    shipping_address: dict = {}
    payment_status: str = "created"
    created_at: Optional[str] = None
    items: list[SalesOrderItemOut] = []


class SalesOrdersListResponse(BaseModel):
    orders: list[SalesOrderOut] = []


class SettingsResponse(BaseModel):
    active_bot: str = "information_bot"
    bot_name: str | None = None
    greeting: str | None = None
    main_instruction: str | None = None
    dos: str | None = None
    donts: str | None = None
    company_address: str | None = None
    company_phone: str | None = None
    company_email: str | None = None
    social_handles: list[dict] = []
    flow_builder: Optional[PipelineConfig] = None
    section_states: dict = {}
    enhanced_retrieval_enabled: bool = False
    conversation_history_enabled: bool = True
    setup_completed: bool = False
