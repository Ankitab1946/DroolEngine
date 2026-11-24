from typing import Literal
from decimal import Decimal, InvalidOperation
from pydantic import BaseModel, Field, model_validator

RULE_TYPES = ['MANDATORY','LIMIT','MOVEMENT','DUPLICATE','DATA_TYPE','ENUMERATION','PATTERN','COMPARISON','RECONCILIATION']
OPS = {'>','>=','<','<=','=','!=','BETWEEN','NOT_BETWEEN','IS_NULL','IS_NOT_NULL','IN','NOT_IN'}

class RuleInput(BaseModel):
    id: str = Field(min_length=2, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    name: str = Field(min_length=2, max_length=200)
    rule_type: str
    targets: list[str] = Field(min_length=1)
    portfolios: list[str] = Field(default_factory=list)
    config: dict = Field(default_factory=dict)
    # Optional top-level values also available in config for backward compatibility.
    threshold_value: Decimal | None = None
    threshold_type: Literal['PERCENTAGE', 'COUNT', 'AMOUNT'] | None = None
    changed_by: str = Field(default='ui', max_length=120)
    change_reason: str = Field(default='Rule updated', max_length=255)
    severity: Literal['LOW','MEDIUM','HIGH','CRITICAL'] = 'MEDIUM'
    status: Literal['DRAFT','ACTIVE'] = 'DRAFT'
    @model_validator(mode='after')
    def validate_rule(self):
        if self.rule_type not in RULE_TYPES: raise ValueError('Unsupported rule type')
        if self.rule_type in ('LIMIT','MOVEMENT','COMPARISON','RECONCILIATION'):
            if self.config.get('operator') not in OPS: raise ValueError('Valid operator required')
        configured = (self.threshold_value if self.threshold_value is not None else
                      self.config.get('threshold_value', self.config.get('threshold')))
        if self.rule_type in ('LIMIT','MOVEMENT') and configured is None:
            raise ValueError('threshold_value is required for LIMIT/MOVEMENT')
        if configured is not None:
            try: number = Decimal(str(configured))
            except (ValueError, InvalidOperation, TypeError): raise ValueError('threshold_value must be numeric')
            if not number.is_finite(): raise ValueError('threshold_value must be finite')
            self.config['threshold_value'] = str(number)
            self.config['threshold'] = str(number)  # older consumers/handlers remain compatible
            self.threshold_value = number
        t = (self.config.get('threshold_type') or self.threshold_type or
             ('PERCENTAGE' if self.rule_type == 'MOVEMENT' else 'AMOUNT')).upper()
        if t not in ('PERCENTAGE', 'COUNT', 'AMOUNT'):
            raise ValueError('threshold_type must be PERCENTAGE, COUNT or AMOUNT')
        if self.rule_type == 'MOVEMENT' and t != 'PERCENTAGE':
            raise ValueError('Movement requires PERCENTAGE threshold_type')
        if self.rule_type == 'LIMIT' and t == 'COUNT':
            raise ValueError('COUNT thresholds apply to DUPLICATE rules, not LIMIT financial values')
        if self.rule_type == 'DUPLICATE' and self.threshold_value is None and configured is None:
            self.config.update(threshold_value='1', threshold='1')
            self.threshold_value = Decimal(1)
            t = 'COUNT'
        if self.rule_type == 'DUPLICATE' and t != 'COUNT':
            raise ValueError('DUPLICATE requires COUNT threshold_type')
        self.threshold_type = t
        self.config['threshold_type'] = t
        if self.rule_type in ('COMPARISON','RECONCILIATION') and not self.config.get('other_prj_id'):
            raise ValueError('other_prj_id required for multi-attribute rule')
        if self.rule_type == 'ENUMERATION' and not isinstance(self.config.get('allowed'), list):
            raise ValueError('allowed list required')
        if self.rule_type == 'PATTERN':
            import re
            pattern = self.config.get('pattern','')
            if len(pattern)>150: raise ValueError('Pattern too long')
            re.compile(pattern)
        return self

class ExecutionInput(BaseModel):
    source: Literal['DEMO_NORMALIZED', 'UNS_FINANCIAL_LINE_ITEMS'] = 'DEMO_NORMALIZED'
    cde_only: bool = False
    rule_ids: list[str] = Field(default_factory=list)
    portfolio: str | None = None
    company: str | None = None
    document_id: str | None = None
    period: str | None = None

class ScheduleInput(BaseModel):
    name: str
    frequency: Literal['DAILY','MONTHLY']
    hour_utc: int = Field(default=0, ge=0, le=23)
    minute_utc: int = Field(default=0, ge=0, le=59)
    day_of_month: int = Field(default=1, ge=1, le=28)
    rule_ids: list[str] = Field(default_factory=list)
    active: bool = True
