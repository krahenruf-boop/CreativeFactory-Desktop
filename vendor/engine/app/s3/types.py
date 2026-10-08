from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_serializer

AUDIO_SPEED_MAX = 1.15
VIDEO_SPEED_MAX = 1.30
DURATION_TOLERANCE_RATIO = .05
FINAL_TOLERANCE = .08
FPS = 30
VOICE_LUFS = -16
VOICE_VOLUME = 1.0
BGM_VOLUME = .12

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)

class SubtitlePresetV1(Strict):
    version: Literal[1] = 1
    font_family: Literal['Arial', 'Helvetica', 'Heiti', 'Poppins'] = 'Arial'
    font_file: str | None = None
    font_size: int = Field(default=54, ge=28, le=90)
    font_weight: Literal[400, 700] = 700
    text_color: str = Field(default='#ffffff', pattern=r'^#[\da-fA-F]{6}$')
    stroke_enabled: bool = True
    stroke_color: str = Field(default='#20242c', pattern=r'^#[\da-fA-F]{6}$')
    stroke_width: int = Field(default=3, ge=0, le=8)
    shadow_enabled: bool = True
    background_enabled: bool = False
    background_color: str = Field(default='#17181b', pattern=r'^#[\da-fA-F]{6}$')
    background_opacity: float = Field(default=.65, ge=0, le=1)
    background_radius: int = Field(default=16, ge=0, le=60)
    position_x: float = Field(default=.5, ge=.15, le=.85)
    position_y: float = Field(default=.73, ge=.15, le=.82)
    max_lines: int = Field(default=2, ge=1, le=2)
    line_spacing: int = Field(default=10, ge=0, le=30)

    @model_serializer(mode='wrap')
    def serialize_font(self, handler):
        value = handler(self)
        # Keep legacy presets/plans and their cache identities byte-compatible.
        if value.get('font_file') is None: value.pop('font_file', None)
        return value

class Caption(Strict):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str
    lines: list[str]

class SubtitleTrackV1(Strict):
    version: Literal[1] = 1
    source: Literal['tts_alignment', 'none'] = 'tts_alignment'
    text: str
    captions: list[Caption]
    preset: SubtitlePresetV1

class PlanOptions(Strict):
    primary_asset_id: str | None = None
    disabled_supplement_ids: list[str] = Field(default_factory=list)
    supplement_order: list[str] = Field(default_factory=list)
    audio_speed: float | None = Field(default=None, ge=1, le=AUDIO_SPEED_MAX)
    video_speed: float | None = Field(default=None, ge=1, le=VIDEO_SPEED_MAX)
    title_enabled: bool | None = None
    voice_result_id: str | None = None

class Segment(Strict):
    asset_id: str
    source_rank: int
    source_start: float = Field(ge=0)
    source_end: float = Field(gt=0)
    output_start: float = Field(ge=0)
    output_end: float = Field(gt=0)
    speed: float = Field(ge=1, le=VIDEO_SPEED_MAX)
    role: Literal['primary', 'supplement']

class EditPlanV1(Strict):
    version: Literal[1] = 1
    edit_mode: Literal['transport'] = 'transport'
    id: str
    task_id: str
    created_at: str
    status: str
    issues: list[dict] = Field(default_factory=list)
    gap_seconds: float = 0
    target_duration: float
    voice_result_id: str
    video_speed: float
    video_segments: list[Segment]
    inputs: list[dict]
    audio: dict
    title: dict
    subtitles: SubtitleTrackV1
    bgm: dict
    cta: dict
    options: PlanOptions
    duration_tolerance_ratio: float = DURATION_TOLERANCE_RATIO
    source_audio_muted: Literal[True] = True
