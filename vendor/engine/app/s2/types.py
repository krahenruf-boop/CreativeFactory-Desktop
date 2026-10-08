from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid', allow_inf_nan=False)

class Rate(Strict):
    currency: str = Field(default='USD', pattern='^[A-Z]{3}$')
    tts_per_1000_chars: float | None = Field(default=None, ge=0)
    input_per_million: float | None = Field(default=None, ge=0)
    cached_input_per_million: float | None = Field(default=None, ge=0)
    output_per_million: float | None = Field(default=None, ge=0)
    model_id: str = ''
    source: str = Field(default='',max_length=500)
    verified_at: str = Field(default='',max_length=80)
    @model_validator(mode='after')
    def provenance(self):
        if any(v is not None for v in [self.tts_per_1000_chars,self.input_per_million,self.cached_input_per_million,self.output_per_million]) and not (self.model_id and self.source and self.verified_at):
            raise ValueError('配置费率时须填写模型、依据和核对日期')
        return self

class Budget(Strict):
    request_chars: int=Field(default=5000,ge=1,le=20000)
    batch_chars: int=Field(default=15000,ge=1,le=60000)
    daily_chars: int=Field(default=30000,ge=1,le=1000000)
    daily_requests: int=Field(default=30,ge=1,le=10000)
    request_amount: float | None=Field(default=None,gt=0)
    batch_amount: float | None=Field(default=None,gt=0)
    daily_amount: float | None=Field(default=None,gt=0)
    timezone: str='Asia/Shanghai'

class ProviderConfig(Strict):
    provider: Literal['elevenlabs','deepseek','fake']
    enabled: bool=False
    account_scope: str=Field(default='',max_length=120)
    permission_scope: str=Field(default='default',min_length=1,max_length=120)
    key: SecretStr | None=None
    clear_key: bool=False
    rate: Rate=Field(default_factory=Rate)
    budget: Budget=Field(default_factory=Budget)

class TTS(Strict):
    text: str=Field(min_length=1,max_length=20000)
    voice_id: str=Field(min_length=1,max_length=150,pattern=r'^[\w-]+$')
    model_id: str=Field(min_length=1,max_length=150,pattern=r'^[\w.-]+$')
    settings: dict[str,float|bool]=Field(default_factory=dict)
    output_format: Literal['mp3_44100_128']='mp3_44100_128'
    language: str=''
    seed: int | None=Field(default=None,ge=0,le=4294967295)
    previous_text: str=Field(default='',max_length=2000)
    next_text: str=Field(default='',max_length=2000)
    script_id: str | None=None
    revision: int | None=None
    @model_validator(mode='after')
    def nonblank(self):
        if not self.text.strip(): raise ValueError('文案不能为空')
        return self

class Batch(Strict):
    provider: Literal['elevenlabs','fake']='elevenlabs'
    items: list[TTS]=Field(min_length=1,max_length=3)
    task_id: str | None=None
    regenerate: bool=False

class Confirm(Strict):
    confirmed: bool
    accept_unknown_cost: bool=False

class Localize(Strict):
    provider: Literal['deepseek','fake']='deepseek'
    model_id: str=Field(min_length=1,max_length=150)
    script_id: str
    market: str=Field(min_length=1,max_length=80)
    language: str=Field(min_length=1,max_length=80)
    facts: str=Field(default='',max_length=3000)
    forbidden: str=Field(default='',max_length=2000)
    style: str=Field(default='',max_length=1000)
    reference_seconds: float | None=Field(default=None,gt=0,le=300)

class LocalizedResult(Strict):
    localized_text: str=Field(min_length=1,max_length=20000)
    review_notes: list[str]=Field(default_factory=list,max_length=30)
    source_claims: list[str]=Field(default_factory=list,max_length=30)
    changed_or_removed_claims: list[str]=Field(default_factory=list,max_length=30)
    duration_estimate: float | None=Field(default=None,ge=0,le=10000)
