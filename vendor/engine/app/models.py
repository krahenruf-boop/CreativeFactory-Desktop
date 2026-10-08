from typing import Literal
from pydantic import BaseModel, Field, model_validator, model_serializer
from .cta import CTAConfig

class Style(BaseModel):
    cta: CTAConfig | None = None
    @model_serializer(mode='wrap')
    def preserve_legacy(self, handler):
        value=handler(self)
        if value.get('cta') is None: value.pop('cta',None)
        return value
    title: str = Field(default='让灵感，成为作品', max_length=200)
    x: float = Field(default=0.5, ge=0.05, le=0.95)
    y: float = Field(default=0.15, ge=0.02, le=0.8)
    size: int = Field(default=64, ge=24, le=120)
    color: str = Field(default='#ffffff', pattern=r'^#[0-9a-fA-F]{6}$')
    stroke: int = Field(default=3, ge=0, le=10)
    stroke_color: str = Field(default='#20242c', pattern=r'^#[0-9a-fA-F]{6}$')
    start: float = Field(default=0, ge=0, le=8)
    end: float = Field(default=3, gt=0, le=8)
    arrow: bool = True
    arrow_x: float = Field(default=0.78, ge=0.1, le=0.9)
    arrow_y: float = Field(default=0.78, ge=0.15, le=0.85)
    arrow_size: int = Field(default=150, ge=60, le=240)
    arrow_color: str = Field(default='#ffffff', pattern=r'^#[0-9a-fA-F]{6}$')
    arrow_rotation: float = Field(default=0, ge=-180, le=180)
    last_seconds: float = Field(default=2, gt=0, le=8)
    @model_validator(mode='after')
    def times(self):
        if self.end <= self.start: raise ValueError('标题结束时间必须晚于开始时间')
        return self

class TaskBGM(BaseModel):
    enabled: bool = False
    music_only: bool = False
    path: str = Field(default='', max_length=2000)
    rights_confirmed: bool = False
    instrumental_confirmed: bool = False

class TaskSpec(BaseModel):
    product: str = Field(min_length=1, max_length=180)
    market: str = Field(default='BR', min_length=1, max_length=40)
    language: str = Field(default='pt-BR', min_length=1, max_length=40)
    mode: Literal['revoice'] = 'revoice'
    script_count: int = Field(default=3, ge=1, le=20)
    variants_per_script: int = Field(default=3, ge=1, le=20)
    music_only_count: int = Field(default=1, ge=0, le=20)
    voice_mix: str = Field(default='男声 2 / 女声 1', max_length=150)
    notes: str = Field(default='', max_length=3000)
    style: Style = Field(default_factory=Style)
    bgm: TaskBGM = Field(default_factory=TaskBGM)
    @model_validator(mode='after')
    def nonblank(self):
        if not all(v.strip() for v in [self.product,self.market,self.language]): raise ValueError('产品、市场和语言不能为空')
        return self

class AssetEdit(BaseModel):
    rank: int = Field(ge=1, le=10000)
    voice: Literal['any','male','female','specific'] = 'any'
    voice_id: str = Field(default='', max_length=150)
    enabled: bool = True
    rights: str = Field(default='待确认', max_length=250)
    watermark_needs_cleaning: bool = False
    source_url: str = Field(default='', max_length=1000)
    @model_validator(mode='after')
    def voice_check(self):
        if self.voice == 'specific' and not self.voice_id.strip(): raise ValueError('请填写指定 Voice ID')
        return self

class ImportSpec(BaseModel):
    paths: list[str] = Field(min_length=1,max_length=100)

class ExportSpec(BaseModel):
    asset_id: str
    duration: float = Field(default=6, ge=3, le=8)
