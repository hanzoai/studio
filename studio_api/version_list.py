from studio_api.latest import StudioAPI_latest
from studio_api.v0_0_2 import StudioAPIAdapter_v0_0_2
from studio_api.v0_0_1 import StudioAPIAdapter_v0_0_1
from studio_api.internal import StudioAPIBase

supported_versions: list[type[StudioAPIBase]] = [
    StudioAPI_latest,
    StudioAPIAdapter_v0_0_2,
    StudioAPIAdapter_v0_0_1,
]

