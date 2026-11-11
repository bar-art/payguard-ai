"""Deterministic rules. They decide hard limits; the AI review can only add risk on top."""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from app.schemas import AIReview, Decision, GuardResult, PurchaseIntent, RiskAssessment, RiskLevel, RiskSignal, StoredMandate

RULES_VERSION = '2026.11-2'
VELOCITY_PER_HOUR = 5

# Text aimed at an AI agent rather than a human shopper. A cheap first line of defence;
# the AI review looks for the same thing with more context.
INJECTION_PATTERNS = [
    r'ignore (all |any |the )?(previous|prior|above) (instructions|rules)',
    r'\b(ai|llm|shopping) (agent|assistant)s?\b.{0,80}\b(must|should|are required|are instructed)\b',
    r'\b(budget|spending) (limits?|caps?) (do not|don\'t|no longer) apply',
    r'^\s*(system|assistant)\s*:',
    r'\bdo not (tell|inform|ask) (the )?(user|owner|human)\b',
]


@dataclass
class History:
    committed: Decimal                # amount already held or captured under this mandate
    recent_count: int                 # purchases submitted under this mandate in the last hour


def check_rules(mandate: StoredMandate, intent: PurchaseIntent, history: History,
                now: datetime | None = None) -> tuple[list[RiskSignal], list[str]]:
    now = now or datetime.now(timezone.utc)
    hard: list[str] = []
    signals: list[RiskSignal] = []

    if mandate.expires_at and now > mandate.expires_at:
        hard.append('mandate_expired: the mandate has expired')
    if intent.currency != mandate.currency:
        hard.append(f'currency_mismatch: {intent.currency} is not the mandate currency {mandate.currency}')
    if intent.amount > mandate.max_per_purchase:
        hard.append(f'over_purchase_cap: {intent.amount} exceeds the per-purchase cap {mandate.max_per_purchase}')
    if history.committed + intent.amount > mandate.budget_total:
        left = max(Decimal('0'), mandate.budget_total - history.committed)
        hard.append(f'over_budget: only {left:.2f} of the {mandate.budget_total} budget is left')

    if mandate.allowed_merchants and intent.merchant not in mandate.allowed_merchants:
        signals.append(RiskSignal(rule_id='merchant_not_allowed', points=35,
                                  description=f'{intent.merchant} is not on the mandate merchant list'))
    if mandate.allowed_categories and intent.category not in mandate.allowed_categories:
        signals.append(RiskSignal(rule_id='category_not_allowed', points=30,
                                  description=f'category {intent.category} is not allowed by the mandate'))
    if intent.amount >= mandate.max_per_purchase * Decimal('0.8'):
        signals.append(RiskSignal(rule_id='near_purchase_cap', points=15,
                                  description=f'{intent.amount} is at least 80% of the per-purchase cap'))
    if history.recent_count >= VELOCITY_PER_HOUR:
        signals.append(RiskSignal(rule_id='velocity', points=25,
                                  description=f'{history.recent_count} purchases under this mandate in the last hour'))
    if len(intent.agent_rationale.strip()) < 10:
        signals.append(RiskSignal(rule_id='no_rationale', points=10,
                                  description='the agent gave no reason for this purchase'))
    if any(re.search(p, intent.page_excerpt, re.I | re.M) for p in INJECTION_PATTERNS):
        # The agent read text written to steer it; whatever it chose afterwards is not trusted.
        hard.append('injection_pattern: the merchant page contains instructions aimed at AI agents')
    return signals, hard


def level_for(score: int) -> RiskLevel:
    return RiskLevel.LOW if score < 30 else RiskLevel.MEDIUM if score < 70 else RiskLevel.HIGH


def combine(purchase_id: str, signals: list[RiskSignal], hard: list[str], ai: AIReview | None,
            ai_status: str, ai_model: str | None = None) -> GuardResult:
    """Merge rules and AI. The AI can raise the score or block; nothing it says lowers risk."""
    signals = list(signals)
    hard = list(hard)
    if ai is not None:
        if not ai.matches_mandate:
            signals.append(RiskSignal(rule_id='ai_mandate_mismatch', points=max(30, ai.risk_points),
                                      description='AI review: the purchase does not match the mandate'))
        elif ai.risk_points:
            signals.append(RiskSignal(rule_id='ai_concern', points=ai.risk_points,
                                      description='AI review raised concerns'))
        if ai.injection_suspected:
            hard.append('ai_prompt_injection: AI review found instructions aimed at the agent')
    elif ai_status == 'unavailable':
        # Fail closed: if the mandate could not be checked, a human has to look.
        signals.append(RiskSignal(rule_id='ai_unavailable', points=30,
                                  description='AI review failed, so a human must confirm the mandate'))
    score = min(100, sum(s.points for s in signals))
    assessment = RiskAssessment(order_id=purchase_id, score=score, level=level_for(score),
                                signals=signals, rules_version=RULES_VERSION)
    if hard:
        decision = Decision.BLOCK
    elif assessment.level == RiskLevel.LOW:
        decision = Decision.ALLOW
    else:
        decision = Decision.REVIEW
    return GuardResult(decision=decision, assessment=assessment, hard_blocks=hard,
                       ai_status=ai_status, ai_model=ai_model, ai=ai)


def recent_cutoff(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(hours=1)
