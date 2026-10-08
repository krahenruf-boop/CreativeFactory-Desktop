"""Global fixed CTA template, in the shared 1080 x 1920 reference canvas."""
import math
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class CTAAnimation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['source', 'none', 'pulse', 'bounce', 'wiggle', 'float'] = 'none'

class CTAConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    enabled: bool = True
    asset_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    preset_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    position_x: float = Field(default=18, ge=0, le=100)
    position_y: float = Field(default=78, ge=0, le=100)
    anchor: Literal['center'] = 'center'
    scale: float = Field(default=.22, ge=.05, le=.5)
    rotation: float = Field(default=0, ge=-180, le=180)
    opacity: float = Field(default=1, ge=.2, le=1)
    duration_seconds: float = Field(default=2, gt=0, le=30)
    animation: CTAAnimation = Field(default_factory=CTAAnimation)

# One shared animation definition; preview and FFmpeg consume the same derived sprite.
CTA_MOTION = {
    'pulse': dict(period=.8, amount=.10),
    'bounce': dict(period=.8, amount=.008),
    'wiggle': dict(period=.8, amount=6),
    'float': dict(period=1.4, amount=.005),
}

PURCHASE_CTA = {
    'id': 'purchase-link-lower-left-v1', 'version': 1,
    'name': '购买链接 · 固定左下安全区',
    'arrow_x': .16, 'arrow_y': .82, 'arrow_size': 120, 'arrow_rotation': 45,
    'direction': 'down-left', 'tracking': False,
    'safe_bounds': [.08, .78, .24, .86],
}
GEOMETRY_KEYS = ('arrow_x', 'arrow_y', 'arrow_size', 'arrow_rotation')

def apply_purchase_cta(style):
    # Only CTA geometry changes. Title, colors, enabled flags and timing remain the user's.
    return {**style, **{k: PURCHASE_CTA[k] for k in GEOMETRY_KEYS}}

def arrow_points(style, width=1080, height=1920):
    x, y, size = style.arrow_x*width, style.arrow_y*height, style.arrow_size
    points = [(-.14,-.5),(.14,-.5),(.14,.08),(.44,.08),(0,.55),(-.44,.08),(-.14,.08)]
    angle = math.radians(style.arrow_rotation); c, s = math.cos(angle), math.sin(angle)
    return [(x+size*(px*c-py*s), y+size*(px*s+py*c)) for px,py in points]

def boxes_intersect(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
