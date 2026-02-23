from studio_api.latest import (
    StudioAPI_latest,
    Input as Input_latest,
    InputImpl as InputImpl_latest,
    Types as Types_latest,
)
from typing import Type, TYPE_CHECKING
from studio_api.internal.async_to_sync import create_sync_class
from studio_api.latest import io, ui, IO, UI, StudioExtension  #noqa: F401


class StudioAPIAdapter_v0_0_2(StudioAPI_latest):
    VERSION = "0.0.2"
    STABLE = False


class Input(Input_latest):
    pass


class InputImpl(InputImpl_latest):
    pass


class Types(Types_latest):
    pass


StudioAPI = StudioAPIAdapter_v0_0_2

# Create a synchronous version of the API
if TYPE_CHECKING:
    from studio_api.v0_0_2.generated.StudioAPISyncStub import StudioAPISyncStub  # type: ignore

    StudioAPISync: Type[StudioAPISyncStub]
StudioAPISync = create_sync_class(StudioAPIAdapter_v0_0_2)

__all__ = [
    "StudioAPI",
    "StudioAPISync",
    "Input",
    "InputImpl",
    "Types",
    "StudioExtension",
    "io",
    "IO",
    "ui",
    "UI",
]
