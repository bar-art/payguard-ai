from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Currency = Annotated[str, StringConstraints(pattern=r'^[A-Z]{3}$')]
Money = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
Slug = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=64)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class RiskLevel(str, Enum):
    LOW = 'low'
    MEDIUM = 'medium'
    HIGH = 'high'


class ReviewAction(str, Enum):
    APPROVE = 'approve'
    HOLD = 'hold'
    REJECT = 'reject'


class Decision(str, Enum):
    """What the guard does with an agent's purchase."""
    ALLOW = 'allow'    # authorize, then capture automatically
    REVIEW = 'review'  # authorize (funds held), a human captures or voids
    BLOCK = 'block'    # no PayPal order is created


class PurchaseStatus(str, Enum):
    BLOCKED = 'blocked'
    AWAITING_BUYER = 'awaiting_buyer'  # PayPal order created, buyer has not approved yet
    IN_REVIEW = 'in_review'            # authorized, funds held, waiting for a human
    CAPTURED = 'captured'
    VOIDED = 'voided'
    CANCELLED = 'cancelled'            # buyer cancelled at PayPal
    ERROR = 'error'


class PaymentOrder(StrictModel):
    order_id: str = Field(min_length=1, max_length=128)
    amount: Money
    currency: Currency = 'USD'
    status: str = Field(default='CREATED', min_length=1, max_length=64)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Mandate(StrictModel):
    """What a person allows their shopping agent to buy."""
    owner: str = Field(min_length=1, max_length=128)
    instruction: str = Field(min_length=1, max_length=1000)
    max_per_purchase: Money
    budget_total: Money
    currency: Currency = 'USD'
    allowed_categories: list[Slug] = Field(default_factory=list, max_length=20)
    allowed_merchants: list[Slug] = Field(default_factory=list, max_length=50)
    expires_at: datetime | None = None

    @model_validator(mode='after')
    def cap_within_budget(self):
        if self.max_per_purchase > self.budget_total:
            raise ValueError('max_per_purchase cannot exceed budget_total')
        return self


class StoredMandate(Mandate):
    mandate_id: str
    created_at: datetime


class PurchaseIntent(StrictModel):
    """A payment an agent wants to make. Every field comes from the agent and is untrusted."""
    mandate_id: str = Field(min_length=1, max_length=64)
    agent_id: str = Field(default='shopping-agent', min_length=1, max_length=64)
    merchant: Slug
    item: str = Field(min_length=1, max_length=200)
    category: Slug
    amount: Money
    currency: Currency = 'USD'
    agent_rationale: str = Field(default='', max_length=1000)
    page_excerpt: str = Field(default='', max_length=4000)


class RiskSignal(StrictModel):
    rule_id: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    points: int = Field(ge=0, le=100)


class RiskAssessment(StrictModel):
    order_id: str = Field(min_length=1, max_length=128)
    score: int = Field(ge=0, le=100)
    level: RiskLevel
    signals: list[RiskSignal] = Field(default_factory=list)
    rules_version: str = Field(default='v1', min_length=1)

    @model_validator(mode='after')
    def consistent_score(self):
        expected = RiskLevel.LOW if self.score < 30 else RiskLevel.MEDIUM if self.score < 70 else RiskLevel.HIGH
        if self.level != expected:
            raise ValueError('Risk level must match score: low <30, medium 30-69, high >=70')
        if self.score != min(100, sum(signal.points for signal in self.signals)):
            raise ValueError('Score must equal capped sum of signal points')
        return self


class AIReview(StrictModel):
    """The model's check of an intent against the mandate. It can add risk, never remove it."""
    matches_mandate: bool
    injection_suspected: bool
    risk_points: int = Field(ge=0, le=40)
    summary: str = Field(min_length=1, max_length=600)
    evidence: list[Annotated[str, StringConstraints(max_length=300)]] = Field(default_factory=list, max_length=5)
    recommendation: str = Field(min_length=1, max_length=300)


class GuardResult(StrictModel):
    decision: Decision
    assessment: RiskAssessment
    hard_blocks: list[str] = Field(default_factory=list)
    ai_status: str  # ok | unavailable | disabled
    ai_model: str | None = None
    ai: AIReview | None = None


class HumanReviewDecision(StrictModel):
    order_id: str = Field(min_length=1, max_length=128)
    reviewer: str = Field(min_length=1, max_length=128)
    action: ReviewAction
    reason: str = Field(min_length=1, max_length=1000)
    # Timestamp assigned by the application, never supplied by a dashboard user.


class ReviewRequest(StrictModel):
    reviewer: str = Field(min_length=1, max_length=128)
    action: ReviewAction
    reason: str = Field(min_length=1, max_length=1000)


class ShopRequest(StrictModel):
    mandate_id: str = Field(min_length=1, max_length=64)
    errand: str = Field(default='', max_length=500)
